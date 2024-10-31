#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict
# pyre-ignore-all-errors[56]

import random
import unittest
from typing import Callable, Dict, List, Optional, Tuple

import hypothesis.strategies as st
import numpy as np
import torch
from fbgemm_gpu.split_embedding_configs import SparseType
from fbgemm_gpu.split_table_batched_embeddings_ops_common import (
    CacheAlgorithm,
    EmbeddingLocation,
    PoolingMode,
)
from fbgemm_gpu.split_table_batched_embeddings_ops_inference import (
    IntNBitTableBatchedEmbeddingBagsCodegen,
)
from fbgemm_gpu.split_table_batched_embeddings_ops_training import DEFAULT_ASSOC
from fbgemm_gpu.tbe.utils import generate_requests, quantize_embs, round_up
from hypothesis import assume, given, HealthCheck, settings, Verbosity

from ..common import MAX_EXAMPLES, MAX_EXAMPLES_LONG_RUNNING, open_source
from .common import get_nbit_weights_ty, NBitFowardTestCommon

if open_source:
    # pyre-ignore[21]
    from test_utils import gpu_unavailable, optests, TEST_WITH_ROCM
else:
    from fbgemm_gpu.test.test_utils import gpu_unavailable, optests, TEST_WITH_ROCM


VERBOSITY: Verbosity = Verbosity.verbose


# pyre-ignore
additional_decorators: Dict[str, List[Callable]] = {
    "test_faketensor__test_nbit_forward_uvm_cache": [
        unittest.skip("CUDA Assert"),
    ],
    "test_faketensor__test_nbit_forward_cpu": [
        unittest.skip("Operator not implemented for Meta tensors"),
    ],
    "test_faketensor__test_nbit_forward_fused_pooled_emb_quant": [
        unittest.skip("Operator not implemented for Meta tensors"),
    ],
    "test_faketensor__test_nbit_forward_gpu_no_cache": [
        unittest.skip("Operator not implemented for Meta tensors"),
    ],
    "test_faketensor__test_nbit_forward_gpu_no_cache_fp8_2048": [
        unittest.skip("Operator not implemented for Meta tensors"),
    ],
    "test_faketensor__test_nbit_forward_cpu_seq_int8": [
        unittest.skip("Operator not implemented for Meta tensors"),
    ],
    "test_faketensor__test_nbit_forward_cpu_gpu_dequantize_parity": [
        unittest.skip("Operator not implemented for Meta tensors"),
    ],
    "test_faketensor__test_nbit_forward_cpu_seq_int4": {
        unittest.skip(
            "Operator outputs int4 tensors which do not support opcheck tests"
        ),
    },
    "test_schema__test_nbit_forward_cpu_seq_int4": {
        unittest.skip(
            "Operator outputs int4 tensors which do not support opcheck tests"
        ),
    },
    "test_autograd_registration__test_nbit_forward_cpu_seq_int4": {
        unittest.skip(
            "Operator outputs int4 tensors which do not support opcheck tests"
        ),
    },
    "test_aot_dispatch_static__test_nbit_forward_cpu_seq_int4": {
        unittest.skip(
            "Operator outputs int4 tensors which do not support opcheck tests"
        ),
    },
    "test_aot_dispatch_dynamic__test_nbit_forward_cpu_seq_int4": {
        unittest.skip(
            "Operator outputs int4 tensors which do not support opcheck tests"
        ),
    },
    "test_pt2_compliant_tag_fbgemm_int_nbit_split_embedding_codegen_lookup_function": [
        unittest.skip(
            "Operator outputs int4 tensors which do not support opcheck tests"
        ),
    ],
}


@optests.generate_opcheck_tests(fast=True, additional_decorators=additional_decorators)
class NBitFowardTest(NBitFowardTestCommon):
    @unittest.skipIf(*gpu_unavailable)
    @given(
        T=st.integers(min_value=1, max_value=10),
        D=st.integers(min_value=2, max_value=128),
        B=st.integers(min_value=1, max_value=128),
        log_E=st.integers(min_value=3, max_value=5),
        L=st.integers(min_value=0, max_value=20),
        weights_ty=st.sampled_from(
            [
                SparseType.FP32,
                SparseType.FP16,
                SparseType.INT8,
                SparseType.INT4,
                # FIXME: INT2 caused big numerical error for this test
                # SparseType.INT2,
            ]
        ),
        output_dtype=(
            st.sampled_from(
                [
                    SparseType.FP16,
                    SparseType.BF16,
                    SparseType.INT8,
                    # SparseType.INT4,
                ]
            )
            if not TEST_WITH_ROCM
            else st.sampled_from(
                [
                    SparseType.FP16,
                    # The counterparts of __nv_bfloat16 and __nv_bfloat162 are not supported on ROCm
                    SparseType.INT8,
                    # SparseType.INT4,
                ]
            )
        ),
    )
    @settings(
        verbosity=VERBOSITY,
        max_examples=MAX_EXAMPLES_LONG_RUNNING,
        deadline=None,
        suppress_health_check=[HealthCheck.filter_too_much],
    )
    def test_nbit_forward_fused_pooled_emb_quant(
        self,
        T: int,
        D: int,
        B: int,
        log_E: int,
        L: int,
        weights_ty: SparseType,
        output_dtype: SparseType,
    ) -> None:
        D_alignment = max(weights_ty.align_size() for t in range(T))
        D_alignment = max(D_alignment, output_dtype.align_size())
        D = round_up(D, D_alignment)
        # BF16 output only works for CUDA device sm80+ (e.g., A100)
        assume(
            torch.cuda.is_available()
            and torch.cuda.get_device_capability() >= (8, 0)
            or not output_dtype == SparseType.BF16
        )
        Ds = [
            round_up(
                np.random.randint(low=int(max(0.25 * D, 1)), high=int(1.0 * D)),
                D_alignment,
            )
            for _ in range(T)
        ]
        Ds = [D] * T
        E = int(10**log_E)
        Es = [np.random.randint(low=int(0.5 * E), high=int(2.0 * E)) for _ in range(T)]

        weights_ty_list = [weights_ty] * T
        managed = [EmbeddingLocation.DEVICE] * T
        op = IntNBitTableBatchedEmbeddingBagsCodegen(
            embedding_specs=[
                (
                    "",
                    E,
                    D,
                    W_TY,
                    EmbeddingLocation(M),
                )
                for (E, D, M, W_TY) in zip(Es, Ds, managed, weights_ty_list)
            ],
            output_dtype=output_dtype,
            device=torch.cuda.current_device(),
        )
        # Initialize the random weights for int nbit table split embedding bag
        op.fill_random_weights()

        op_ref = IntNBitTableBatchedEmbeddingBagsCodegen(
            embedding_specs=[
                (
                    "",
                    E,
                    D,
                    W_TY,
                    EmbeddingLocation(M),
                )
                for (E, D, M, W_TY) in zip(Es, Ds, managed, weights_ty_list)
            ],
            output_dtype=SparseType.FP32,
            device=torch.cuda.current_device(),
        )
        # Initialize the random weights for int nbit table split embedding bag
        op_ref.fill_random_weights()

        # sync weights between two ops
        split_weights = op.split_embedding_weights()
        ref_split_weights = op_ref.split_embedding_weights()
        for t in range(T):
            (weights, scale_shift) = split_weights[t]
            (ref_weights, ref_scale_shift) = ref_split_weights[t]
            self.assertEqual(weights.size(), ref_weights.size())
            element_size = weights_ty_list[t].bit_rate() / 8.0
            rand_tensor = torch.rand(
                ref_weights.shape[0], int(ref_weights.shape[1] / element_size)
            )
            rand_weights, rand_scale_shift = quantize_embs(
                rand_tensor, weights_ty_list[t]
            )
            ref_weights.copy_(rand_weights)
            weights.copy_(ref_weights)
            if rand_scale_shift is not None:
                self.assertIsNotNone(scale_shift)
                self.assertIsNotNone(ref_scale_shift)
                ref_scale_shift.copy_(rand_scale_shift)
                scale_shift.copy_(ref_scale_shift)

        requests = generate_requests(1, B, T, L, min(Es), reuse=0.1)
        for req in requests:
            indices, offsets = req.unpack_2()
            lowp_pooled_output = op(
                indices=indices.int(),
                offsets=offsets.int(),
            )
            fp32_pooled_output = op_ref(
                indices=indices.int(),
                offsets=offsets.int(),
            )
            lowp_pooled_emb_split = [
                d + 8 if output_dtype == SparseType.INT8 else d for d in Ds
            ]
            lowp_pooled_output_per_table = torch.split(
                lowp_pooled_output, lowp_pooled_emb_split, dim=1
            )
            deq_lowp_pooled_output_per_table = [
                (
                    torch.ops.fbgemm.Fused8BitRowwiseQuantizedToFloat(t.contiguous())
                    if output_dtype == SparseType.INT8
                    else t.float()
                )
                for t in lowp_pooled_output_per_table
            ]
            fp32_pooled_output_per_table = torch.split(fp32_pooled_output, Ds, dim=1)
            dq_fp32_pooled_output_per_table = [
                (
                    torch.ops.fbgemm.Fused8BitRowwiseQuantizedToFloat(
                        torch.ops.fbgemm.FloatToFused8BitRowwiseQuantized(
                            t.contiguous()
                        ).contiguous()
                    ).contiguous()
                    if output_dtype == SparseType.INT8
                    else t.half().float()
                )
                for t in fp32_pooled_output_per_table
            ]
            cat_deq_lowp_pooled_output = torch.cat(
                deq_lowp_pooled_output_per_table, dim=1
            )
            cat_dq_fp32_pooled_output = torch.cat(
                dq_fp32_pooled_output_per_table, dim=1
            )
            torch.testing.assert_close(
                cat_deq_lowp_pooled_output,
                cat_dq_fp32_pooled_output,
                rtol=1e-2,
                atol=1e-2,
                equal_nan=True,
            )

    @given(
        nbit_weights_ty=get_nbit_weights_ty(),
        use_array_for_index_remapping=st.booleans(),
        do_pruning=st.booleans(),
        pooling_mode=st.sampled_from(
            [PoolingMode.SUM, PoolingMode.NONE, PoolingMode.MEAN]
        ),
        indices_dtype=st.sampled_from([torch.int32, torch.int64]),
        output_dtype=st.sampled_from(
            [SparseType.FP32, SparseType.FP16, SparseType.BF16]
        ),
    )
    @settings(
        verbosity=VERBOSITY,
        max_examples=MAX_EXAMPLES_LONG_RUNNING,
        deadline=None,
    )
    def test_nbit_forward_cpu(
        self,
        nbit_weights_ty: Optional[SparseType],
        use_array_for_index_remapping: bool,
        do_pruning: bool,
        pooling_mode: PoolingMode,
        indices_dtype: torch.dtype,
        output_dtype: SparseType,
    ) -> None:
        use_cpu = True
        T = random.randint(1, 50)
        B = random.randint(0, 128)
        L = random.randint(0, 32)
        D = random.randint(2, 2048)
        log_E = random.randint(2, 4)

        use_cache = False
        # cache_algorithm is don't care as we don't use cache.
        cache_algorithm = CacheAlgorithm.LRU

        mixed = random.choice([True, False])
        if pooling_mode == PoolingMode.SUM:
            weighted = random.choice([True, False])
        else:
            weighted = False

        if nbit_weights_ty is None:
            # don't care when mixed type is used.
            weights_ty: SparseType = SparseType.INT8
            mixed_weights_ty = True
        else:
            weights_ty: SparseType = nbit_weights_ty
            mixed_weights_ty = False

        self.execute_nbit_forward_(
            T,
            D,
            B,
            log_E,
            L,
            weighted,
            mixed,
            pooling_mode,
            weights_ty,
            use_cache,
            cache_algorithm,
            use_cpu,
            use_array_for_index_remapping,
            do_pruning,
            mixed_weights_ty,
            indices_dtype,
            output_dtype,
        )

    @given(
        indices_dtype=st.sampled_from([torch.int32, torch.int64]),
    )
    @settings(deadline=None)
    @unittest.skipIf(*gpu_unavailable)
    def test_nbit_forward_gpu_no_cache_fp8_2048(
        self, indices_dtype: torch.dtype
    ) -> None:
        # Test the case of FB8 table with 128B*8 < D <= 128B*16
        self.execute_nbit_forward_(
            T=1,
            D=2048,  # 128B*8 < D <= 128B*16
            B=128,
            log_E=2,
            L=4,
            weighted=False,
            mixed=False,
            pooling_mode=PoolingMode.SUM,
            weights_ty=SparseType.FP8,  # FP8 table
            use_cache=False,
            cache_algorithm=CacheAlgorithm.LRU,
            use_cpu=False,
            use_array_for_index_remapping=True,
            do_pruning=False,
            mixed_weights_ty=False,
            indices_dtype=indices_dtype,
            output_dtype=SparseType.FP16,
        )

    @unittest.skipIf(*gpu_unavailable)
    @given(
        nbit_weights_ty=get_nbit_weights_ty(),
        use_array_for_index_remapping=st.booleans(),
        do_pruning=st.booleans(),
        indices_dtype=st.sampled_from([torch.int32, torch.int64]),
        output_dtype=st.sampled_from([SparseType.FP32, SparseType.FP16]),
    )
    @settings(
        verbosity=VERBOSITY,
        max_examples=MAX_EXAMPLES_LONG_RUNNING,
        deadline=None,
    )
    def test_nbit_forward_gpu_no_cache(
        self,
        nbit_weights_ty: Optional[SparseType],
        use_array_for_index_remapping: bool,
        indices_dtype: torch.dtype,
        do_pruning: bool,
        output_dtype: SparseType,
    ) -> None:
        # NOTE: The combination of INT2 and FP32 as weight and output types, respectively, is
        # currently not supported.
        if nbit_weights_ty == SparseType.INT2 and output_dtype == SparseType.FP32:
            self.skipTest(
                "The combination of INT2 and FP32 as weight and output types, respectively, is not supported"
            )

        # NOTE: Hash-based index remapping in general is an experimental feature
        if indices_dtype != torch.int32 and not use_array_for_index_remapping:
            self.skipTest(
                "Hash-based index_remapping is an experimental feature and is "
                "currently not supported for indices.dtype == torch.int64 and "
                "indices.device != cpu"
            )

        use_cpu = False
        T = random.randint(1, 50)
        B = random.randint(0, 128)
        L = random.randint(0, 32)
        D = random.randint(2, 2048)
        log_E = random.randint(2, 4)

        use_cache = False
        # cache_algorithm is don't care as we don't use cache.
        cache_algorithm = CacheAlgorithm.LRU

        pooling_mode = random.choice(
            [
                PoolingMode.SUM,
                PoolingMode.MEAN,
                PoolingMode.NONE,
            ]
        )
        if pooling_mode == PoolingMode.NONE:
            mixed = False
        else:
            mixed = random.choice([True, False])
        if pooling_mode == PoolingMode.SUM:
            weighted = random.choice([True, False])
        else:
            weighted = False

        if nbit_weights_ty is None:
            # don't care when mixed type is used.
            weights_ty: SparseType = SparseType.INT8
            mixed_weights_ty = True
        else:
            weights_ty: SparseType = nbit_weights_ty
            mixed_weights_ty = False

        self.execute_nbit_forward_(
            T,
            D,
            B,
            log_E,
            L,
            weighted,
            mixed,
            pooling_mode,
            weights_ty,
            use_cache,
            cache_algorithm,
            use_cpu,
            use_array_for_index_remapping,
            do_pruning,
            mixed_weights_ty,
            indices_dtype,
            output_dtype,
        )

    @unittest.skipIf(*gpu_unavailable)
    @given(
        weights_ty=st.sampled_from(
            [
                SparseType.FP32,
                SparseType.FP16,
                SparseType.INT8,
                SparseType.INT4,
                SparseType.INT2,
            ]
        ),
        cache_algorithm=st.sampled_from(CacheAlgorithm),
        associativity=st.sampled_from([1, DEFAULT_ASSOC]),
        do_pruning=st.booleans(),
        use_array_for_index_remapping=st.booleans(),
    )
    @settings(verbosity=VERBOSITY, max_examples=MAX_EXAMPLES, deadline=None)
    def test_nbit_forward_uvm_cache(
        self,
        weights_ty: SparseType,
        cache_algorithm: CacheAlgorithm,
        associativity: int,
        do_pruning: bool,
        use_array_for_index_remapping: bool,
    ) -> None:
        assume(cache_algorithm == CacheAlgorithm.LRU or associativity != 1)

        T = random.randint(1, 5)
        B = random.randint(1, 128)
        L = random.randint(1, 20)
        D = random.randint(2, 256)
        log_E = random.randint(3, 5)
        mixed = random.choice([True, False])

        iters = 3
        E = int(10**log_E)

        D_alignment = (
            1 if weights_ty.bit_rate() % 8 == 0 else int(8 / weights_ty.bit_rate())
        )
        D = round_up(D, D_alignment)

        if not mixed:
            Ds = [D] * T
            Es = [E] * T
        else:
            Ds = [
                round_up(
                    np.random.randint(low=int(max(0.25 * D, 1)), high=int(1.0 * D)),
                    D_alignment,
                )
                for _ in range(T)
            ]
            Es = [
                np.random.randint(low=int(0.5 * E), high=int(2.0 * E)) for _ in range(T)
            ]
        managed = [EmbeddingLocation.MANAGED_CACHING] * T
        if mixed:
            average_D = sum(Ds) // T
            for t, d in enumerate(Ds):
                managed[t] = EmbeddingLocation.DEVICE if d < average_D else managed[t]
        index_remapping = None
        pruning_hash_load_factor = 0.5
        if do_pruning:
            current_device = torch.cuda.current_device()
            index_remapping = []
            for E in Es:
                # For each table, keep the first half of rows as is, but
                # the rest is treated as pruned (-1).
                remapping = list(range(0, E // 2)) + [-1] * (E - E // 2)
                remapping_t = torch.tensor(
                    remapping,
                    dtype=torch.int32,
                    device=current_device,
                )
                index_remapping.append(remapping_t)
        cc_ref = IntNBitTableBatchedEmbeddingBagsCodegen(
            [
                (
                    "",
                    E,
                    D,
                    weights_ty,
                    EmbeddingLocation.DEVICE,
                )
                for (E, D) in zip(Es, Ds)
            ],
            index_remapping=index_remapping,
            use_array_for_index_remapping=use_array_for_index_remapping,
            pruning_hash_load_factor=pruning_hash_load_factor,
        )
        cc_ref.fill_random_weights()
        cc = IntNBitTableBatchedEmbeddingBagsCodegen(
            [("", E, D, weights_ty, M) for (E, D, M) in zip(Es, Ds, managed)],
            cache_algorithm=cache_algorithm,
            cache_assoc=associativity,
            index_remapping=index_remapping,
            use_array_for_index_remapping=use_array_for_index_remapping,
            pruning_hash_load_factor=pruning_hash_load_factor,
        )
        cc.fill_random_weights()

        split_weights = cc.split_embedding_weights()
        ref_split_weights = cc_ref.split_embedding_weights()
        for t in range(T):
            (weights, scale_shift) = split_weights[t]
            (ref_weights, ref_scale_shift) = ref_split_weights[t]
            self.assertEqual(weights.size(), ref_weights.size())
            weights.copy_(ref_weights)
            if ref_scale_shift is not None:
                scale_shift.copy_(ref_scale_shift)

        requests = generate_requests(iters, B, T, L, min(Es), reuse=0.1)

        for req in requests:
            indices, offsets = req.unpack_2()
            indices = indices.int()
            offsets = offsets.int()
            output = cc(indices, offsets)
            output_ref = cc_ref(indices, offsets)
            torch.testing.assert_close(output, output_ref, equal_nan=True)

    @given(
        D=st.sampled_from([32, 256, 384, 512, 1024]),
        B=st.integers(min_value=8, max_value=32),
        T=st.integers(min_value=10, max_value=20),
        L=st.integers(min_value=10, max_value=100),
        MAXH=st.integers(min_value=50, max_value=100),
    )
    @settings(
        verbosity=VERBOSITY,
        max_examples=MAX_EXAMPLES_LONG_RUNNING,
        deadline=None,
    )
    def test_nbit_forward_cpu_seq_int8(
        self,
        D: int,
        B: int,
        T: int,
        L: int,
        MAXH: int,
    ) -> None:
        """
        we init a quant table split embedding bag with int8 weights and scale of 1 and 0 bias
        and compare brute force table lookup vs tbe based int8 output lookup.
        """
        pooling_mode = PoolingMode.NONE

        nbit_weights_ty = SparseType.INT8
        D_alignment = (
            1
            if nbit_weights_ty.bit_rate() % 8 == 0
            else int(8 / nbit_weights_ty.bit_rate())
        )
        D = round_up(D, D_alignment)
        T_H = [np.random.randint(low=1, high=MAXH + 1) for _ in range(T)]
        quant_cc = IntNBitTableBatchedEmbeddingBagsCodegen(
            embedding_specs=[
                (
                    "",
                    H,
                    D,
                    nbit_weights_ty,
                    EmbeddingLocation.HOST,
                )
                for H in T_H
            ],
            pooling_mode=pooling_mode,
            device="cpu",
            output_dtype=nbit_weights_ty,
        )
        # Initialize the random weights for int nbit table split embedding bag
        quant_cc.fill_random_weights()
        raw_embedding_weights = quant_cc.split_embedding_weights()
        # we mimic 1.0 scale, 0.0 bias for better results comparison
        embedding_weights: List[Tuple[torch.Tensor, Optional[torch.Tensor]]] = [
            (table_weight, torch.tensor([1, 0], dtype=torch.float16).view(torch.uint8))
            for table_weight, _ in raw_embedding_weights
        ]
        # Initialize the random weights for int8 nbit table split embedding bag
        quant_cc.assign_embedding_weights(embedding_weights)
        lengths_list = [
            torch.randint(
                1,
                L + 1,
                (B,),
            )
            for _ in range(T)
        ]
        indices_list = [
            torch.randint(0, H, (int(length.sum().item()),))
            for length, H in zip(lengths_list, T_H)
        ]
        indices = torch.cat(indices_list, 0)
        lengths = torch.cat(lengths_list, 0)
        offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(lengths)
        quant_cc_output = quant_cc(indices.int(), offsets.int())
        tables_rows = [
            T for T, _, _ in quant_cc.split_embedding_weights_with_scale_bias(0)
        ]
        ref_output = torch.cat(
            [
                table_rows[indice_table]
                for indice_table, table_rows in zip(indices_list, tables_rows)
            ],
            dim=0,
        )
        torch.testing.assert_close(
            quant_cc_output.cpu(),
            ref_output.cpu(),
            equal_nan=False,
        )

    @given(
        D=st.sampled_from([32, 256, 384, 512, 1024]),
        B=st.integers(min_value=8, max_value=32),
        T=st.integers(min_value=10, max_value=20),
        L=st.integers(min_value=10, max_value=100),
        MAXH=st.integers(min_value=50, max_value=100),
        indices_dtype=st.sampled_from([torch.int32, torch.int64]),
    )
    @settings(
        verbosity=VERBOSITY,
        max_examples=MAX_EXAMPLES_LONG_RUNNING,
        deadline=None,
    )
    def test_nbit_forward_cpu_seq_int4(
        self,
        D: int,
        B: int,
        T: int,
        L: int,
        MAXH: int,
        indices_dtype: torch.dtype,
    ) -> None:
        """
        we init a quant table split embedding bag with int4 weights and scale of 1 and 0 bias
        and compare brute force table lookup vs tbe based int4 output lookup.
        """
        self.execute_nbit_forward_(
            T,
            D,
            B,
            log_E=4,
            L=L,
            weighted=False,
            mixed=False,
            pooling_mode=PoolingMode.NONE,
            weights_ty=SparseType.INT4,
            use_cache=False,
            cache_algorithm=CacheAlgorithm.LRU,  # doesn't matter since we don't use cache
            use_cpu=True,
            use_array_for_index_remapping=True,
            do_pruning=False,
            mixed_weights_ty=False,
            indices_dtype=indices_dtype,
            output_dtype=SparseType.INT4,
        )

    @unittest.skipIf(*gpu_unavailable)
    @given(
        nbit_weights_ty=st.sampled_from(
            [
                SparseType.INT8,
            ]
        ),
        pooling_mode=st.sampled_from([PoolingMode.NONE]),
        output_dtype=st.sampled_from([SparseType.BF16, SparseType.FP16]),
        D=st.sampled_from([32, 256, 384, 512, 1024]),
        B=st.integers(min_value=8, max_value=32),
        T=st.integers(min_value=10, max_value=20),
        L=st.integers(min_value=10, max_value=100),
        MAXH=st.integers(min_value=50, max_value=100),
    )
    @settings(
        verbosity=VERBOSITY,
        max_examples=MAX_EXAMPLES_LONG_RUNNING,
        deadline=None,
    )
    def test_nbit_forward_cpu_gpu_dequantize_parity(
        self,
        nbit_weights_ty: SparseType,
        pooling_mode: PoolingMode,
        output_dtype: SparseType,
        D: int,
        B: int,
        T: int,
        L: int,
        MAXH: int,
    ) -> None:
        D_alignment = (
            1
            if nbit_weights_ty.bit_rate() % 8 == 0
            else int(8 / nbit_weights_ty.bit_rate())
        )
        D = round_up(D, D_alignment)
        T_H = [np.random.randint(low=1, high=MAXH + 1) for _ in range(T)]
        quant_cc = IntNBitTableBatchedEmbeddingBagsCodegen(
            embedding_specs=[
                (
                    "",
                    H,
                    D,
                    nbit_weights_ty,
                    EmbeddingLocation.HOST,
                )
                for H in T_H
            ],
            pooling_mode=pooling_mode,
            device="cpu",
            output_dtype=nbit_weights_ty,
        )
        # Initialize the random weights for int nbit table split embedding bag
        quant_cc.fill_random_weights()
        dequant_cc = IntNBitTableBatchedEmbeddingBagsCodegen(
            embedding_specs=[
                (
                    "",
                    H,
                    D,
                    nbit_weights_ty,
                    EmbeddingLocation.HOST,
                )
                for H in T_H
            ],
            pooling_mode=pooling_mode,
            device="cpu",
            output_dtype=output_dtype,
        )
        dequant_cc.fill_random_weights()
        split_weights = quant_cc.split_embedding_weights()
        ref_split_weights = dequant_cc.split_embedding_weights()
        for t in range(T):
            (weights, scale_shift) = split_weights[t]
            (ref_weights, ref_scale_shift) = ref_split_weights[t]
            self.assertEqual(weights.size(), ref_weights.size())
            element_size = SparseType.INT8.bit_rate() / 8.0
            rand_tensor = torch.rand(
                ref_weights.shape[0], int(ref_weights.shape[1] / element_size)
            )
            rand_weights, rand_scale_shift = quantize_embs(
                rand_tensor,
                SparseType.INT8,
            )
            ref_weights.copy_(rand_weights)
            weights.copy_(ref_weights)
            if rand_scale_shift is not None:
                self.assertIsNotNone(scale_shift)
                self.assertIsNotNone(ref_scale_shift)
                ref_scale_shift.copy_(rand_scale_shift)
                scale_shift.copy_(ref_scale_shift)

        lengths_list = [
            torch.randint(
                1,
                L + 1,
                (B,),
            )
            for _ in range(T)
        ]
        indices_list = [
            torch.randint(0, H, (int(length.sum().item()),))
            for length, H in zip(lengths_list, T_H)
        ]
        indices = torch.cat(indices_list, 0)
        lengths = torch.cat(lengths_list, 0)
        offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(lengths)
        quant_cc_output = quant_cc(indices.int(), offsets.int())
        dequant_cc_output = dequant_cc(indices.int(), offsets.int())
        cuda_device = torch.device("cuda")
        dequant_output_from_quant_cc = (
            torch.ops.fbgemm.Fused8BitRowwiseQuantizedToFloatOrHalf(
                quant_cc_output.to(cuda_device),
                output_dtype.as_int(),
                quant_padding_float_type=False,
                scale_bias_last=False,
            )
        )
        torch.testing.assert_close(
            dequant_cc_output.cpu(),
            dequant_output_from_quant_cc.cpu(),
            equal_nan=False,
        )


if __name__ == "__main__":
    unittest.main()
