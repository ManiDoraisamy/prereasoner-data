"""Train the structural rank head on execution-labeled Spider-train pools.

Pairwise ranking over each pool's top-K prefix — exactly the window ``RankHead.rerank``
reorders in serving, vectorized by the ONE shared ``engine.sql_rank.rank_head_vector``.
Only head weights train; the encoder, searcher, and named features are frozen inputs.
Split is by db_id so validation databases are never seen in training. Standardization is
folded into the first linear layer at save time, so the artifact is a pure MLP and serving
needs no preprocessing state.

Outputs (into the experiment directory): rank_head.pt + metrics.json. Promotion into
engine/data/ is a separate, explicit, user-approved action.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine.sql_rank import (
    RANK_HEAD_EVIDENCE_PREFIXES,
    build_rank_head_model,
    canonical_feature_name,
    rank_head_vector,
)

SEED = 7


def load_pools(path, top_k):
    """Return (feature_names, pools). Each pool: list of (vector-primitives, strict|None)."""
    records = []
    names = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if "idx" not in record or not record.get("candidates"):
                continue
            prefix = record["candidates"][:top_k]
            if len(prefix) < 2:
                continue
            for candidate in prefix:
                names.update(canonical_feature_name(name) for name in candidate["features"])
            records.append((record["db_id"], prefix))
    return tuple(sorted(names)), records


def pool_vectors(prefix, feature_names):
    best = max(candidate["score"] for candidate in prefix)
    mean = sum(candidate["score"] for candidate in prefix) / len(prefix)
    vectors, labels = [], []
    for rank, candidate in enumerate(prefix):
        vectors.append(rank_head_vector(
            candidate["score"], rank, candidate["features"], candidate["evidence"],
            best, mean, feature_names,
        ))
        labels.append(candidate.get("strict"))  # None when execution failed (unlabeled)
    return vectors, labels


def is_validation(db_id, val_dbs=None):
    if val_dbs is not None:
        return db_id in val_dbs
    return int(hashlib.md5(db_id.encode()).hexdigest(), 16) % 10 == 0


def pairwise_auc(scores, pools):
    wins = total = 0
    for start, labels in pools:
        for i, a in enumerate(labels):
            for j, b in enumerate(labels):
                if a is True and b is False:
                    total += 1
                    if scores[start + i] > scores[start + j]:
                        wins += 1
                    elif scores[start + i] == scores[start + j]:
                        wins += 0.5
    return wins / total if total else None


def top1_strict(scores, pools, margin=0.0):
    """Serving-faithful conversion: the head's pick applies only when it clears the
    deterministic top's score by `margin` (mirrors RankHead.rerank exactly)."""
    hits = deterministic = n = 0
    for start, labels in pools:
        n += 1
        deterministic += labels[0] is True
        best = max(range(len(labels)), key=lambda k: (scores[start + k], -k))
        chosen = best if scores[start + best] - scores[start] >= margin else 0
        hits += labels[chosen] is True
    return hits, deterministic, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--hidden", type=int, default=32)
    ap.add_argument("--epochs", type=int, default=300)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--val-dbs", default="",
                    help="comma-separated db_ids for validation; default = deterministic "
                         "md5 bucket (~10%% of dbs)")
    args = ap.parse_args()
    val_dbs = frozenset(filter(None, args.val_dbs.split(","))) or None

    import torch

    torch.manual_seed(SEED)
    feature_names, records = load_pools(args.labels, args.top_k)
    print(f"pools: {len(records)}   named features: {len(feature_names)}")

    all_vectors, splits = [], {"train": [], "val": []}
    for db_id, prefix in records:
        vectors, labels = pool_vectors(prefix, feature_names)
        if not any(label is True for label in labels) or not any(label is False for label in labels):
            usable_for_pairs = False
        else:
            usable_for_pairs = True
        start = len(all_vectors)
        all_vectors.extend(vectors)
        split = "val" if is_validation(db_id, val_dbs) else "train"
        # pair-less pools still count in top-1 conversion; they just contribute no loss pairs
        splits[split].append((start, labels, usable_for_pairs))

    X = torch.tensor(all_vectors, dtype=torch.float32)
    mean, std = X.mean(dim=0), X.std(dim=0).clamp_min(1e-6)
    Xn = (X - mean) / std

    def pairs_of(split):
        out = []
        for start, labels, usable in splits[split]:
            if not usable:
                continue
            positives = [start + i for i, label in enumerate(labels) if label is True]
            negatives = [start + i for i, label in enumerate(labels) if label is False]
            out.extend((p, n) for p in positives for n in negatives)
        return out

    train_pairs = pairs_of("train")
    val_pools = [(start, labels) for start, labels, _ in splits["val"]]
    train_pools = [(start, labels) for start, labels, _ in splits["train"]]
    print(f"train pairs: {len(train_pairs)}   val pools: {len(val_pools)}")
    if not train_pairs or not val_pools:
        sys.exit("not enough labeled pools to train")

    model = build_rank_head_model(Xn.shape[1], args.hidden)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    pos_index = torch.tensor([p for p, _ in train_pairs])
    neg_index = torch.tensor([n for _, n in train_pairs])

    best = {"auc": -1.0, "state": None, "epoch": -1}
    for epoch in range(args.epochs):
        model.train()
        optimizer.zero_grad()
        scores = model(Xn).squeeze(1)
        loss = torch.nn.functional.softplus(scores[neg_index] - scores[pos_index]).mean()
        loss.backward()
        optimizer.step()
        if (epoch + 1) % 10 == 0 or epoch == args.epochs - 1:
            model.eval()
            with torch.no_grad():
                s = model(Xn).squeeze(1).tolist()
            auc = pairwise_auc(s, val_pools)
            if auc is not None and auc > best["auc"]:
                best = {"auc": auc,
                        "state": {k: v.clone() for k, v in model.state_dict().items()},
                        "epoch": epoch + 1}
            print(f"epoch {epoch + 1:4d}  loss {loss.item():.4f}  val AUC {auc}")

    model.load_state_dict(best["state"])
    model.eval()
    # Fold standardization into the first layer: W'x + b' == W((x - mean)/std) + b.
    with torch.no_grad():
        first = model[0]
        first.weight /= std
        first.bias -= first.weight @ mean
        s = model(X).squeeze(1).tolist()
    # Confidence-margin sweep on val: the head's order applies only when it clears the
    # deterministic top by the margin. Ties prefer the larger margin (conservative).
    margin_grid = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0)
    margin, val_hits = max(
        ((m, top1_strict(s, val_pools, m)[0]) for m in margin_grid),
        key=lambda item: (item[1], item[0]),
    )
    _, val_prior, val_n = top1_strict(s, val_pools, margin)
    train_hits, train_prior, train_n = top1_strict(s, train_pools, margin)
    metrics = {
        "seed": SEED, "top_k": args.top_k, "hidden": args.hidden,
        "epochs": args.epochs, "lr": args.lr, "best_epoch": best["epoch"],
        "feature_count": len(feature_names),
        "margin": margin,
        "val_pairwise_auc": round(best["auc"], 4),
        "val_top1_strict": {"head": val_hits, "deterministic": val_prior, "pools": val_n},
        "val_top1_margin0": top1_strict(s, val_pools, 0.0)[0],
        "train_top1_strict": {"head": train_hits, "deterministic": train_prior, "pools": train_n},
    }
    os.makedirs(args.out_dir, exist_ok=True)
    torch.save({
        "feature_names": list(feature_names),
        "input_size": Xn.shape[1],
        "top_k": args.top_k,
        "hidden": args.hidden,
        "margin": margin,
        "evidence_prefixes": list(RANK_HEAD_EVIDENCE_PREFIXES),
        "state_dict": model.state_dict(),
    }, os.path.join(args.out_dir, "rank_head.pt"))
    with open(os.path.join(args.out_dir, "metrics.json"), "w", encoding="utf-8") as handle:
        json.dump(metrics, handle, indent=2)
    print(json.dumps(metrics, indent=2))
    print(f"wrote {args.out_dir}/rank_head.pt")


if __name__ == "__main__":
    main()
