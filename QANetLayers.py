"""Assortment of QA layers for use in models.py.
"""
import math 
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from util import masked_softmax

class position_encoding(nn.Module):
    def __init__(self, n_embd, device, seq_len = 1000):
    # x shape is [batch size, seq_len, n_embd]
        super().__init__()
        pos_encodings = torch.zeros(seq_len, n_embd)
        pos = torch.arange(seq_len).unsqueeze(1)
        val = torch.exp(torch.arange(0, n_embd, 2) * -(math.log(10000.0) / n_embd))

        pos_encodings[:, 0::2] = torch.sin(pos * val)
        pos_encodings[:, 1::2] = torch.cos(pos*val)
        pos_encodings = pos_encodings.unsqueeze(0).to(device) # [1, seq_len, n_embd]

        self.register_buffer('pos_encodings', pos_encodings)
    def forward(self, x):

        return x + Variable(self.pos_encodings[:, :x.shape[1]], 
                            requires_grad = False)



class CausalSelfAttention(nn.Module):
    """
    A vanilla multi-head masked self-attention layer with a projection at the end.
    I believe I could have just used torch.nn.MultiheadAttention but their documentation
    is all but absent and code ugly so I don't trust it, rolling my own here.
    """

    def __init__(self, n_embd, n_head, attn_pdrop, resid_pdrop, block_size):
        super().__init__()
        assert n_embd % n_head == 0
        # key, query, value projections for all heads
        self.key = nn.Linear(n_embd, n_embd)
        self.query = nn.Linear(n_embd, n_embd)
        self.value = nn.Linear(n_embd, n_embd)
        # regularization
        self.attn_drop = nn.Dropout(attn_pdrop)
        self.resid_drop = nn.Dropout(resid_pdrop)
        # output projection
        self.proj = nn.Linear(n_embd, n_embd)
        self.n_head = n_head

    def forward(self, x, mask):
        B, T, C = x.size()

        # calculate query, key, values for all heads in batch and move head forward to be the batch dim
        k = self.key(x).view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        q = self.query(x).view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        v = self.value(x).view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)

        mask=mask.view(B, 1, 1, -1)
        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att.masked_fill(mask == 0, -1e10) # todo: just use float('-inf') instead?
        att = F.softmax(att, dim=-1)
        att = self.attn_drop(att)
        y = att @ v # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side

        # output projection
        y = self.resid_drop(self.proj(y))
        return y

class Block(nn.Module):
    """ an QANet Transformer block with Conv nets"""

    def __init__(self, hidden_size, resid_pdrop, num_convs, device):
        super(Block, self).__init__()

        self.num_convs = num_convs
        self.position_encoder = position_encoding(hidden_size, device)
        self.conv_ln = nn.LayerNorm(hidden_size)
        self.convolution = nn.Sequential(
            nn.Conv1d(in_channels = hidden_size, 
                      out_channels = hidden_size,
                      kernel_size = 7, 
                      groups = hidden_size, 
                      padding = 7//2,
                      bias = False),

            nn.Conv1d(in_channels = hidden_size,
                      out_channels = hidden_size,
                      kernel_size = 1,
                      padding = 0,
                      bias = True),
            nn.ReLU(),
            nn.Dropout(resid_pdrop)
        )

        self.attn_ln = nn.LayerNorm(hidden_size)        
        self.attn = CausalSelfAttention(n_embd = hidden_size, 
                                        n_head = 8, 
                                        attn_pdrop = 0.1,
                                        resid_pdrop = resid_pdrop,
                                        block_size =  128)
        
        self.ff_ln = nn.LayerNorm(hidden_size)
        self.ff_1 = nn.Linear(hidden_size, hidden_size, bias = True)
        self.ff_relu = nn.ReLU()
        self.ff_2 = nn.Linear(hidden_size, hidden_size, bias = True)
        nn.init.xavier_uniform_(self.ff_1.weight)
        nn.init.xavier_uniform_(self.ff_2.weight)
    
    def forward(self, x, mask):
        x = self.position_encoder(x)
        residual = x

        # convolution layers 
        for i in range(self.num_convs):
            x = self.conv_ln(x)
            x = self.convolution(x.transpose(1,2)).transpose(1,2)
            x += residual
            residual = x

        # multihead attn
        x = self.attn_ln(x)
        x = F.dropout(x, p = 0.1, training = self.training)
        x = x + self.attn(x, mask) + residual
        residual = x

        # feedforwards
        x = self.ff_ln(x)
        x = F.dropout(x, p = 0.1, training = self.training)
        x = self.ff_1(x)
        x = self.ff_relu(x)
        x = self.ff_2(x)
        x += residual
        
        return x

class QANetOutput(nn.Module):
    def __init__(self, n_embd):
        super(QANetOutput, self).__init__()
        self.w1 = nn.Linear(n_embd*2, 1, bias = False)
        self.w2 = nn.Linear(n_embd*2, 1, bias = False)
    def forward(self, M1, M2, M3, mask):
        x1 = torch.cat((M1, M2), dim = 2)
        x2 = torch.cat((M2, M3), dim = 2)

        p1 = masked_softmax(self.w1(x1).squeeze(), mask, log_softmax = True)
        p2 = masked_softmax(self.w2(x2).squeeze(), mask, log_softmax = True)

        return p1, p2


# class QANetEnsemble(nn.Module):
#     def __init__(self, list_of_models):
#         super(QANetEnsemble, self).__init__()
#         self.models = list_of_models
#     def 