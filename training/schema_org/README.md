# Schema.org Named-Dimension Training

This package owns the generalized URI-indexed Schema.org property head, deterministic class
superposition, semantic corpus, calibration, and promotion. Schema.org 30.0 defines the coordinate
names. Wikidata and versioned publisher releases provide observations projected into them.

The promoted head is active in serving. `engine/router.py` maps calibrated servable classes to
coarse resolver families; `engine/knowledge_query.py` still requires deterministic source-key
grounding before a world join. Unsupported classes abstain, and an inherited exact-membership
fallback preserves source-backed coverage when the class model has insufficient evidence.

## Pipeline

From the repository root:

```powershell
python -m training.schema_org.corpus
python -m training.schema_org.train_property_head
python -m training.schema_org.promote <corpus-prefix> --revision <immutable-bundle-commit>
python -m tests.test_schema_coverage
python -m tests.test_schema_decode
```

The current promoted corpus has 48,507 derivation-group-isolated instances. The model trains 80
named properties, validation-qualifies 56, and releases 11 calibrated classes. Machine-readable
counts and identity live in `data/semantic_manifest.json` and
`../../engine/data/schema_training_manifest.json`.

Training never writes into `engine/data/`. Candidates live under
`data/experiments/<corpus-sha>/`; `promote.py` is the only writer of the promoted property head,
model metadata, training manifest, and class signatures. Promotion verifies the candidate,
committed trainer sources, immutable published weight revision, and all artifact hashes before an
atomic install.

GPU runs use `training/tools/run_schema_training.sh` inside a clean checkout. The RunPod wrapper
creates a time-bounded lease and terminates it on success, failure, timeout, interruption, and
process exit. `--keep` is the explicit exceptional behavior. A compatible fingerprinted embedding
cache may be reused; the property-head optimizer itself is deterministic and records its runtime.

Read these next:

- [`../../docs/TRAINING.md`](../../docs/TRAINING.md): complete training architecture and commands
- [`../../docs/MODEL_CARD.md`](../../docs/MODEL_CARD.md): runtime authority and limitations
- [`../../docs/DATA_CARD.md`](../../docs/DATA_CARD.md): corpus composition and leakage controls
- [`../../docs/SOURCE_DATA.md`](../../docs/SOURCE_DATA.md): source ownership and release policy
