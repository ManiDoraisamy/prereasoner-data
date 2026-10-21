"""Convert a stage-2 reading model trained before the share outputs (readers v5, v6) to the current layout
(engine/readers/layout.py): one more per-block column output ("share_part", last of each block) and one more
global output per block ("share", last of each block's group). The new outputs get zero weights and a bias of -20,
so the converted model reads exactly what it read before and never reads a share. Every other weight is copied.

usage: python -m training.readers.relayout <old model.pt> <new model.pt>
"""
import argparse

import torch

from engine.readers.layout import GLOBAL, GLOBAL_WIDTH, PER_BLOCK

NEVER = -20.0


def widen(weight, bias, positions):
    """``weight``/``bias`` with a never-firing output inserted before each of ``positions`` (new indexes)."""
    rows, biases, old = [], [], 0
    width = weight.shape[0] + len(positions)
    for new in range(width):
        if new in positions:
            rows.append(torch.zeros_like(weight[0]))
            biases.append(torch.tensor(NEVER, dtype=bias.dtype))
        else:
            rows.append(weight[old])
            biases.append(bias[old])
            old += 1
    assert old == weight.shape[0]
    return torch.stack(rows), torch.stack(biases)


def relayout(state):
    """The state dict ``state`` (pre-share layout) widened to the current layout, in place."""
    column = state["column_head.weight"]
    assert column.shape[0] == 2 * (PER_BLOCK - 1), column.shape
    state["column_head.weight"], state["column_head.bias"] = widen(column, state["column_head.bias"],
                                                                   {PER_BLOCK - 1, 2 * PER_BLOCK - 1})
    glob = state["global_head.weight"]
    assert glob.shape[0] == GLOBAL_WIDTH - 2, glob.shape
    shares = {GLOBAL["share0"][0], GLOBAL["share1"][0]}
    state["global_head.weight"], state["global_head.bias"] = widen(glob, state["global_head.bias"], shares)
    return state


def main():
    ap = argparse.ArgumentParser(description="Widen a pre-share stage-2 model to the current output layout.")
    ap.add_argument("old")
    ap.add_argument("new")
    args = ap.parse_args()
    torch.save(relayout(torch.load(args.old, map_location="cpu", weights_only=True)), args.new)
    print("converted", args.old, "->", args.new)


if __name__ == "__main__":
    main()
