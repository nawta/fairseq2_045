# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any, Protocol, Sequence, final

import torch
from torch import Generator, Tensor
from torch.autograd import Function
from torch.nn import Dropout, Module
from torch.utils.hooks import RemovableHandle
from typing_extensions import override

from fairseq2.data_type import DataType
from fairseq2.device import CPU, Device
from fairseq2.error import InvalidOperationError
from fairseq2.nn import LayerNorm, LayerStack
from fairseq2.nn.padding import PaddingMask

# isort: split

from fairseq2.models.transformer._attention_mask import AttentionMaskFactory
from fairseq2.models.transformer._encoder_layer import TransformerEncoderLayer
from fairseq2.models.transformer._norm_order import TransformerNormOrder
from fairseq2.models.transformer._normalization import (
    LayerNormFactory,
    create_standard_layer_norm,
)


class TransformerEncoder(LayerStack, ABC):
    """Represents a Transformer encoder."""

    model_dim: int

    _layer_hooks: dict[int, TransformerEncoderLayerHook]

    def __init__(
        self, model_dim: int, layers: Sequence[TransformerEncoderLayer]
    ) -> None:
        """
        :param model_dim: The dimensionality of the model.
        """
        super().__init__(layers)

        self.model_dim = model_dim

        self._layer_hooks = OrderedDict()

    @abstractmethod
    def forward(
        self, seqs: Tensor, padding_mask: PaddingMask | None
    ) -> tuple[Tensor, PaddingMask | None]:
        """
        :param seqs:
            The sequences to encode. *Shape:* :math:`(N,S,M)`, where :math:`N`
            is the batch size, :math:`S` is the sequence length, and :math:`M`
            is the dimensionality of the model.
        :param padding_mask:
            The padding mask of ``seqs``. *Shape:* :math:`(N,S)`, where :math:`N`
            is the batch size and :math:`S` is the sequence length.

        :returns:
            - The encoder output. *Shape:* Same as ``seqs``.
            - The padding mask of the encoder output. *Shape:* Same as
              ``padding_mask``.
        """

    def register_layer_hook(self, hook: TransformerEncoderLayerHook) -> RemovableHandle:
        """
        Registers a layer hook on the module.

        The hook will be called every time after a layer in the encoder stack
        has computed an output.

        :param hook: The hook to register.

        :returns:
            A handle that can be used to remove the added hook by calling
            ``handle.remove()``.
        """
        handle = RemovableHandle(self._layer_hooks)

        self._layer_hooks[handle.id] = hook

        return handle

    def extra_repr(self) -> str:
        """:meta private:"""
        return f"model_dim={self.model_dim}"


class TransformerEncoderLayerHook(Protocol):
    """
    Represents a hook to pass to :meth:`~TransformerEncoder.register_layer_hook`.
    """

    def __call__(
        self,
        layer_idx: int,
        layer_output: Tensor,
        layer_padding_mask: PaddingMask | None,
        num_layers: int,
    ) -> bool:
        """
        :param layer_idx:
            The index of the layer in the encoder stack.
        :param layer_output:
            The encoded output of the layer.
        :param layer_padding_mask:
            The padding mask of ``layer_output``.
        :param num_layers:
            The number of layers in the encoder stack.

        :returns:
            ``True`` if the encoder should continue executing the remaining
            layers in the stack; ``False`` if the encoder should treat this
            layer as the final layer in the stack.
        """


@final
class StandardTransformerEncoder(TransformerEncoder):
    """Represents a Transformer encoder as described in
    :cite:t:`https://doi.org/10.48550/arxiv.1706.03762`."""

    self_attn_mask_factory: AttentionMaskFactory | None
    layer_drop_p: float
    generator: Generator | None
    layer_norm: LayerNorm | None
    dropout_p: float
    norm_order: TransformerNormOrder

    def __init__(
        self,
        layers: Sequence[TransformerEncoderLayer],
        *,
        self_attn_mask_factory: AttentionMaskFactory | None = None,
        layer_drop_p: float = 0.0,
        generator: Generator | None = None,
        dropout_p: float = 0.0,
        norm_order: TransformerNormOrder = TransformerNormOrder.POST,
        layer_norm_factory: LayerNormFactory | None = None,
        device: Device | None = None,
        dtype: DataType | None = None,
    ) -> None:
        """
        :param layers: The encoder layers.
        :param self_attn_mask_factory: The self attention mask factory.
        :param layer_drop_p: If greater than zero, applies LayerDrop to the
            encoder layers as described in
            :cite:t:`https://doi.org/10.48550/arxiv.1909.11556`.
        :param generator: The random number generator for LayerDrop
            probabilities.
        :param dropout_p: The dropout probability on encoder outputs.
        :param norm_order: The Layer Normalization order.
        :param layer_norm_factory: The factory to construct the Layer
            Normalization module.
        """
        if not layers:
            raise ValueError("`layers` must be non-empty.")

        model_dim = layers[0].model_dim

        super().__init__(model_dim, layers)

        if layer_norm_factory is None:
            layer_norm_factory = create_standard_layer_norm

        self.self_attn_mask_factory = self_attn_mask_factory

        self.layer_drop_p = layer_drop_p

        self.generator = generator

        if norm_order != TransformerNormOrder.POST:
            self.layer_norm = layer_norm_factory(model_dim, device=device, dtype=dtype)
        else:
            self.register_module("layer_norm", None)

        if dropout_p > 0.0:
            self.dropout = Dropout(dropout_p)
        else:
            self.register_module("dropout", None)

        self.norm_order = norm_order

    @override
    def forward(
        self, seqs: Tensor, padding_mask: PaddingMask | None
    ) -> tuple[Tensor, PaddingMask | None]:
        if self._layer_hooks and self.layer_drop_p > 0.0 and self.training:
            raise InvalidOperationError(
                "The layer output hooks cannot be run when LayerDrop is enabled."
            )

        num_layers = len(self.layers)

        if self.self_attn_mask_factory is None:
            self_attn_mask = None
        else:
            self_attn_mask = self.self_attn_mask_factory(
                seqs, keys=seqs, training=self.training
            )

        for layer_idx, (layer, drop) in enumerate(self._drop_iter()):
            layer_output, layer_padding_mask = layer(seqs, padding_mask, self_attn_mask)

            if drop:
                seqs = _record_drop_for_backward(seqs, layer_output)

                continue

            seqs, padding_mask = layer_output, layer_padding_mask

            for hook in self._layer_hooks.values():
                if not hook(layer_idx, seqs, padding_mask, num_layers):
                    break

        if self.layer_norm is not None:
            seqs = self.layer_norm(seqs)

        if self.dropout is not None:
            seqs = self.dropout(seqs)

        return seqs, padding_mask

    def _drop_iter(self) -> Iterator[tuple[Module, bool]]:
        if self.training and self.layer_drop_p > 0.0:
            prob_dist = torch.rand(
                len(self.layers), generator=self.generator, device=CPU
            )
        else:
            prob_dist = None

        for idx, m in enumerate(self.layers):
            drop = prob_dist is not None and float(prob_dist[idx]) <= self.layer_drop_p

            yield m, drop

    def extra_repr(self) -> str:
        """:meta private:"""
        s = super().extra_repr()

        if self.self_attn_mask_factory is not None:
            self_attn_mask_factory = getattr(
                self.self_attn_mask_factory, "__name__", self.self_attn_mask_factory
            )

            s = f"{s}, self_attn_mask_factory={self_attn_mask_factory}"

        if self.layer_drop_p > 0.0:
            s = f"{s}, layer_drop_p={self.layer_drop_p:G}"

        return f"{s}, norm_order={self.norm_order.name}"


# mypy: disable-error-code="no-any-return,override"


def _record_drop_for_backward(x: Tensor, dropped_output: Tensor) -> Tensor:
    return _RecordDropForBackwardFunction.apply(x, dropped_output)


class _RecordDropForBackwardFunction(Function):
    @staticmethod
    def forward(ctx: Any, x: Tensor, dropped_output: Tensor) -> Tensor:
        return x

    @staticmethod
    def backward(ctx: Any, grad_output: Tensor) -> tuple[Tensor, Tensor]:
        return grad_output, torch.zeros_like(grad_output)
