# Training Data Card

## Semantic Contract

Schema.org 30.0 supplies 1,521 named properties, 926 classes, inheritance, domains, and ranges. It is
an ontology, not an instance database. Source-owned observations are mapped into that coordinate
system through reviewed adapters. Wikidata supplies public entity identity and many observations,
but it neither defines Schema.org nor overrides publisher-owned facts.

## Promoted Multi-Source Corpus

The authoritative record is `training/schema_org/data/semantic_manifest.json`.

- Corpus SHA-256: `2bef200d6d2733ce686eda1dbca22a2fa55c655d3693825f77e698d889926340`
- Instances: 48,507
- Derivation groups: 33,591
- Train: 39,173
- Validation: 4,745
- Untouched test: 4,589
- Split salt: `schema-org-corpus:v8`
- Ontology: Schema.org 30.0, pinned by source and compiled-contract hashes
- Wikidata observations: 28,884 entity instances plus 2,081 column instances

Publisher observations come from active, release-pinned IANA, Unicode CLDR, Google
libphonenumber, GeoNames, ECB, European Commission TEDB, Nager.Date, CDC/NCHS, and NIH/NLM CDE
tables. Twenty-four serving-shaped product-template instances and 44 class-free demo-upload
instances exercise application domains and abstention without publishing customer rows.

The manifest records exact source counts, release IDs, content hashes, mappings, drop reasons,
split shares, and unavailable credential-gated sources. Generated `semantic_instances.jsonl` is
intentionally ignored; its digest and complete derivation recipe are committed.

## Label Construction

- Wikidata P-ids map to Schema.org property URIs through reviewed bridge mappings.
- Wikidata type QIDs map to Schema.org classes as observation labels, never ontology definitions.
- Publisher tables use explicit source, relation, column, property, and class mappings in
  `training/schema_org/source_adapters.py`.
- A property label is retained only when supporting evidence remains visible after tokenization and
  truncation.
- Wide relations are emitted as bounded facets so a label cannot refer only to hidden columns.
- Mutable values teach relation shapes; runtime answers use pinned source tables instead of weights.

## Leakage Controls

The split key is the parent derivation group, not an individual rendered instance. A source row,
its column forms, row windows, and presentation variants cannot cross train, validation, and test.
The builder rejects:

- a split assignment inconsistent with the deterministic group draw;
- source identities or identical text crossing splits;
- labels without visible evidence;
- duplicate or malformed instance identities; and
- realized split shares outside configured tolerance.

This corpus includes the QID-to-column isolation correction: Wikidata column blocks inherit entity
group splits, and the builder rejects any source identity observed in multiple splits.

## Promoted Coverage

- 80 Schema.org properties have trained coordinates.
- 56 pass validation qualification.
- 11 of 926 classes pass validation-only selection and untouched-test release gates.
- Unsupported properties and classes remain representable and explicitly abstain.

The 11 servable classes are `DefinedTerm`, `ExchangeRateSpecification`, `MedicalCode`, `Movie`,
`MusicRecording`, `Periodical`, `PostalAddress`, `Product`, `PropertyValueSpecification`, `Taxon`,
and `UnitPriceSpecification`. Exact metrics and support are machine-readable in
`engine/data/schema_property_model.json`; this card intentionally does not duplicate rounded metric
tables that could drift.

## Known Limitations And Biases

- The corpus reflects available publisher tables and a capped Wikidata sample, not the distribution
  of every spreadsheet or every Schema.org class.
- Wikidata is the largest source, so its coverage and missing-reference patterns materially shape
  the model.
- Many person-valued and relationship properties are dropped when referenced QIDs are absent from
  the capped snapshot; the manifest records the exact counts.
- Only 80 of 1,521 properties are trained and only 11 of 926 classes are servable.
- Product-template examples are public serving-shaped fixtures, not consented customer-heldout
  evidence.
- Publisher snapshots differ in dates, scope, quality guarantees, and redistribution terms.
- Credential-gated WHO and LOINC data are not in this corpus.

## Historical Encoder Data

`training/props/` built the shared encoder and its historical 71-coordinate compatibility head from
primarily Wikidata-derived inputs plus SQL intent and calculation contrasts. The promoted byte
identity is pinned, but the original run lacks a complete corpus/split manifest and contains two
non-property labels. The generalized Schema.org head is therefore the active class vocabulary;
historical coordinates remain only for compatible intent, ranking, and diagnostic paths.

## Reproduction

Rebuilding requires a synchronized database containing the exact source releases in the manifest:

```powershell
python -m training.schema_org.corpus
python -m training.schema_org.train_property_head
python -m training.schema_org.promote <corpus-prefix> --revision <immutable-bundle-commit>
python -m tests.test_schema_coverage
python -m tests.test_schema_decode
```

Training dependencies are exact-pinned in `training/requirements.txt`. Candidate artifacts stay in
ignored experiment directories; promotion is the only writer of runtime model files.

## Licensing And Privacy

Source terms and attribution are in `THIRD_PARTY.md` and `docs/SOURCE_DATA.md`. Mapping an
observation into Schema.org does not change its upstream license. Public fixtures contain no
customer rows. Consent-bound evaluation metadata, when an operator has it, belongs under ignored
`regress/private/` and must not be published.
