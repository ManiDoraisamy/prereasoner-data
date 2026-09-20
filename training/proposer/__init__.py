"""Proposer pipeline constants: the ONE pinned base-model identity.

Training and inference must load the same immutable snapshot; a mutable hub name would
let the base silently change between an adapter's training and its evaluation.
"""

BASE_MODEL_ID = "Qwen/Qwen2.5-0.5B"
BASE_MODEL_REVISION = "060db6499f32faf8b98477b0a26969ef7d8b9987"
