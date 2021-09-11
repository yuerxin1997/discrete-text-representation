import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence, PackedSequence
from src.models.simple_module import PackedSequneceUtil


class VectorQuantizer(nn.Module):
    """
    VQVAE: https://arxiv.org/abs/1711.00937
    Modified from those sources:
        original tensorflow implementation by deepmind
            https://github.com/deepmind/sonnet/blob/master/sonnet/python/modules/nets/vqvae.py
            https://github.com/deepmind/sonnet/blob/master/sonnet/examples/vqvae_example.ipynb
        pytorch reimplementation
            https://github.com/zalandoresearch/pytorch-vq-vae

    Args:
        embedding_dim: integer representing the dimensionality of the tensors in the
            quantized space. Inputs to the modules must be in this format as well.
        num_embeddings: integer, the number of vectors in the quantized space.

    self.training: affects EMA update, be careful with self.train(), self.eval()
    """

    def __init__(self, config, num_embeddings, embedding_dim):
        super(VectorQuantizer, self).__init__()

        # K
        self._num_embeddings = num_embeddings #256
        # D
        self._embedding_dim = embedding_dim #64
        self.commitment_cost = config.vq.commitment_cost
        self.ema = config.vq.use_ema
        # K × D
        self._embedding = nn.Embedding(self._num_embeddings, self._embedding_dim) # 256 * 64

        # initialize embedding
        if config.vq.init == "xavier_uniform":
            nn.init.xavier_uniform_(self._embedding.weight)
        else:
            self._embedding.weight.data.uniform_(-config.vq.init, config.vq.init)

        if self.ema:
            self.ema_init()

    def forward(self, inputs):
        """
        Connects the module to some inputs.
        Args:
            inputs: Tensor, final dimension must be equal to embedding_dim. All other
                leading dimensions will be flattened and treated as a large batch.
        Returns:
            quantize: Tensor containing the quantized version of the input.
            encodings: Tensor containing the discrete encodings, ie which element
                of the quantized space each input element was mapped to.

        inputs: B × T (optional) × D
        """

        # support PackedSequence
        packed_seq_util = PackedSequneceUtil()
        # print("inputs_org",inputs)
        inputs = packed_seq_util.preprocess(inputs) #seq:[batch,64] #word:
        # print("inputs_after",inputs, inputs.size())
        input_shape = inputs.shape  # bsz × decompose × D or (bsz * decompose) × D [batch,64]
        # print("input_shape",input_shape)
        # Flatten input (bsz * decompose) × D
        flat_input = inputs.view(-1, self._embedding_dim) #[batch,64]
        # print("flat_input",flat_input.size())
        # l2 distances between z_e and embedding vectors: (bsz * decompose) × K
        distances = (
            torch.sum(flat_input ** 2, dim=1, keepdim=True)
            + torch.sum(self._embedding.weight ** 2, dim=1)
            - 2 * torch.matmul(flat_input, self._embedding.weight.t())
        )
        part1 = torch.sum(flat_input ** 2, dim=1, keepdim=True)
        part2 = torch.sum(self._embedding.weight ** 2, dim=1)
        p12 = part1+part2
        part3 = 2 * torch.matmul(flat_input, self._embedding.weight.t())
        # print("part1", part1, part1.size())
        # print("part2", part2, part2.size())
        # print("part1+part2", p12, p12.size())
        # print("part3", part3, part3.size())
        # print("distances",distances,distances.size())
        """
        encoding_indices: Tensor containing the discrete encoding indices, i.e.
        which element of the quantized space each input element was mapped to.
        """
        # encoding_indices: bsz * decompose
        min_distances, encoding_indices = torch.min(distances, dim=1) #min_distances = [batch] 代表每一行和dis_embedding最短距离，encoding_indices=[batch]，代表是dis_embedding编号。
        # print("min_distances",min_distances,min_distances.size())
        # print("encoding_indices",encoding_indices,encoding_indices.size())
        # (bsz * decompose) × K
        encodings = F.one_hot(encoding_indices, self._num_embeddings).float()
        # print("encodings",encodings, encodings.size())
        # Quantize and unflatten
        quantized = self._embedding(encoding_indices).view(input_shape) #[batch,64]
        # print("quantized",quantized,quantized.size()) 
        # straight through gradient
        quantized_st = inputs + (quantized - inputs).detach()
        # print("quantized_st",quantized_st,quantized_st.size())
        # for EMA, only update embedding when training
        if self.ema and self.training:
            self.ema_update(encodings, flat_input)
        
        # calculate loss
        loss_vq = F.mse_loss(quantized, inputs.detach(), reduction="sum")
        loss_commit = F.mse_loss(inputs, quantized.detach(), reduction="sum")

        if self.ema:
            loss = loss_commit * self.commitment_cost
        else:
            loss = loss_commit * self.commitment_cost + loss_vq

        if packed_seq_util.is_packed:
            quantized_st = packed_seq_util.postprocess(quantized_st, pad=0.0)
            encoding_indices = packed_seq_util.postprocess(encoding_indices, pad=-1)
            min_distances = packed_seq_util.postprocess(min_distances, pad=-1)
            # print("quantized_st",quantized_st,quantized_st.size())
            # print("encoding_indices",encoding_indices,encoding_indices.size())
            # print("min_distances",min_distances,min_distances.size())
        else:
            encoding_indices = encoding_indices.contiguous().view(input_shape[:-1])
            min_distances = min_distances.contiguous().view(input_shape[:-1])

        output_dict = {
            "quantized": quantized_st,
            "loss": loss,
            "encoding_indices": encoding_indices,
            "min_distances": min_distances,
            "loss_commit": loss_commit,
        }
        # exit()
        return output_dict

    def ema_init(self):
        self._decay = config.vq.ema.decay
        self._epsilon = config.vq.ema.epsilon
        # K
        self.register_buffer("_ema_cluster_size", torch.zeros(self._num_embeddings))
        # (K, D)
        self.register_buffer(
            "_ema_w", torch.Tensor(self._num_embeddings, self._embedding_dim)
        )
        self._ema_w.data = self._embedding.weight.clone()

    def ema_update(self, encodings, flat_input):
        with torch.no_grad():
            # N moving average
            self._ema_cluster_size = self._ema_cluster_size * self._decay + (
                1 - self._decay
            ) * torch.sum(encodings, 0)

            # additive smoothing to avoid zero count
            n = torch.sum(self._ema_cluster_size.data)
            self._ema_cluster_size = (
                (self._ema_cluster_size + self._epsilon)
                / (n + self._num_embeddings * self._epsilon)
                * n
            )

            # m moving average
            dw = torch.matmul(encodings.t(), flat_input)
            self._ema_w = self._ema_w * self._decay + (1 - self._decay) * dw

            # e update
            self._embedding.weight.data.copy_(
                self._ema_w / self._ema_cluster_size.unsqueeze(1)
            )


class DVQ(nn.Module):
    def __init__(
        self, config, num_embeddings, embedding_dim, split=2, decompose_option="slice"
    ):
        super().__init__()

        self.K = num_embeddings #256
        self.D = embedding_dim #64
        self.M = split # 4
        self.decompose_option = decompose_option

        if self.decompose_option == "project":
            self.projection = nn.Linear(embedding_dim * split, embedding_dim * split)

        self.vq_layers = nn.ModuleList(
            [
                VectorQuantizer(config, num_embeddings=self.K, embedding_dim=self.D)
                for _ in range(self.M)
            ]
        )

    def decompose(self, inp, option="slice"):
        # each slice: B × T (optional) × D

        # support PackedSequence
        is_packed = isinstance(inp, PackedSequence)
        if is_packed:
            inp, *pack_shape = inp

        if option == "project":
            inp = self.projection(inp)
        elif option == "slice":
            pass

        slices = inp.split(self.D, dim=-1)

        if is_packed:
            slices = [PackedSequence(i, *pack_shape) for i in slices]

        return slices

    def forward(self, inputs):
        """
        inputs: B × T (optional) × (M * D)
        """
        # print("inputs",inputs) #[batch,256]
        slices = self.decompose(inputs, self.decompose_option) #这里slices变成4了 就是看几个离散向量代表一个句子/单词
        # print("slices",slices)
        
        # apply vq to each slice separately
        vq_out_list = []
        for slice, vq_layer in zip(slices, self.vq_layers):
            vq_out = vq_layer(slice)  #每一层是一个输出,一共层 (1,batch,64) (2,batch,64) (3,batch,64) (4,batch,64)
            vq_out_list.append(vq_out) 
        # print("vq_out",vq_out)
        # print("vq_out_list",len(vq_out_list))
        # aggregate results
        aggregate_out = {}
        keys = vq_out_list[0].keys()
        for k in keys:
            aggregate_out[k] = []
            for vq_out in vq_out_list:
                aggregate_out[k].append(vq_out[k])
        # print("aggregate_out",aggregate_out)
        
        
        # combine by concatenation
        quantized = torch.cat(aggregate_out["quantized"], dim=-1)
        # sum losses
        loss = torch.stack(aggregate_out["loss"]).sum()

        # just for logging purpose
        loss_commit = torch.stack(aggregate_out["loss_commit"]).sum()
        encoding_indices = torch.stack(aggregate_out["encoding_indices"], dim=-1)

        # combine by stacking, can do sum or mean later on
        quantized_stack = torch.stack(aggregate_out["quantized"], dim=-2) #把M个向量聚合起来（1，batch，64）+（1，batch，64）.。=（M，batch，64）
        # print("quantized_stack",quantized_stack)
        output_dict = {
            # B × T (optional) × (M * D)
            "quantized": quantized,
            # B × T (optional) × M × D
            "quantized_stack": quantized_stack,
            # B × T (optional) × M
            "encoding_indices": encoding_indices,
            "loss": loss,
            "loss_commit": loss_commit.detach(),
            "min_distances": None,
        }

        return output_dict
