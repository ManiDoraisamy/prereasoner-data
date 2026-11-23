# Spider calibration split

`dev.json` (1,053 questions) and `tables.json` (their 20 schemas): every question of 20 of the 140 `train_spider`
databases, drawn with seed 20261019 by the readers' database split (`training/readers/splits.py`, deleted with the
readers in 09b555b4; databases were split, never questions). It is the gold-blind measure `spider/results/RESULTS.md`
reports as "Spider calibration": `whole_db`, served, fresh.

```
python -m spider.probe.fetch_data --include-train      # the databases, into spider/data/dbs
python -m spider.probe.full_eval --data spider/calibration --dbs spider/data/dbs --config whole_db --selection served --tag <name> --out <folder>
```

Spider (Yu et al., 2018) is CC BY-SA 4.0; these files are its examples and schemas, unchanged.
