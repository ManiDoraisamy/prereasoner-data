# Schema.org Multi-Source Evidence Training

This directory owns the URI-indexed Schema.org property head and deterministic class signatures.
Schema.org 30.0 defines the semantic coordinates. Wikidata and publisher releases provide
observations projected into those coordinates.

This head is evidence-only in serving. The legacy 9-family model in `engine/router.py` still owns
world-table routing until the measured retirement condition in `DECISIONS.md` is met.

## Pipeline

From the repository root:

```powershell
python -m training.schema_org.corpus
python -m training.schema_org.train_property_head
python -m training.schema_org.promote <corpus-prefix> --revision <immutable-bundle-commit>
python -m tests.test_schema_coverage
python -m tests.test_schema_decode
```

GPU runs use `training/tools/run_schema_training.sh` inside a clean checkout. It verifies the expected
commit before staging ignored corpus, embedding-cache, and LoRA inputs, then exports the four artifacts
that `promote.py` gates. Invoke it through `python -m training.tools.runpod_api lease`; the lease terminates
the pod on success, failure, timeout, and interruption unless an operator explicitly passes `--keep`.

Corpus and candidate files are generated under `training/schema_org/data/` and are gitignored.
`data/semantic_manifest.json` is committed because it records the corpus identity, source releases,
mappings, split counts, and drop statistics.

Training never writes into `engine/data/`. Candidates live under
`data/experiments/<corpus-sha>/`; `promote.py` is the only writer of the promoted property head,
model metadata, and class signatures.

The conceptual architecture, data sources, leakage controls, and release limitations are documented
in:

- [`../../docs/TRAINING.md`](../../docs/TRAINING.md)
- [`../../docs/MODEL_CARD.md`](../../docs/MODEL_CARD.md)
- [`../../docs/DATA_CARD.md`](../../docs/DATA_CARD.md)
- [`../../docs/SOURCE_DATA.md`](../../docs/SOURCE_DATA.md)
