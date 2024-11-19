import numpy as np
import torch
import torch.nn as nn
import math
import quant_cuda
import torch.nn.functional as F


def round_to_nearest_pole_sim(w, poles):
    """
    w: weight values (1d vector)
    poles: tuple of values

    Round the numbers in w to the nearest value in poles.
    """
    stack = []
    for c in poles:
        diff = (w - c).abs()
        stack.append(diff)
    diff = torch.stack(stack)
    idx = diff.argmin(axis=0)
    aug = 0
    for i, c in enumerate(poles):
        aug += (idx == i) * c
    return aug


# drop-in layer replacement class
class QuantLinearLUT(nn.Module):
    def __init__(
        self,
        bits,
        infeatures,
        outfeatures,
        bias,
        include_sparse=False,
        num_outlier_vals=0,
        num_sensitive_vals=0,
        topX=0,
        balanced=False,
        num_nonzero_per_thread=10,
        sparse_dtype=torch.float32,
    ):
        super().__init__()
        if bits not in [3, 4]:
            raise NotImplementedError("Only 3 and 4 bits is supported.")
        self.infeatures = infeatures
        self.outfeatures = outfeatures
        self.bits = bits

        self.register_buffer(
            "qweight",
            torch.zeros((infeatures // 32 * self.bits, outfeatures), dtype=torch.int32),
        )
        self.register_buffer(
            "dequantized_weight",
            torch.zeros(outfeatures, infeatures, dtype=sparse_dtype)
        )
        if bias:
            self.include_bias = True
            self.register_buffer("bias", torch.zeros(outfeatures, dtype=sparse_dtype))
        else:
            self.include_bias = False
            self.bias = None
        self.register_buffer(
            "lookup_table",
            torch.zeros((outfeatures, 2**self.bits), dtype=sparse_dtype),
        )

        self.include_sparse = include_sparse
        self.numvals = (num_outlier_vals or 0) + (num_sensitive_vals or 0)
        self.topX = topX
        if num_outlier_vals > 0:
            self.register_buffer("outlier_rows", torch.zeros(outfeatures + 1, dtype=torch.int32))
            self.register_buffer("outlier_cols", torch.zeros(num_outlier_vals, dtype=torch.int32))
            self.register_buffer("outlier_vals", torch.randn(num_outlier_vals, dtype=sparse_dtype))

        if num_sensitive_vals > 0:
            self.register_buffer("sensitive_rows", torch.zeros(outfeatures + 1, dtype=torch.int32))
            self.register_buffer("sensitive_cols", torch.zeros(num_sensitive_vals, dtype=torch.int32))

            self.register_buffer("sensitive_vals", torch.randn(num_sensitive_vals, dtype=sparse_dtype))
            self.register_buffer("sensitive_grad_vals", torch.randn(num_sensitive_vals, dtype=sparse_dtype))

            self.register_buffer("sensitive_indices", torch.zeros(2, num_sensitive_vals, dtype=torch.int32))

            # sensitive_grad_vals share the nnz indices as sensitive_vals

        if topX > 0:
            self.register_buffer(
                "full_rows", torch.zeros((infeatures, topX), dtype=torch.float32)
            )
            self.register_buffer(
                "full_row_indices", torch.zeros(topX, dtype=torch.int32)
            )

        self.balanced = balanced

        if include_sparse and balanced and num_outlier_vals > 0:
            print("use num_nonzero_per_thread")
            self.num_threads = int(
                (num_outlier_vals + num_nonzero_per_thread - 1) / num_nonzero_per_thread
            )
            self.num_threads = 128 * math.ceil(
                self.num_threads / 128
            )  # round up to nearest factor of blocksize = 128
            self.register_buffer(
                "startrows", torch.zeros(self.num_threads, dtype=torch.int32)
            )
            print("self.num_threads : ", self.num_threads)

    def pack2(self, linear, lookup_table, include_sparse, num_nonzero_per_thread=-1):
        if self.include_bias:  # linear.bias is not None:
            self.bias = linear.bias.clone()  # todo: check this condition

        # self.lookup_table = lookup_table.float()
        lut, sensitive_weights, sensitive_grads, outlier_weights = lookup_table

        # handle dense matrix
        intweight = linear.weight.data.clone()

        if include_sparse:
            if outlier_weights is not None:
                outlier_weights = outlier_weights.to_dense()
            if sensitive_weights is not None:
                sensitive_weights = sensitive_weights.to_dense()

        # get zero mapping
        num_channels = len(lut)
        for channel in range(num_channels):
            centroid, indices = lut[channel][0]  # last 0 is for group 0
            intweight[channel] = torch.from_numpy(indices)
            self.lookup_table[channel] = torch.from_numpy(centroid)
            self.dequantized_weight[channel] = torch.from_numpy(centroid[indices])

            if include_sparse:
                zero_mapping = round_to_nearest_pole_sim(torch.zeros(1), centroid)
                
                if outlier_weights is not None:
                    nonzero_outlier_vals = torch.nonzero(outlier_weights[channel])
                    outliers_channel = outlier_weights[channel]
                    outliers_channel[nonzero_outlier_vals] -= zero_mapping
                    outlier_weights[channel] = outliers_channel

                if sensitive_weights is not None:
                    nonzero_sensitive_vals = torch.nonzero(sensitive_weights[channel])
                    sensitive_channel = sensitive_weights[channel]
                    sensitive_channel[nonzero_sensitive_vals] -= zero_mapping
                    sensitive_weights[channel] = sensitive_channel
   

        if include_sparse:
            if outlier_weights is not None:
                outlier_weights = outlier_weights.to_sparse(layout=torch.sparse_csr)
                self.register_buffer("outlier_rows", outlier_weights.crow_indices().to(torch.int32))
                self.register_buffer("outlier_cols", outlier_weights.col_indices().to(torch.int32))
                self.register_buffer("outlier_vals", outlier_weights.values().to(torch.float32))

            if sensitive_weights is not None:
                sensitive_weights_nnz_mask = sensitive_weights.view(-1) != 0
                sensitive_weights = sensitive_weights.to_sparse(layout=torch.sparse_csr)
                self.register_buffer("sensitive_rows", sensitive_weights.crow_indices().to(torch.int32))
                self.register_buffer("sensitive_cols", sensitive_weights.col_indices().to(torch.int32))
                self.register_buffer("sensitive_vals", sensitive_weights.values().to(torch.float32))

            if sensitive_grads is not None:
                sensitive_grads = sensitive_grads.to(torch.float32).to_dense()
                sensitive_grad_vals = sensitive_grads.view(-1)[sensitive_weights_nnz_mask].clone()
                assert sensitive_grad_vals.numel() == self.sensitive_vals.numel()
                self.register_buffer("sensitive_grad_vals", sensitive_grad_vals)


            # self.balanced
            if self.balanced:
                raise NotImplementedError()
                # self.numvals = self.vals.shape[0]
                # print("self.numvals: ", self.numvals)
                # print("self.rows: ", self.rows.shape[0])

                # self.num_threads = int(
                #     (self.numvals + num_nonzero_per_thread - 1)
                #     / num_nonzero_per_thread
                # )
                # self.num_threads = 128 * math.ceil(
                #     self.num_threads / 128
                # )  # round up to nearest factor of blocksize = 128

                # nnz_per_thread = int(
                #     (self.numvals + self.num_threads - 1) / self.num_threads
                # )
                # start_rows = torch.zeros(self.num_threads, dtype=torch.int32)

                # print("self.num_threads: ", self.num_threads)
                # print("nnz_per_thread: ", nnz_per_thread)

                # minidx = 0
                # for i in range(0, self.num_threads):
                #     tmpmin = minidx
                #     for j in range(minidx, self.outfeatures):
                #         if nnz_per_thread * i > self.numvals:
                #             start_rows[i] = -1
                #             break
                #         elif self.rows[j] < nnz_per_thread * i:
                #             start_rows[i] = j
                #             tmpmin = j
                #         else:
                #             break
                #     minidx = tmpmin

                # self.register_buffer("startrows", start_rows)

        intweight = intweight.to(torch.int)
        intweight = intweight.t().contiguous()
        intweight = intweight.numpy().astype(np.uint32)
        qweight = np.zeros(
            (intweight.shape[0] // 32 * self.bits, intweight.shape[1]), dtype=np.uint32
        )
        i = 0
        row = 0
        while row < qweight.shape[0]:
            if self.bits in [2, 4, 8]:
                for j in range(i, i + (32 // self.bits)):
                    qweight[row] |= intweight[j] << (self.bits * (j - i))
                i += 32 // self.bits
                row += 1
            elif self.bits == 3:
                for j in range(i, i + 10):
                    qweight[row] |= intweight[j] << (3 * (j - i))
                i += 10
                qweight[row] |= intweight[i] << 30
                row += 1
                qweight[row] |= (intweight[i] >> 2) & 1
                i += 1
                for j in range(i, i + 10):
                    qweight[row] |= intweight[j] << (3 * (j - i) + 1)
                i += 10
                qweight[row] |= intweight[i] << 31
                row += 1
                qweight[row] |= (intweight[i] >> 1) & 0x3
                i += 1
                for j in range(i, i + 10):
                    qweight[row] |= intweight[j] << (3 * (j - i) + 2)
                i += 10
                row += 1
            else:
                raise NotImplementedError("Only 2,3,4,8 bits are supported.")

        qweight = qweight.astype(np.int32)
        self.qweight = torch.from_numpy(qweight)

    def forward(self, x):
        return F.linear(x, self.dequantized_weight, self.bias)
    
    def other_forward(self, x):
        if x.shape[-1] == x.numel():
            outshape = list(x.shape)
            if self.bias is not None:
                y = self.bias.clone()
                outshape[-1] = self.bias.numel()
            else:
                y = torch.zeros((self.outfeatures), device="cuda", dtype=x.dtype)
                outshape[-1] = self.outfeatures
            dtype = x.dtype

            if self.bits == 3:
                if self.include_sparse and self.topX > 0:
                    # quant_cuda.vecquant3matmul_spmv_hybrid_nuq_perchannel(
                    #     self.rows,
                    #     self.cols,
                    #     self.vals,
                    #     x,
                    #     self.full_rows,
                    #     self.full_row_indices,
                    #     y,
                    #     self.outfeatures,
                    #     self.qweight,
                    #     self.lookup_table,
                    # )
                    raise NotImplementedError()
                elif self.include_sparse and self.balanced:
                    # quant_cuda.vecquant3matmul_spmv_balanced_nuq_perchannel(
                    #     self.rows,
                    #     self.cols,
                    #     self.startrows,
                    #     self.vals,
                    #     x,
                    #     y,
                    #     self.qweight,
                    #     self.lookup_table,
                    #     self.outfeatures,
                    #     self.num_threads,
                    #     self.numvals,
                    # )
                    raise NotImplementedError()
                elif self.include_sparse:
                    outlier_sparse_vals = torch.sparse_csr_tensor(
                        self.outlier_rows,
                        self.outlier_cols,
                        self.outlier_vals,
                        size=(self.outfeatures, self.infeatures)
                    )
                    sensitive_sparse_vals = torch.sparse_csr_tensor(
                        self.sensitive_rows,
                        self.sensitive_cols,
                        self.sensitive_vals,
                        size=(self.outfeatures, self.infeatures)
                    )
                    sparse_vals = outlier_sparse_vals + sensitive_sparse_vals

                    quant_cuda.vecquant3matmul_spmv_nuq_perchannel(
                        sparse_vals.crow_indices(),
                        sparse_vals.col_indices(),
                        sparse_vals.values(),
                        x,
                        y,
                        self.outfeatures,
                        self.qweight,
                        self.lookup_table,
                    )
                else:
                    quant_cuda.vecquant3matmul_nuq_perchannel(
                        x, self.qweight, y, self.lookup_table
                    )
            elif self.bits == 4:
                if self.include_sparse and self.topX > 0:
                    # quant_cuda.vecquant4matmul_spmv_hybrid_nuq_perchannel(
                    #     self.rows,
                    #     self.cols,
                    #     self.vals,
                    #     x,
                    #     self.full_rows,
                    #     self.full_row_indices,
                    #     y,
                    #     self.outfeatures,
                    #     self.qweight,
                    #     self.lookup_table,
                    # )
                    raise NotImplementedError()
                elif self.include_sparse and self.balanced:
                    # quant_cuda.vecquant4matmul_spmv_balanced_nuq_perchannel(
                    #     self.rows,
                    #     self.cols,
                    #     self.startrows,
                    #     self.vals,
                    #     x,
                    #     y,
                    #     self.qweight,
                    #     self.lookup_table,
                    #     self.outfeatures,
                    #     self.num_threads,
                    #     self.numvals,
                    # )
                    raise NotImplementedError()
                elif self.include_sparse:
                    outlier_sparse_vals = torch.sparse_csr_tensor(
                        self.outlier_rows,
                        self.outlier_cols,
                        self.outlier_vals,
                        size=(self.outfeatures, self.infeatures)
                    )
                    sensitive_sparse_vals = torch.sparse_csr_tensor(
                        self.sensitive_rows,
                        self.sensitive_cols,
                        self.sensitive_vals,
                        size=(self.outfeatures, self.infeatures)
                    )
                    sparse_vals = outlier_sparse_vals + sensitive_sparse_vals

                    quant_cuda.vecquant4matmul_spmv_nuq_perchannel(
                        sparse_vals.crow_indices(),
                        sparse_vals.col_indices(),
                        sparse_vals.values(),
                        x,
                        y,
                        self.outfeatures,
                        self.qweight,
                        self.lookup_table,
                    )
                else:
                    quant_cuda.vecquant4matmul_nuq_perchannel(
                        x, self.qweight, y, self.lookup_table
                    )

            y = y.to(dtype)
            return y.reshape(outshape)
        else:
            out_shape = x.shape[:-1] + (self.outfeatures,)
            x = x.reshape(-1, x.shape[-1])
            out = torch.zeros(
                (x.shape[0], self.outfeatures), device="cuda", dtype=x.dtype
            )
            dtype = x.dtype
            if self.bits == 3:
                if self.include_sparse and self.topX > 0:
                    # quant_cuda.vecquant3matmul_spmv_hybrid_nuq_perchannel_batched(
                    #     self.rows,
                    #     self.cols,
                    #     self.vals,
                    #     x,
                    #     self.full_rows,
                    #     self.full_row_indices,
                    #     out,
                    #     self.outfeatures,
                    #     self.qweight,
                    #     self.lookup_table,
                    # )
                    raise NotImplementedError()
                elif self.include_sparse:
                    outlier_sparse_vals = torch.sparse_csr_tensor(
                        self.outlier_rows,
                        self.outlier_cols,
                        self.outlier_vals,
                        size=(self.outfeatures, self.infeatures),
                        dtype=torch.float32
                    )
                    sensitive_sparse_vals = torch.sparse_csr_tensor(
                        self.sensitive_rows,
                        self.sensitive_cols,
                        self.sensitive_vals,
                        size=(self.outfeatures, self.infeatures),
                        dtype=torch.float32
                    )
                    sparse_vals = outlier_sparse_vals + sensitive_sparse_vals
                    quant_cuda.vecquant3matmul_spmv_nuq_perchannel_batched(
                        sparse_vals.crow_indices(),
                        sparse_vals.col_indices(),
                        sparse_vals.values(),
                        x,
                        out,
                        self.outfeatures,
                        self.qweight,
                        self.lookup_table,
                    )
                else:
                    quant_cuda.vecquant3matmul_nuq_perchannel_batched(
                        x, self.qweight, out, self.lookup_table
                    )
            elif self.bits == 4:
                if self.include_sparse and self.topX > 0:
                    # quant_cuda.vecquant4matmul_spmv_hybrid_nuq_perchannel_batched(
                    #     self.rows,
                    #     self.cols,
                    #     self.vals,
                    #     x,
                    #     self.full_rows,
                    #     self.full_row_indices,
                    #     out,
                    #     self.outfeatures,
                    #     self.qweight,
                    #     self.lookup_table,
                    # )
                    raise NotImplementedError()
                elif self.include_sparse:
                    outlier_sparse_vals = torch.sparse_csr_tensor(
                        self.outlier_rows,
                        self.outlier_cols,
                        self.outlier_vals,
                        size=(self.outfeatures, self.infeatures),
                        dtype=torch.float32
                    )
                    sensitive_sparse_vals = torch.sparse_csr_tensor(
                        self.sensitive_rows,
                        self.sensitive_cols,
                        self.sensitive_vals,
                        size=(self.outfeatures, self.infeatures),
                        dtype=torch.float32
                    )
                    sparse_vals = outlier_sparse_vals + sensitive_sparse_vals

                    quant_cuda.vecquant4matmul_spmv_nuq_perchannel_batched(
                        sparse_vals.crow_indices(),
                        sparse_vals.col_indices(),
                        sparse_vals.values(),
                        x,
                        out,
                        self.outfeatures,
                        self.qweight,
                        self.lookup_table,
                    )
                else:
                    quant_cuda.vecquant4matmul_nuq_perchannel_batched(
                        x, self.qweight, out, self.lookup_table
                    )
            out = out.to(dtype)
            out = out.reshape(out_shape)
            out = out + self.bias if self.bias is not None else out
            return out


def make_quant_lut(
    module,
    names,
    bits,
    name="",
    include_sparse=False,
    num_outlier_vals=None,
    num_sensitive_vals=None,
    topX=0,
    balanced=False,
    num_nonzero_per_thread=10,
    sparse_dtype=torch.float32,
    fake_quant=False
):
    if isinstance(module, QuantLinearLUT):
        return
    for attr in dir(module):
        tmp = getattr(module, attr)
        name1 = name + "." + attr if name != "" else attr
        if name1 in names:
            if num_outlier_vals is not None:
                num_outlier_val = num_outlier_vals[name1]
            else:
                num_outlier_val = 0

            if num_sensitive_vals is not None:
                num_sensitive_val = num_sensitive_vals[name1]
            else:
                num_sensitive_val = 0

            delattr(module, attr)
            setattr(
                module,
                attr,
                QuantLinearLUT(
                    bits,
                    tmp.in_features,
                    tmp.out_features,
                    tmp.bias is not None,
                    include_sparse=include_sparse,
                    num_outlier_vals=num_outlier_val,
                    num_sensitive_vals=num_sensitive_val,
                    topX=topX,
                    balanced=balanced,
                    num_nonzero_per_thread=num_nonzero_per_thread,
                    sparse_dtype=sparse_dtype
                ),
            )
            if fake_quant:
                module.qweight = None

                module.outlier_rows = None
                module.outlier_cols = None
                module.outlier_vals = None

                # module.sensitive_rows = None
                # module.sensitive_cols = None
                module.sensitive_vals = None
            else:
                module.dequantized_weight = None
                module.sensitive_indices = None


    for name1, child in module.named_children():
        make_quant_lut(
            child,
            names,
            bits,
            name + "." + name1 if name != "" else name1,
            include_sparse=include_sparse,
            num_outlier_vals=num_outlier_vals,
            num_sensitive_vals=num_sensitive_vals,
            topX=topX,
            balanced=balanced,
            num_nonzero_per_thread=num_nonzero_per_thread,
            sparse_dtype=sparse_dtype
        )



def remove_fake_quantization_weight(
    module,
):
    if isinstance(module, QuantLinearLUT):
        module.dequantized_weight = None
        return

    for name1, child in module.named_children():
        remove_fake_quantization_weight(child)