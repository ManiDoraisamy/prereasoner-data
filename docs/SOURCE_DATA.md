# Source Data Catalog

This is the canonical inventory of externally published data. PostgreSQL schemas are named
for publishers or publisher products, never for broad domains. `healthcare`, `finance`,
`tax`, `food`, and `assessment` are planner roles, not data owners.

Every active source schema has its own `release` table. A synchronizer stages immutable
rows, verifies all declared table counts, and switches the active release in one transaction.
`completeness` always refers to the stated `import_scope`; it never means that every file a
publisher has ever released was copied.

## Relationship To Schema.org And Training

Schema.org owns the semantic vocabulary; the sources below own observations and facts. Training
adapters may project a source relation into Schema.org classes and properties, but that projection
does not transfer factual authority to Schema.org or to the model. Wikidata is treated the same way:
its P-ids and entity types are mapped into the ontology, and its QIDs provide cross-source identity,
but it is not the primary semantic schema.

The training projection is read-only. It does not copy mutable source values into serving artifacts.
At answer time, dated rates, code assignments, holidays, medical classifications, and similar facts
must still be read from a pinned source release under that source's terms and planner policy.

## Active Sources

The following releases were active in the `world` database after the 2026-08-17 sync.
Data tables exclude the one-row `release` ledger.

| Schema | Publisher/source | Active scope | Data tables and rows |
|---|---|---|---|
| `iana` | IANA Time Zone Database 2026c | Full declared tzdb structures: country codes, canonical zones/links, and `zone1970.tab`; no compiled transitions | `country_code` 249; `zone` 341; `zone_alias` 257; `zone_location` 312; `country_zone` 423 |
| `cldr` | Unicode CLDR 48.2 | Full declared territory, currency, unit, preference, and localized-display structures; not every CLDR subsystem | 14 data tables; 243,411 rows total |
| `google_libphonenumber` | Google libphonenumber 9.0.31 | Full `PhoneNumberMetadata.xml` | `territory` 254; `number_pattern` 1,431; `number_format` 761 |
| `geonames` | GeoNames | Full worldwide postal artifact plus the scoped `cities5000` place extract | `postal_code` 1,826,904; `place` 69,628 |
| `ecb` | European Central Bank | Full published euro reference-rate history; EUR is the base | `exchange_rate` 220,107 |
| `ec_tedb` | European Commission Taxes in Europe Database | EU VAT response for 2026-08-17, all supported member-state codes | `response_status` 28; `vat_rate` 1,125; `vat_rate_cn_code` 6,109; `vat_rate_cpa_code` 891 |
| `nager_date` | Nager.Date | Community-maintained public-holiday snapshot for all 204 advertised countries, years 2025-2027 | `country` 204; `holiday` 7,989; `holiday_subdivision` 1,474; `holiday_type` 8,100 |
| `cdc` | CDC/NCHS | Full ICD-10-CM tabular hierarchy effective 2026-04-01 through 2026-09-30 | `icd10cm_code` 46,881 |
| `nlm_cde` | NIH/NLM Common Data Elements Repository | All anonymously retrievable CDE and form documents on 2026-08-17; source rights flags and raw JSON retained | `cde` 22,291; `cde_designation` 34,399; `cde_permissible_value` 71,545; `form` 1,667; `form_element` 33,407 |

`ec_tedb.response_status` distinguishes request coverage, returned rules, and optional
publisher metadata. All 28 requested member-state codes have status rows and their
`returned_rate_count` values sum to 1,125. The SOAP response omitted its optional
`additionalInformation/countries` block, so `metadata_present` is false and both capability
flags are null rather than fabricated false values.

The NLM API advertised 22,308 CDEs but anonymously returned 22,291 through lossless typed
partitions. The release scope records both values. The missing 17 are access-state rows that
the public API counted but did not return, not records discarded by this importer.
Of the 1,667 imported forms, 850 are marked copyrighted and 209 prohibit rendering. Those
flags are part of the table contract, not optional descriptive text.

Live data also confirmed why generic dimensions would be wrong: libphonenumber region `001`
has nine distinct calling codes, 88,181 GeoNames `(country_code, postal_code)` keys have more
than one row, and the TEDB response contains source-effective dates from 2025-01-01 through
2026-07-01 even though the requested situation date was 2026-08-17. Composite keys,
ambiguity handling, and source-effective dates must survive planner integration.

### Data-shape audit

The 2026-08-17 post-sync audit measured the serving-relevant failure shapes directly:

- GeoNames contains 1,080,715 distinct country/postal keys. Its 88,181 duplicate keys cover
  834,370 rows (45.7% of all postal rows); the largest key has 646 distinct place names.
  `accuracy` is absent on 282,764 rows.
- Twelve libphonenumber calling codes map to multiple territories. Code `1` maps to 25;
  codes `44`, `590`, `61`, `212`, and others are also shared.
- CLDR has 262 open-ended tender-currency rows and seven territories with two such rows.
- ECB spans 7,071 published dates and 41 historical quote currencies. Currency coverage
  changes over time, so a fixed complete currency matrix does not exist.
- Nager.Date has 131 country/date keys with multiple holiday rows and 91 with different
  names. Types include Public, Observance, Bank, Optional, School, and Authorities.
- NLM forms contain 714 references to 542 CDE IDs absent from the anonymous CDE snapshot.
  They occur in 43 forms and remain unresolved source references rather than fabricated FKs.

All source constraints are validated and each source has exactly one active release. There
are currently no active-release views. Serving must resolve and pin a release explicitly;
queries must never read a source table without a `release_id` predicate.

Serving status is independent of this physical release ledger. As of 2026-08-17,
`iana.country_code` is the only code-approved logical source dataset, under registry name
`iana_country`; deployments remain off unless their allowlist names it. All other source
definitions are disabled. CLDR currency data is intentionally not activated: code, localized
name, symbol, and fraction facts live in separate locale-aware tables, so a single
`cldr.currency_code` lookup would lose required semantics.

The post-sync audit led to three applied schema-v2 corrections:

1. `ec_tedb.response_status` replaced `country_coverage` and uses nullable capability flags.
2. `cldr.territory_currency.valid_from` and `valid_to` are nullable PostgreSQL `date` columns.
3. GeoNames has one release-scoped `(release_id, country_code, postal_code)` lookup index;
   the old unscoped index was removed.

Each correction is recorded with a checksum in the source's `schema_migration` table. The
active release rows retain their content-addressed IDs and now report `schema_version=2`.

## Licensed Sources

These have implementation-ready importers but no database schema yet. This is deliberate:
absence of credentials must not create an empty schema that looks synchronized.

| Future schema | Source | Requirement | Import contract |
|---|---|---|---|
| `who` | WHO ICD-11 MMS | Register for ICD API credentials and set `WHO_ICD_CLIENT_ID` and `WHO_ICD_CLIENT_SECRET`; content is CC BY-ND 3.0 IGO | Traverse one explicit release/language hierarchy from the official v2 API and retain each source document unchanged in JSON |
| `loinc` | Regenstrief LOINC Complete | Free LOINC account, license acceptance, and a downloaded `Loinc_<version>.zip` | Import terms, answer lists/links, panels and forms, parts, and primary part links; retain every imported CSV row in JSON |

Commands:

```powershell
$env:WHO_ICD_CLIENT_ID = "<registered client id>"
$env:WHO_ICD_CLIENT_SECRET = "<registered client secret>"
python -m db.sync.sources.who.sync --release <WHO release id> --dry-run
python -m db.sync.sources.who.sync --release <WHO release id>

python -m db.sync.sources.loinc.sync --archive C:\path\Loinc_2.82.zip --dry-run
python -m db.sync.sources.loinc.sync --archive C:\path\Loinc_2.82.zip
```

LOINC also contains standardized assessment panels, hierarchy, answer lists, and form
display metadata. `nlm_cde` is independently useful for public research CDEs and forms, but
it does not replace the licensed LOINC distribution.

## Not Yet Materialized

| Proposed schema | Source | Why it is not active |
|---|---|---|
| `openfoodfacts` | Open Food Facts | The public full CSV is currently about 1.28 GB compressed and source quality is contributor-dependent. A streaming, ODbL-compliant full-row contract and production storage budget still need approval. |
| `gleif` | GLEIF LEI Golden Copy | The current complete file is about 3.4 million entities and 499 MB compressed. It remains lower value for the products' SMB, restaurant, school, nonprofit, and private-practice customer base. |

No generic global tax feed exists. `ec_tedb` covers EU VAT and explicitly remains
non-binding; national law is authoritative. Other jurisdictions require their own
publisher-named schemas and effective-date/category/rounding contracts. VIES validates EU
VAT registrations and is not a tax-rate source.

No single official global holiday authority exists either. `nager_date` is useful
community data, so serving must expose its provenance and bounded year range. A jurisdiction's
official calendar can supersede it only through a separate source-owned contract.

## Sync Commands

Install ingestion dependencies with `pip install -r db/sync/requirements.txt`, set the
`KB_PG_*` variables, and run:

```powershell
python -m db.sync.sources.iana.sync
python -m db.sync.sources.cldr.sync
python -m db.sync.sources.google_libphonenumber.sync
python -m db.sync.sources.geonames.sync
python -m db.sync.sources.ecb.sync
python -m db.sync.build_exchange_rate       # build the joinable daily cross-rate projection
python -m db.sync.sources.ec_tedb.sync --situation-on 2026-08-17
python -m db.sync.sources.nager_date.sync --year-start 2025 --year-end 2027
python -m db.sync.sources.cdc.sync
python -m db.sync.sources.nlm_cde.sync --version 2026-08-17

# Apply checked-in migrations to source schemas that already exist. This downloads no data.
python -m db.sync.migrations
```

Use each command's local archive or snapshot option when reproducing an exact downloaded
artifact. `--dry-run` always downloads or reads, parses, validates, and hashes without
creating a schema.

## Correctness Boundaries

- libphonenumber describes possible number patterns and formatting. It does not prove that
  a number is allocated, reachable, or owned by a person.
- GeoNames postal coverage and accuracy vary by country. Duplicate postal codes and place
  names are retained as source rows; serving must use country and locality context.
- ECB rates are EUR reference rates for information, normally published on working days.
  They are not transaction rates.
- TEDB data is category- and date-sensitive and legally non-binding. A bare country plus
  percentage is not a complete tax rule.
- Nager.Date is community-maintained. It is not a substitute for legal advice on business
  closures, observances, or employee entitlements.
- ICD and LOINC are terminology references. They do not diagnose a patient, authorize PHI
  egress, or make clinical decisions.
- NLM form records retain `is_copyrighted` and `no_render_allowed`; availability in the API
  is not blanket permission to render or redistribute every assessment.

The ECB source history is not itself the serving projection: run `db.sync.build_exchange_rate`
after the source sync to materialize the joinable daily `knowledgebase."exchange_rate"`
columns. That builder reads the ledger-selected active ECB release, carries rates across
non-publishing calendar days without extending retired series, stores `source_release_id`,
and supplies the production currency-conversion world join. This is a curated derived
projection, not direct planner access to arbitrary `ecb` rows.

Other publisher tables remain planner-invisible unless a code-approved registry definition,
deployment allowlist, grants, eligibility evidence, bounded request-local materialization,
and release evaluation all agree. At this revision, `iana_country` is the only such
source-registry dataset approved for activation; ECB conversion is the explicit derived-world
exception described above.
