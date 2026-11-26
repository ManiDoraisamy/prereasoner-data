# regress/sets: the evaluation's own inputs and records

Everything an evaluation of this repository reads, other than public datasets and public model weights, is here, so a
clone reproduces every measurement the repository reports (`spider/results/RESULTS.md`) without this project's
machines. Public datasets are fetched from their pinned sources and checked against the digests in `sources.json`:

```
python -m regress.eval_sets fetch          # WikiTableQuestions 1.0.2, SQUALL, DataBench into PREREASONER_DATASETS
python -m spider.probe.fetch_data --include-train   # Spider's databases (the calibration split's are train databases)
python -m engine.fetch_weights             # the runtime weights, pinned by engine/data/weights_manifest.json
```

The loaders, graders and the call store are `regress/eval_sets.py`; the dev replay is `regress/run_dev_decisions.py`;
the public benchmark and the training find set are `regress/run_public.py`.

| Folder | What it is | Source and license |
|---|---|---|
| `fresh_dev/` | Our 176 development questions on 44 public US government workbooks (52 files in `workbooks/`, as downloaded); `questions.jsonl` with each gold, `manifest.json` (method, sources), `scripts/` (the pipeline that built it, and `common.py`, its normalizers the grader uses), `work/` (its intermediate files; `same_publication_as_frozen.json` lists the workbooks the dev set leaves out) | Workbooks: US government open data (public domain or open-portal terms, per `manifest.json`). Questions and golds: this project |
| `fresh_frozen/` | The frozen twin: questions on other public US government workbooks, run once at a launch gate and never read for development (`verification.json`, the owner's review in `review.csv`/`review.json`) | As above |
| `dev_decisions/reference/` | The dev replay's reference: every dev and demo question's record in the last kept measurement (listed answers, their grades, the choice probabilities, the served label, the calls it made) | This project |
| `dev_decisions/records/` | The dev replay records a results entry compares (by tag) | This project |
| `demo_maps/` | The request databases production built for the demo datasets on 2026-11-18 (`<dataset>.main.sqlite`, `.kb.sqlite`) and the links it recorded (`<dataset>.json`): the demo gate's inputs offline | Demo data: `web/public/dataset/`; knowledgebase rows: Wikidata (CC0), ECB rates |
| `calls/` | The Decisions API's answers by request-body hash (`engine.llm.decision_hash`), one batch per recording: probabilities over options the engine built, no text | This project's API spend |
| `public/samples/` | The public benchmark's samples of WikiTableQuestions test ids (seed, size, the samples each leaves out) | Ids of WikiTableQuestions (CC BY-SA 4.0) |
| `public/records/<run>/<set>/<system>.jsonl.gz` | Every public run's per-question records (engine, served, Gemini 4 Neon, Claude Opus 5.5; `judge-<model>-<thinking>`, the judge test's picks over the engine's listings, one file per judge) | Questions quoted from WikiTableQuestions (CC BY-SA 4.0) and DataBench (MIT) |
| `public/calls/<system>/` | The written queries' answers by request-body hash: the retry's (Gemini 4 Neon at low thinking), and the frontier systems'; `judge/`, the judges' one-key answers to the choice's closed question: the served second judge's on a public run (`engine/llm.py` `judge`), and every judge's of the judge test (`regress/run_public.py --judge`) | This project's API spend |
| `retry_calls/` | The dev replay's retry answers (`--retry`), when recorded | This project's API spend |
| `judge_calls/` | The dev replay's second-judge answers (a run without `--unjudged`), when recorded | This project's API spend |

A kept change to the Decisions path re-measures every dev and demo question with
`python -m regress.run_dev_decisions --record --keep` (three processes, as its docstring says), packs the new calls
(`python -m regress.eval_sets pack`) and commits the references and calls with the change, so the next replay is at
parity from a clone. The frozen splits (`fresh_frozen/`, WikiTableQuestions' and DataBench's test splits outside the
committed samples) are read only by the evaluation that names them.
