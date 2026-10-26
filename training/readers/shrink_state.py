"""Shrink a fine-tuned encoder's state dict for a slow link: rows of the word embedding (the base model's input
embedding, whatever its name: XLM-R's encoder.embeddings.word_embeddings, ModernBERT's encoder.embeddings.tok_embeddings)
that fine-tuning only decayed are
stored as their scale of the base row; changed rows (relative residual above 1e-3), the heads and the norms in full
precision; the rest of the encoder in fp16. expand() rebuilds the state dict from the base model. Leases shrink on
the pod before the download; an expanded model differs from the trained one only by those roundings.

usage: python -m training.readers.shrink_state shrink <model.pt> <base dir or hf id> <out.pt>
       python -m training.readers.shrink_state expand <shrunk.pt> <base dir or hf id> <model.pt>
"""
import sys

import torch
from transformers import AutoModel

def base_state(base):
    """(the base encoder's state dict under the readers' "encoder." prefix, the key of its input embedding)."""
    model = AutoModel.from_pretrained(base)
    embedding = model.get_input_embeddings().weight
    key = next(f"encoder.{name}" for name, parameter in model.named_parameters() if parameter is embedding)
    return {f"encoder.{k}": v for k, v in model.state_dict().items()}, key


def shrink(path, base, out):
    tuned = torch.load(path, map_location="cpu", weights_only=True)
    state, key = base_state(base)
    reference = state[key]
    rows = tuned[key]
    scale = (rows * reference).sum(1) / (reference * reference).sum(1).clamp(min=1e-12)
    residual = (rows - scale[:, None] * reference).norm(dim=1) / reference.norm(dim=1).clamp(min=1e-12)
    changed = torch.nonzero(residual > 1e-3).flatten()
    packed = {"changed_rows": changed, "changed": rows[changed].clone(), "scale": scale.clone(), "rest": {}}
    for name, tensor in tuned.items():
        if name == key:
            continue
        keep_full = not name.startswith("encoder.") or "LayerNorm" in name or tensor.dim() < 2
        packed["rest"][name] = tensor if keep_full else tensor.half()
    torch.save(packed, out)
    print(f"changed rows {len(changed)} of {len(rows)}")


def expand(path, base, out):
    packed = torch.load(path, map_location="cpu", weights_only=True)
    base_states, key = base_state(base)
    rows = packed["scale"][:, None] * base_states[key]
    rows[packed["changed_rows"]] = packed["changed"]
    state = {key: rows}
    for name, tensor in packed["rest"].items():
        state[name] = tensor.float()
    torch.save(state, out)


def main():
    if len(sys.argv) != 5 or sys.argv[1] not in ("shrink", "expand"):
        sys.exit("usage: python -m training.readers.shrink_state shrink|expand <in.pt> <base dir or hf id> <out.pt>")
    {"shrink": shrink, "expand": expand}[sys.argv[1]](*sys.argv[2:5])


if __name__ == "__main__":
    main()
