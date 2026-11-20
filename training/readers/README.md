# training/readers

What remains of the readers' training package after the Decisions path replaced the readers as production's own-data
path (`DECISIONS.md`, 2026-11-20):

- `data/experiments/<id>/` (gitignored): every measured run, the readers' and the Decisions path's (the dev replay,
  `regress/run_dev_decisions.py`, reads `decisions-probe-20261118/cache` there). Disposable experiment data, never
  committed and never deleted by a migration.
- `lease.py`: the one GPU lease loop over `training/tools/runpod_api.py` that `training/named/lease.py` builds its jobs
  on.

The readers' pipeline (labels, examples, the two reader trainers, the re-scorer, the refusal and the promotion into
`engine/data/readers`) is in git history before the cut-over.
