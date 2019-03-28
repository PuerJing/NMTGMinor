import onmt
import onmt.modules
import torch.nn as nn
import torch, math

from torch.nn.modules.loss import _Loss
from onmt.modules.Loss import LossFuncBase
from collections import defaultdict


class NMTL2Loss(LossFuncBase):
    """
    Standard NMT Loss Computation.
    """

    def __init__(self, output_size, label_smoothing=0.0, shard_size=1):
        super(NMTL2Loss, self).__init__(output_size)
        self.shard_split = shard_size

        if label_smoothing > 0:
            # When label smoothing is turned on,
            # KL-divergence between q_{smoothed ground truth prob.}(w)
            # and p_{prob. computed by model}(w) is minimized.
            # If label smoothing value is set to zero, the loss
            # is equivalent to NLLLoss or CrossEntropyLoss.
            # All non-true labels are uniformly set to low-confidence.
            self.smoothing_value = label_smoothing / (output_size - 1)
            # ~ self.func = nn.KLDivLoss(size_average=False)
            # ~ one_hot = torch.randn(1, output_size)
            # ~ one_hot.fill_(self.smoothing_value)
            # ~ one_hot[0][self.padding_idx] = 0
            # ~ self.register_buffer('one_hot', one_hot)

        else:
            weight = torch.ones(output_size)
            weight[self.padding_idx] = 0
            self.func = nn.NLLLoss(weight, reduction='sum')
        self.confidence = 1.0 - label_smoothing
        self.label_smoothing = label_smoothing

    def _compute_loss(self, scores, targets, smooth=True):

        gtruth = targets.view(-1)  # batch * time
        scores = scores.view(-1, scores.size(-1))  # batch * time X vocab_size

        if self.confidence < 1:  # label smoothing
            tdata = gtruth

            lprobs = scores
            non_pad_mask = gtruth.ne(self.padding_idx)
            nll_loss = -lprobs.gather(1, gtruth.unsqueeze(1))[non_pad_mask]
            smooth_loss = -lprobs.sum(dim=-1, keepdim=True)[non_pad_mask]
            nll_loss = nll_loss.sum()
            smooth_loss = smooth_loss.sum()

            eps_i = self.smoothing_value
            loss = (1. - self.label_smoothing) * nll_loss + eps_i * smooth_loss
            loss_data = nll_loss.item()

        else:
            loss = self.func(scores.float(), gtruth)
            loss_data = loss.data.item()

        return loss, loss_data

    def forward(self, output_dict, targets, model=None, backward=False,
                tgt_mask=None, normalizer=1, params=None, **kwargs):
        """
        Compute the loss. Subclass must define this method.
        Args:
            output_dict: the predictive output from the model. time x batch x vocab_size
                                                   or time x batch x hidden_size
            targets: the validate target to compare output with. time x batch
            backward
            tgt_mask: for masking the target (saving memory)
            model: pass the model for necessary access to model components
            normalizer: the denomination term of the loss
            params: a dictionary of additional parameters required for computing loss function
            **kwargs(optional): additional info for computing loss.
        """

        outputs = output_dict['hiddens']
        tgt_outputs = output_dict['tgt_hiddens']
        mask = tgt_mask
        # flatten the output
        outputs = outputs.contiguous().view(-1, outputs.size(-1))
        tgt_outputs = tgt_outputs.contiguous().view(-1, tgt_outputs.size(-1))
        targets = targets.view(-1)

        if params is None:
            params = defaultdict(lambda: 0.0)

        if mask is not None:
            """ We remove all positions with PAD 
                to save memory on unwanted positions
            """
            flattened_mask = mask.view(-1)

            non_pad_indices = torch.nonzero(flattened_mask).squeeze(1)

            clean_output_from_src = outputs.index_select(0, non_pad_indices)

            clean_targets = targets.index_select(0, non_pad_indices)

            clean_output_from_tgt = tgt_outputs.index_select(0, non_pad_indices)

        else:
            clean_output_from_src = outputs
            clean_targets = targets
            clean_output_from_tgt = tgt_outputs

        dists_from_src = model.generator(clean_output_from_src) 

        loss, loss_data = self._compute_loss(dists_from_src, clean_targets)

        l2_loss = (clean_output_from_src.float() - clean_output_from_tgt.float()) ** 2
        l2_loss = l2_loss.sum()

        loss = loss + params['l2'] * l2_loss

        if backward:
            loss.div(normalizer).backward()

        output = defaultdict(lambda: None)
        output['loss'] = loss
        output['nll'] = loss_data
        output['l2'] = l2_loss.item()

        return output