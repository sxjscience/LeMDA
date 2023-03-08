import logging
from xmlrpc.client import Boolean
import torch
from torch import nn
import torch.nn.functional as F
import pytorch_lightning as pl
from .utils import (
    get_optimizer,
    get_lr_scheduler,
    apply_two_stages_lr,
    apply_layerwise_lr_decay,
    apply_single_lr,
    get_augment_network_parameters,
)
from ..constants import LOGITS, WEIGHT, AUTOMM
from typing import Union, Optional, List, Dict, Callable
from ..data.mixup import MixupModule, multimodel_mixup
import torchmetrics
from torchmetrics.aggregation import BaseAggregator
from torch.nn.modules.loss import _Loss

logger = logging.getLogger(AUTOMM)


class LitModule(pl.LightningModule):
    """
    Control the loops for training, evaluation, and prediction. This module is independent of
    the model definition. This class inherits from the Pytorch Lightning's LightningModule:
    https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html
    """

    def __init__(
        self,
        model: nn.Module,
        optim_type: Optional[str] = None,
        lr_choice: Optional[str] = None,
        lr_schedule: Optional[str] = None,
        lr: Optional[float] = None,
        lr_decay: Optional[float] = None,
        end_lr: Optional[Union[float, int]] = None,
        lr_mult: Optional[Union[float, int]] = None,
        weight_decay: Optional[float] = None,
        warmup_steps: Optional[int] = None,
        loss_func: Optional[_Loss] = None,
        validation_metric: Optional[torchmetrics.Metric] = None,
        validation_metric_name: Optional[str] = None,
        custom_metric_func: Callable = None,
        test_metric: Optional[torchmetrics.Metric] = None,
        efficient_finetune: Optional[str] = None,
        mixup_fn: Optional[MixupModule] = None,
        mixup_off_epoch: Optional[int] = 0,
        aug_optimizer: Optional[Boolean] = False,
        aug_turn_on: Optional[Boolean] = False,
        aug_lr: Optional[float] = None,
        aug_optim_type: Optional[str] = None,
        aug_weight_decay: Optional[float] = None,
        grad_steps: Optional[int] = 1,
    ):
        """
        Parameters
        ----------
        model
            A Pytorch model
        optim_type
            Optimizer type. We now support:
            - adamw
            - adam
            - sgd
        lr_choice
            How to set each layer's learning rate. If not specified, the default is a single
            learnng rate for all layers. Otherwise, we now support two choices:
            - two_stages
                The layers in the pretrained models have a small learning rate (lr * lr_mult),
                while the newly added head layers use the provided learning rate.
            - layerwise_decay
                The layers have decreasing learning rate from the output end to the input end.
                The intuition is that later layers are more task-related, hence larger learning rates.
        lr_schedule
            Learning rate schedule. We now support:
            - cosine_decay
                Linear warmup followed by cosine decay
            - polynomial_decay
                Linear warmup followed by polynomial decay
        lr
            Learning rate.
        lr_decay
            The learning rate decay factor (0, 1). It is used only when lr_choice is "layerwise_decay".
        end_lr
            The final learning rate after decay.
        lr_mult
            The learning rate multiplier (0, 1). It is used only when lr_choice is "two_stages".
        weight_decay
            The weight decay to regularize layer weights' l2 norm.
        warmup_steps
            How many steps to warmup learning rate. If a float (0, 1), it would represent the
            percentage of steps over all the training steps. The actual number is calculated as
            "int(warmup_steps * max_steps)". If an integer, it would be the exact step number.
        loss_func
            A Pytorch loss module, e.g., nn.CrossEntropyLoss().
        validation_metric
            A torchmetrics module used in the validation stage, e.g., torchmetrics.Accuracy().
        validation_metric_name
            Name of validation metric in case that validation_metric is a aggregation metric,
            e.g., torchmetrics.MeanMetric, whose name can't reflect the real metric name.
        custom_metric_func
            A customized metric function in case that torchmetrics doesn't have the metric.
            It is generally used together with torchmetrics' aggregators, e.g., torchmetrics.MeanMetric.
            Refer to https://github.com/PyTorchLightning/metrics/blob/master/torchmetrics/aggregation.py
        test_metric
            A torchmetrics module used in the test stage, e.g., torchmetrics.Accuracy().
        efficient_finetune
            Whether to use efficient finetuning strategies. This will be helpful for fast finetuning of large backbones.
            We support options such as:

            - bit_fit (only finetune the bias terms)
            - norm_fit (only finetune the weights in norm layers / bias layer)
            - None (do not use efficient finetuning strategies)

        """
        super().__init__()
        self.save_hyperparameters(
            ignore=["model", "validation_metric", "test_metric", "loss_func"]
        )
        self.model = model
        self.validation_metric = validation_metric
        self.validation_metric_name = f"val_{validation_metric_name}"
        self.loss_func = loss_func
        self.mixup_fn = mixup_fn
        if isinstance(validation_metric, BaseAggregator) and custom_metric_func is None:
            raise ValueError(
                f"validation_metric {validation_metric} is an aggregation metric,"
                "which must be used with a customized metric function."
            )
        self.custom_metric_func = custom_metric_func
        if aug_optimizer:
            self.automatic_optimization = False

    def _compute_loss(
        self,
        output: Dict,
        label: torch.Tensor,
    ):

        loss = 0
        for name, per_output in output.items():
            if name != "augmenter":
                weight = per_output[WEIGHT] if WEIGHT in per_output else 1

                if (
                    name.startswith("fusion")
                    and self.model.training
                    and self.model.aug_config.turn_on
                ):

                    loss += (
                        self.loss_func(
                            input=per_output[LOGITS].squeeze(dim=1),
                            target=label.tile((2,)),
                        )
                    )
                    self.log("loss/target", loss, prog_bar=True)

                else:
                    loss += (
                        self.loss_func(
                            input=per_output[LOGITS].squeeze(dim=1),
                            target=label,
                        )
                        * weight
                    )
        if "augmenter" in output.keys():
            reg_loss = 0
            kl_loss = 0
            c_loss = 0
            l = output["augmenter"]
            if "KLD_loss" in l.keys():
                kl_loss = l["KLD_loss"] * l["kl_weight"]
            if "regularizer" in l.keys():
                reg_loss = l["regularizer"] * l["reg_weight"]
            if "consist_loss" in l.keys():
                c_loss = l["consist_loss"] * l["cons_weight"]
                self.log("loss/consist", c_loss, prog_bar=True)

            self.log("loss/reg_loss", reg_loss, prog_bar=True)
            self.log("loss/kl_loss", kl_loss, prog_bar=True)

            loss = loss + reg_loss + kl_loss + c_loss
        return loss

    def _compute_metric_score(
        self,
        metric: torchmetrics.Metric,
        custom_metric_func: Callable,
        logits: torch.Tensor,
        label: torch.Tensor,
    ):
        if isinstance(self.loss_func, nn.BCEWithLogitsLoss):
            logits = torch.sigmoid(logits)
        if isinstance(metric, (torchmetrics.AUROC, torchmetrics.AveragePrecision)):
            prob = F.softmax(logits.float(), dim=1)
            metric.update(
                preds=prob[:, 1], target=label
            )  # only for binary classification
        elif isinstance(metric, BaseAggregator):
            metric.update(custom_metric_func(logits, label))
        else:
            metric.update(logits.squeeze(dim=1), label)

    def _shared_step(
        self,
        batch: Dict,
    ):
        label = batch[self.model.label_key]
        if self.mixup_fn is not None:
            self.mixup_fn.mixup_enabled = self.training & (
                self.current_epoch < self.hparams.mixup_off_epoch
            )
            batch, label = multimodel_mixup(
                batch=batch, model=self.model, mixup_fn=self.mixup_fn
            )
        output = self.model(batch, self.training)
        loss = self._compute_loss(output=output, label=label)
        return output, loss

    def training_step(self, batch, batch_idx):
        """
        Per training step. This function is registered by pl.LightningModule.
        Refer to https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#training-loop

        Parameters
        ----------
        batch
            A dictionary containing the mini-batch data, including both input data and
            ground-truth labels. The mini-batch data are passed to each individual model,
            which indexes its required input data by keys with its model prefix. The
            ground-truth labels are used here to compute the training loss.
        batch_idx
            Index of mini-batch.

        Returns
        -------
        Average loss of the mini-batch data.
        """
        if self.hparams.aug_optimizer:
            if self.hparams.aug_turn_on:
                target_optimizer, aug_optimizer = self.optimizers()
            else:
                target_optimizer = self.optimizers()
            target_opt_scheduler = self.lr_schedulers()

            output, loss = self._shared_step(batch)
            self.manual_backward(loss)

            # gradient accumulation
            if (
                batch_idx + 1
            ) % self.hparams.grad_steps == 0 or self.trainer.is_last_batch:
                nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)

                target_optimizer.step()
                target_opt_scheduler.step()
                if self.hparams.aug_turn_on:
                    aug_optimizer.step()

                target_optimizer.zero_grad()
                if self.hparams.aug_turn_on:
                    aug_optimizer.zero_grad()
        else:
            output, loss = self._shared_step(batch)
            self.log("train_loss", loss)

        return loss

    def validation_step(self, batch, batch_idx):
        """
        Per validation step. This function is registered by pl.LightningModule.
        Refer to https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#validation

        Parameters
        ----------
        batch
            A dictionary containing the mini-batch data, including both input data and
            ground-truth labels. The mini-batch data are passed to each individual model,
            which indexes its required input data by keys with its model prefix. The
            ground-truth labels are used here to compute the validation loss and metric.
            The validation metric is used for top k model selection and early stopping.
        batch_idx
            Index of mini-batch.
        """
        output, loss = self._shared_step(batch)
        # By default, on_step=False and on_epoch=True
        self.log("val_loss", loss)
        self._compute_metric_score(
            metric=self.validation_metric,
            custom_metric_func=self.custom_metric_func,
            logits=output[self.model.prefix][LOGITS],
            label=batch[self.model.label_key],
        ),
        self.log(
            self.validation_metric_name,
            self.validation_metric,
            on_step=False,
            on_epoch=True,
        )

    def predict_step(self, batch, batch_idx, dataloader_idx=0):
        """
        Per prediction step. This function is registered by pl.LightningModule.
        Refer to https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#prediction-loop

        Parameters
        ----------
        batch
            A dictionary containing the mini-batch data.
            The mini-batch data are passed to each individual model,
            which indexes its required input data by keys with its model prefix.
            Ground-truth labels are not needed for prediction.
        batch_idx
            Index of mini-batch.
        dataloader_idx
            Index of dataloader.
        Returns
        -------
        A dictionary with the mini-batch's logits and features.
        """
        output = self.model(batch, self.training)
        return output[self.model.prefix]

    def configure_optimizers(self):
        """
        Configure optimizer. This function is registered by pl.LightningModule.
        Refer to https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#configure-optimizers
        Returns
        -------
        [optimizer]
            Optimizer.
        [sched]
            Learning rate scheduler.
        """
        kwargs = dict(
            model=self.model,
            lr=self.hparams.lr,
            weight_decay=self.hparams.weight_decay,
        )
        if self.hparams.lr_choice == "two_stages":
            logger.debug("applying 2-stage learning rate...")
            grouped_parameters = apply_two_stages_lr(
                lr_mult=self.hparams.lr_mult,
                return_params=True,
                **kwargs,
            )
        elif self.hparams.lr_choice == "layerwise_decay":
            logger.debug("applying layerwise learning rate decay...")
            grouped_parameters = apply_layerwise_lr_decay(
                lr_decay=self.hparams.lr_decay,
                efficient_finetune=self.hparams.efficient_finetune,
                seperate_augment_optimizer=self.hparams.aug_optimizer,
                **kwargs,
            )
        else:
            logger.debug("applying single learning rate...")
            grouped_parameters = apply_single_lr(
                **kwargs,
            )

        optimizer = get_optimizer(
            optim_type=self.hparams.optim_type,
            optimizer_grouped_parameters=grouped_parameters,
            lr=self.hparams.lr,
            weight_decay=self.hparams.weight_decay,
        )

        logger.debug(f"trainer.max_steps: {self.trainer.max_steps}")
        if self.trainer.max_steps is None or -1:
            max_steps = (
                len(self.trainer.datamodule.train_dataloader())
                * self.trainer.max_epochs
                // self.hparams.grad_steps
            )
            logger.debug(
                f"len(trainer.datamodule.train_dataloader()): {len(self.trainer.datamodule.train_dataloader())}"
            )
            logger.debug(f"trainer.max_epochs: {self.trainer.max_epochs}")
            logger.debug(f"trainer.accumulate_grad_batches: {self.hparams.grad_steps}")
        else:
            max_steps = self.trainer.max_steps

        logger.debug(f"max steps: {max_steps}")

        warmup_steps = self.hparams.warmup_steps
        if isinstance(warmup_steps, float):
            warmup_steps = int(max_steps * warmup_steps)
        print(f"warmup steps: {warmup_steps}")
        logger.debug(f"warmup steps: {warmup_steps}")
        logger.debug(f"lr_schedule: {self.hparams.lr_schedule}")
        scheduler = get_lr_scheduler(
            optimizer=optimizer,
            num_max_steps=max_steps,
            num_warmup_steps=warmup_steps,
            lr_schedule=self.hparams.lr_schedule,
            end_lr=self.hparams.end_lr,
        )

        sched = {"scheduler": scheduler, "interval": "step"}

        aug_optimizer = None
        if self.hparams.aug_optimizer and self.hparams.aug_turn_on:
            print("initilize augment optimizer")
            # augment network optimizer
            aug_grouped_parameters = get_augment_network_parameters(
                self.model, self.hparams.aug_lr
            )
            aug_optimizer = get_optimizer(
                optim_type=self.hparams.aug_optim_type,
                optimizer_grouped_parameters=aug_grouped_parameters,
                lr=self.hparams.aug_lr,
                weight_decay=self.hparams.aug_weight_decay,
            )
            return [optimizer, aug_optimizer], [sched]
        logger.debug("done configuring optimizer and scheduler")
        return [optimizer], [sched]
