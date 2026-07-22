#!/usr/bin/env python3
"""Compute per-property Youden-J thresholds for the property model + save props_thr.json to engine/data +
the DATA dir. The consensus router uses these to binarize property firing (fraction of distinctive props firing).

  Stage 5 of the schema.org-property pipeline (see training/props/pipeline.md). Reuses the gen20 encoder/model/harness
  from training.lib + training.train. Reads the Stage-3 trainer outputs (encoder_props*.pt, qwen_lora_props/) and the
  Stage-2 held-out corpus (units_test.jsonl) straight from the DATA dir.
  in:  training/props/data/{encoder_props_meta.pt, encoder_props.pt, qwen_lora_props/, units_test.jsonl}
  out: training/props/data/props_thr.json + engine/data/props_thr.json
"""
import json, os
import numpy as np, torch

from training.lib.edges import fam_dims_map
from training.lib.relblock import RelBlockModel   # gen20's Runtime11Model, renamed
from training.train.train_multitask import load
from training.train.train_unified import pack_csv, evaluate
from training.lib.encoder import LiveQwen

HERE = os.path.dirname(os.path.abspath(__file__))              # training/props/
TRAIN_DIR = os.environ.get("PREREASONER_TRAIN_DIR", HERE)
DATA = os.path.join(TRAIN_DIR, "data"); os.makedirs(DATA, exist_ok=True)
REPO = os.path.dirname(os.path.dirname(HERE))                  # repo root (training/props -> training -> repo)
ENGINE_DATA = os.environ.get("PREREASONER_ENGINE_DATA", os.path.join(REPO, "engine", "data"))

R = DATA; dev = torch.device("cpu")
meta = torch.load(os.path.join(R, "encoder_props_meta.pt"), map_location="cpu", weights_only=False)
alloc, cfg = meta["alloc"], meta["cfg"]; nc = alloc["n_content"]
di = {d["name"]: d["dim_id"] for d in alloc["dims"]}; fd = fam_dims_map(alloc)
enc = LiveQwen(dev, warm_lora=os.path.join(R, "qwen_lora_props"), serving=True)
m = RelBlockModel(in_dim=cfg["in_dim"], H=cfg["H"], layers=cfg["layers"], heads=cfg["heads"],
                  nc=nc, n_edge=cfg["n_edge"]).to(dev)
m.load_state_dict(torch.load(os.path.join(R, "encoder_props.pt"), map_location="cpu")); m.eval()
test = [p for p in (pack_csv(t, di, fd, nc) for t in load(os.path.join(R, "units_test.jsonl"))) if p]
ps, pl = evaluate(enc, m, test, nc, enc.hdim, dev)
thr = {}
for d in alloc["dims"]:
    if d["family"] != "taxonomy":
        continue
    sc, lb = np.array(ps[d["dim_id"]]), np.array(pl[d["dim_id"]])
    if lb.sum() < 5 or lb.sum() == len(lb):
        thr[d["name"]] = 0.5; continue
    o = np.argsort(-sc); lb = lb[o]; sc = sc[o]; P, N = lb.sum(), len(lb) - lb.sum()
    j = np.cumsum(lb) / P - np.cumsum(1 - lb) / max(N, 1)
    thr[d["name"]] = float(sc[int(np.argmax(j))])
for out in (os.path.join(DATA, "props_thr.json"), os.path.join(ENGINE_DATA, "props_thr.json")):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(thr, open(out, "w"), indent=1)
print(f"props_thr.json: {len(thr)} props | sample: {[(k, round(thr[k],3)) for k in list(thr)[:5]]}")
