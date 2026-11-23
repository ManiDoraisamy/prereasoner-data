# Spider calibration split

Every question of 20 of the 140 `train_spider` databases (1,053 questions), drawn with seed 20261019 by the readers'
database split (`training/readers/splits.py`, deleted with the readers in 09b555b4; databases were split, never
questions). It is the gold-blind measure `spider/results/RESULTS.md` reports as "Spider calibration": `whole_db`,
served, fresh.

`databases.json` names the 20 databases and the digests of the split. Spider's questions, gold SQL and schemas are not
redistributed here (the repository keeps only aggregate Spider measurements); `fetch_data --calibration` writes
`dev.json` and `tables.json` (gitignored) from the official release and checks them against those digests.

```
python -m spider.probe.fetch_data --calibration       # train_spider, the databases, and this split
python -m spider.probe.full_eval --data spider/calibration --dbs spider/data/dbs --config whole_db --selection served --tag <name> --out <folder>
```

Spider (Yu et al., 2018) is CC BY-SA 4.0.
