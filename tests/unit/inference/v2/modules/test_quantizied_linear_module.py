# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team

from typing import Optional

import pytest
import torch

from deepspeed.accelerator import get_accelerator
from deepspeed.inference.v2.inference_utils import ActivationType, DtypeEnum, is_gated
from deepspeed.inference.v2.modules import ConfigBundle
from deepspeed.inference.v2.modules.configs import DSLinearConfig
from deepspeed.inference.v2.modules.interfaces import DSLinearRegistry
from ...v2.inference_test_utils import allclose


def save_tensor_to_file(tensor: torch.Tensor, file_name: str) -> None:
    import numpy as np
    np.savetxt(file_name, tensor.cpu().numpy(), delimiter=',',
               newline='},\n{', header='half xxx={\n{', footer='}\n};', comments='', encoding=None)


def load_tensor_from_file(file_name: str) -> torch.Tensor:
    import numpy as np
    data = np.loadtxt(file_name, delimiter=',')
    return torch.as_tensor(data, dtype=torch.float16, device=get_accelerator().current_device_name())


def reference_implementation(hidden_states: torch.Tensor, weight: torch.Tensor, bias: Optional[torch.Tensor],
                             act_type: ActivationType) -> torch.Tensor:
    dtype = hidden_states.dtype
    out_states = torch.nn.functional.linear(hidden_states, weight, bias)
    out_states.float()

    if is_gated(act_type):
        act_func_map = {
            ActivationType.ReGLU: torch.nn.functional.relu,
            ActivationType.GEGLU: lambda x: torch.nn.functional.gelu(x, approximate="tanh"),
            ActivationType.SiGLU: torch.nn.functional.silu,
        }

        act_act = out_states[..., ::2]
        act_linear = out_states[..., 1::2]

        act_act = act_func_map[act_type](act_act)
        out_states = act_act * act_linear
    else:
        act_func_map = {
            ActivationType.RELU: torch.nn.functional.relu,
            ActivationType.GELU: torch.nn.functional.gelu,
            ActivationType.SILU: torch.nn.functional.silu,
            ActivationType.IDENTITY: lambda x: x,
        }

        out_states = act_func_map[act_type](out_states)
    return out_states.to(dtype)


def _fp6_quant_dequant_weights(weight: torch.Tensor) -> torch.Tensor:
    from deepspeed.inference.v2.modules.implementations.linear.quantized_linear import fp_quantize
    weight_quantized_fake_fp6, scales = fp_quantize(
        weight, num_bits=6, exp_bits=3)
    if False:
        import numpy as np
        save_tensor_to_file(weight_quantized_fake_fp6, 'quantized_weight.txt')
        save_tensor_to_file(scales, 'scales.txt')
    if False:
        scales = torch.full_like(scales, 1)
    return weight_quantized_fake_fp6 * scales
    # return weight_quantized_fake_fp6


def quant_dequant_implementation(hidden_states: torch.Tensor, weight: torch.Tensor, bias: Optional[torch.Tensor],
                                 act_type: ActivationType) -> torch.Tensor:
    dtype = hidden_states.dtype
    weight_dequantized = _fp6_quant_dequant_weights(weight)
    out_states = torch.nn.functional.linear(
        hidden_states, weight_dequantized, bias)
    out_states.float()

    if is_gated(act_type):
        act_func_map = {
            ActivationType.ReGLU: torch.nn.functional.relu,
            ActivationType.GEGLU: lambda x: torch.nn.functional.gelu(x, approximate="tanh"),
            ActivationType.SiGLU: torch.nn.functional.silu,
        }

        act_act = out_states[..., ::2]
        act_linear = out_states[..., 1::2]

        act_act = act_func_map[act_type](act_act)
        out_states = act_act * act_linear
    else:
        act_func_map = {
            ActivationType.RELU: torch.nn.functional.relu,
            ActivationType.GELU: torch.nn.functional.gelu,
            ActivationType.SILU: torch.nn.functional.silu,
            ActivationType.IDENTITY: lambda x: x,
        }

        out_states = act_func_map[act_type](out_states)
    return out_states.to(dtype)


def _fp6_quantized_weights_helper(
    in_channels: int,
    out_channels: int,
    dtype: DtypeEnum,
    act_fn: ActivationType,
) -> torch.Tensor:
    weight_out_channels = 2 * \
        out_channels if is_gated(act_fn) else out_channels
    weight = torch.randn(
        (weight_out_channels, in_channels), dtype=dtype.value, device=get_accelerator().current_device_name()) * .01
    weight_dequantized = _fp6_quant_dequant_weights(weight)
    tolerances = (4.8e-1, 3.2e-2)  # tolerances for bf16
    # tolerances = (3e-2, 2e-3) # tolerances for fp16
    assert allclose(weight_dequantized, weight, tolerances=tolerances)


def _fp6_quantized_linear_helper(tokens: int,
                                 in_channels: int,
                                 out_channels: int,
                                 dtype: DtypeEnum,
                                 act_fn: ActivationType,
                                 use_bias: bool = True) -> None:
    # Input vals
    hidden_states = torch.randn(
        (tokens, in_channels), dtype=dtype.value, device=get_accelerator().current_device_name()) * .01

    weight_out_channels = 2 * \
        out_channels if is_gated(act_fn) else out_channels
    weight = torch.randn(
        (weight_out_channels, in_channels), dtype=dtype.value, device=get_accelerator().current_device_name()) * .01
    if False:
        # hidden_states = torch.full_like(
        #     hidden_states, 1.0, dtype=dtype.value, device=get_accelerator().current_device_name())
        # hidden_val = torch.as_tensor(range(
        #     0, hidden_states.shape[0]), dtype=dtype.value, device=get_accelerator().current_device_name())
        # hidden_states = hidden_states * hidden_val.reshape(-1, 1)
        # print(f"hidden is: {hidden_states}")

        # save_tensor_to_file(hidden_states, 'hidden_states.txt')
        hidden_states = load_tensor_from_file('hidden_states.txt')

        weight = torch.full_like(
            weight, 1, dtype=dtype.value, device=get_accelerator().current_device_name())
        weight_val = torch.as_tensor(range(
            0, weight.shape[1]), dtype=dtype.value, device=get_accelerator().current_device_name())
        weight = weight * weight_val.reshape(1, -1)*0.01
    if use_bias:
        bias = torch.randn(
            (weight_out_channels), dtype=dtype.value, device=get_accelerator().current_device_name()) * .01
    else:
        bias = None

    # quantize and dequantize output
    ref_quant_dequant_output = quant_dequant_implementation(
        hidden_states, weight, bias, act_fn)

    linear_config = DSLinearConfig(max_tokens=2048,
                                   in_channels=in_channels,
                                   out_channels=out_channels,
                                   activation=act_fn,
                                   input_dtype=dtype,
                                   output_dtype=dtype)
    bundle = ConfigBundle(name='quantized_wf6af16_linear',
                          config=linear_config)
    fp6_linear_module = DSLinearRegistry.instantiate_config(bundle)
    weight_fp6 = fp6_linear_module.transform_param(
        weight.clone().cpu()).to(get_accelerator().current_device_name())
    ds_output = fp6_linear_module(hidden_states, weight_fp6, bias)

    # tolerances = (4.8e-1, 3.2e-2)  # tolerances for bf16
    tolerances = (3e-2, 2e-3)  # tolerances for fp16

    if True:
        print(ds_output)
        print(ref_quant_dequant_output)

    # Check DeepSpeed implementation
    assert allclose(ds_output, ref_quant_dequant_output, tolerances=tolerances)

    # # Check reference implementation
    # ref_output = reference_implementation(hidden_states, weight, bias, act_fn)
    # assert allclose(ds_output, ref_output, tolerances=tolerances)


all_acts = [
    ActivationType.IDENTITY,
    # ActivationType.RELU,
    # ActivationType.GELU,
    # ActivationType.SILU,
    # ActivationType.GEGLU,
    # ActivationType.ReGLU,
    # ActivationType.SiGLU,
]
# all_tokens = [1, 37, 1280]
# all_in_out_channels = [(4608, 1728), (8192, 4096), (3072, 6144)]
all_tokens = [32]
all_in_out_channels = [(4096, 4096), (8192, 8192), (3072, 3072), (1024, 1024), (512, 512), (256, 256)]

# @pytest.mark.inference_v2_ops
# @pytest.mark.parametrize("in_channels, out_channels", all_in_out_channels)
# @pytest.mark.parametrize("act_fn", all_acts)
# @pytest.mark.parametrize("use_bias", [True, False])
# def test_fp6_quantized_weights(in_channels: int, out_channels: int, act_fn: ActivationType, use_bias: bool) -> None:
#     _fp6_quantized_weights_helper(in_channels, out_channels, DtypeEnum.fp16, act_fn)


@pytest.mark.inference_v2_ops
@pytest.mark.parametrize("tokens", all_tokens)
@pytest.mark.parametrize("in_channels, out_channels", all_in_out_channels)
@pytest.mark.parametrize("act_fn", all_acts)
# @pytest.mark.parametrize("use_bias", [True, False])
@pytest.mark.parametrize("use_bias", [False])
def test_fp6_quantized_linear_act_fn(tokens: int, in_channels: int, out_channels: int, act_fn: ActivationType,
                                     use_bias: bool) -> None:
    _fp6_quantized_linear_helper(
        tokens, in_channels, out_channels, DtypeEnum.fp16, act_fn, use_bias=use_bias)