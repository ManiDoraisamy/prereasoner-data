"""
gen11 structural model = gen10's RelBlock encoder with n_edge=10 (adds the cross-table `fk` edge) and
a classmethod to WARM-START from a gen10 checkpoint. proj + every RelBlock weight has identical shape
(H=384 unchanged; nc is only a read-out SLICE, not a weight), so they copy directly; the only growing
parameter is each block's edge-bias `eb` (9 rows -> 10), where we copy the 9 gen10 rows and leave the new
fk edge type at zero so the model starts behaving EXACTLY like gen10 (no spurious cross-table attention
until the JOIN task trains it). alloc_multitask prepends ONE dim (clause_join) so gen10's 214 dims keep the exact
hidden coordinates they were trained on (the `-nc:` read-out just grows by one coordinate at the low end).
"""
from __future__ import annotations
import torch
import torch.nn as nn
from training.lib.edges import N_EDGE


class RelBlock(nn.Module):
    def __init__(self, H, heads, n_edge):
        super().__init__()
        self.h, self.dh = heads, H // heads
        self.q = nn.Linear(H, H); self.k = nn.Linear(H, H); self.v = nn.Linear(H, H); self.o = nn.Linear(H, H)
        self.eb = nn.Parameter(torch.zeros(n_edge, heads))
        self.n1 = nn.LayerNorm(H); self.n2 = nn.LayerNorm(H)
        self.ff = nn.Sequential(nn.Linear(H, 4 * H), nn.GELU(), nn.Linear(4 * H, H))

    def forward(self, x, edges, key_pad):
        B, S, H = x.shape
        xn = self.n1(x)
        q = self.q(xn).view(B, S, self.h, self.dh).transpose(1, 2)
        k = self.k(xn).view(B, S, self.h, self.dh).transpose(1, 2)
        v = self.v(xn).view(B, S, self.h, self.dh).transpose(1, 2)
        att = (q @ k.transpose(-1, -2)) / (self.dh ** 0.5)
        att = att + self.eb[edges].permute(0, 3, 1, 2)
        att = att.masked_fill(key_pad[:, None, None, :], float("-inf"))
        att = att.softmax(-1)
        x = x + self.o((att @ v).transpose(1, 2).reshape(B, S, H))
        x = x + self.ff(self.n2(x))
        return x


class RelBlockModel(nn.Module):
    def __init__(self, in_dim=896, H=384, layers=10, heads=6, nc=215, n_edge=N_EDGE):
        super().__init__()
        self.nc = nc
        self.proj = nn.Linear(in_dim, H)
        self.blocks = nn.ModuleList([RelBlock(H, heads, n_edge) for _ in range(layers)])
        self.cfg = dict(in_dim=in_dim, H=H, layers=layers, heads=heads, nc=nc, n_edge=n_edge)

    def forward(self, x, edges, key_pad):
        h = self.proj(x)
        for b in self.blocks:
            h = b(h, edges, key_pad)
        return {"content": h[:, :, -self.nc:]}

    def forward_layers(self, x, edges, key_pad):
        h = self.proj(x)
        outs = [h[:, :, -self.nc:]]
        for b in self.blocks:
            h = b(h, edges, key_pad)
            outs.append(h[:, :, -self.nc:])
        return outs

    def warm_start(self, sd):
        """Copy a gen10 state_dict in: identical-shape tensors directly; `eb` by row (9->10, fk row=0)."""
        own = self.state_dict()
        copied = grown = 0
        for key, val in sd.items():
            if key not in own:
                continue
            if own[key].shape == val.shape:
                own[key].copy_(val); copied += 1
            elif key.endswith(".eb") and val.shape[0] <= own[key].shape[0] and val.shape[1] == own[key].shape[1]:
                own[key].zero_(); own[key][:val.shape[0]].copy_(val); grown += 1
        self.load_state_dict(own)
        return copied, grown
