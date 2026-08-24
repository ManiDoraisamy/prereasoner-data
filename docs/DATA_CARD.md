# Training Data Card

## Semantic Contract

Schema.org 30.0 is the label space: 1,521 properties, 926 classes, inheritance, domains, and
ranges. It is not an instance database. Wikidata and publisher-owned datasets supply observations
that are projected into this vocabulary through explicit mappings.

Wikidata is the largest single observation source in the current semantic corpus and supplies QIDs
for public entity identity. It is not the semantic authority and does not supersede facts owned by
IANA, CLDR, GeoNames, ECB, the European Commission, CDC, NLM, or another publisher.

## Multi-Source Semantic Corpus

The committed identity and provenance record is
`training/schema_org/data/semantic_manifest.json`.

- Corpus SHA-256: `02aff8e50dadc50363cc873b938e9e3c956ee2045f1aed64773be085961c08bd`
- Total instances: 45,772
- Train: 36,496
- Validation: 4,553
- Test: 4,723
- Split unit: derivation group, not individual instance
- Ontology: Schema.org 30.0, pinned by source and compiled-contract hashes
- Largest source: Wikidata, 26,248 instances (about 57% of the corpus)

Publisher observations come from the active releases documented in `SOURCE_DATA.md`: IANA,
Unicode CLDR, Google libphonenumber, GeoNames, ECB, EU TEDB, Nager.Date, CDC/NCHS, and NIH/NLM
CDE. Committed demo uploads supply class-free negatives.

The manifest is authoritative for exact per-source counts, active release IDs, mapping versions,
drop reasons, split shares, and instance kinds. It is committed even though the generated JSONL
corpus is not.

## Label Construction

- Wikidata P-ids map to Schema.org property URIs through reviewed bridge mappings.
- Wikidata type QIDs map to Schema.org classes as corpus labels, not ontology definitions.
- Publisher relations use explicit source, relation, column, property, and class mappings in
  `training/schema_org/source_adapters.py`.
- A property label is retained only when its evidence remains visible in the encoder input after
  tokenization and truncation.
- Wide relations are divided into related facets so labels cannot refer only to truncated columns.
- Mutable values are observations used to teach shapes; they are not packaged as factual answer
  tables in the weights.

## Leakage Controls

An instance ID's parent derivation group determines its split. Whole relations, columns, row
windows, and presentation variants derived from the same source group therefore remain together.
The corpus builder rejects:

- an assigned split that differs from the deterministic group draw;
- identical text in multiple splits;
- labels with no visible supporting value;
- duplicate or malformed instance identities; and
- realized split shares outside the configured tolerance.

## Known Limitations And Biases

- The corpus is source-shaped, not a representative sample of every spreadsheet or every
  Schema.org class.
- Wikidata contributes a majority of instances, so its entity coverage and missing-reference
  patterns materially influence the learned distribution.
- The capped Wikidata snapshot lacks many referenced entities. The manifest records 1,055,959
  dropped unresolvable-QID values; person-valued and other relationship properties are especially
  affected.
- Only 75 of 1,521 Schema.org properties currently have trained head coordinates.
- Only six of 926 classes currently clear serving gates. Representability is not model support.
- Publisher snapshots have different dates, scopes, quality controls, and redistribution terms.
- Demo negatives are synthetic and cannot substitute for consented held-out customer metadata.
- The currently promoted Schema.org head was trained before contributing Wikidata QIDs were
  recorded on column blocks, so entity-to-column split isolation cannot be proven for that artifact.
  The corpus builder now groups column blocks by entity split and rejects any source identity found
  across splits; a rebuilt head is required to close the historical gap.

## Legacy Unified-Router Corpus

The active 71-coordinate column router is trained through `training/props/`, primarily from
Wikidata `capped.entity` observations plus SQL intent and calculation data. Its current published
metadata does not contain a complete corpus hash, source release identity, split manifest, seed,
and held-out metric report. It also contains two coordinates that are not valid Schema.org 30.0
properties. It must not be described as the multi-source corpus model above.

The legacy pipeline also permits entity-name inputs to carry property targets whose values are not
present in the input text. This can teach entity-to-property associations rather than
evidence-visible column semantics. Replacing that supervision is a required gate for an
ontology-clean retrain.

## Reproduction

Rebuilding the semantic corpus requires a synchronized database containing the exact source
releases named by the manifest:

```powershell
python -m training.schema_org.corpus
python -m tests.test_schema_coverage
```

Rebuilding the unified router additionally requires the external inputs listed in
`training/props/pipeline.md` and `docs/TRAINING.md`. Neither generated corpus should be committed as
source. Candidate weights belong only in the ignored experiment directories.

## Licensing And Privacy

Source-specific terms and required attribution are listed in `THIRD_PARTY.md` and
`SOURCE_DATA.md`. Mapping an observation into Schema.org does not change its upstream license.
Credential-gated WHO and LOINC sources are not part of the current corpus.

Public fixtures contain no customer rows. Consent-bound evaluation metadata remains under ignored
`regress/private/` and must not be published without explicit consent.
