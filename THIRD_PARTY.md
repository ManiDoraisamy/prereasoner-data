# Third-Party Models And Data

PreReasoner's source code is licensed under [Apache-2.0](LICENSE). That license does not replace the terms of
models, datasets, hosted services, or Python and JavaScript packages used with the project.

The principal runtime and training inputs are listed below. Schema.org defines the semantic
coordinate system. Wikidata and the publisher datasets provide observations projected into that
system; no source's facts are relicensed merely because they are used for training.

| Component | Use | Upstream terms |
|---|---|---|
| [Qwen2.5-0.5B](https://huggingface.co/Qwen/Qwen2.5-0.5B) | Base encoder for the trained LoRA adapter | Apache-2.0 |
| [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) | Entity-resolution embeddings | MIT |
| [spaCy en_core_web_md](https://spacy.io/models/en#en_core_web_md) | English parsing and entity candidates | MIT; the installed wheel contains its license and source notices |
| [Wikidata](https://www.wikidata.org/wiki/Wikidata:Copyright) | Entity identifiers and the largest current set of property-labelled training observations | Structured data is CC0; other Wikidata content can have different terms |
| [Schema.org](https://schema.org/docs/terms.html) | Versioned ontology shell: named classes, properties, and inheritance used by training and evidence | CC BY-SA 3.0 |

The database synchronizers can ingest additional publisher snapshots. Those snapshots are
not distributed with this repository and remain subject to their publisher terms:

| Source | Imported scope | Terms and required handling |
|---|---|---|
| [IANA Time Zone Database](https://www.iana.org/time-zones/tz-link) | Country and time-zone structures | Public-domain tzdb data; preserve release identity and source notices |
| [Unicode CLDR](https://cldr.unicode.org/index/downloads) | Territory, currency, display-name, and unit structures | Unicode License v3 |
| [Google libphonenumber](https://github.com/google/libphonenumber) | Numbering metadata and formats | Apache-2.0 plus notices shipped by the upstream repository |
| [GeoNames](https://www.geonames.org/) | Postal data and the scoped cities extract | CC BY 4.0; attribution required |
| [European Central Bank](https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html) | EUR reference-rate history | ECB reuse conditions; cite the ECB, preserve accuracy, and identify modifications |
| [European Commission TEDB](https://eur-lex.europa.eu/eli/dec/2011/833/oj/eng/pdf) | Dated EU VAT responses | Commission Decision 2011/833/EU; acknowledge the source and retain third-party exceptions |
| [Nager.Date](https://github.com/nager/Nager.Date) | Bounded public-holiday snapshots | Upstream software is MIT; holiday facts are community-maintained and source provenance must remain visible |
| [CDC/NCHS](https://www.cdc.gov/other/agencymaterials.html) | ICD-10-CM hierarchy | U.S. Government material unless a source-specific notice says otherwise; attribution and non-endorsement requirements apply |
| [NIH/NLM CDE Repository](https://www.nlm.nih.gov/web_policies.html) | CDEs, forms, and form elements | Government works and contributed works differ; retain per-record copyright and render-denial fields |

Credential-gated WHO ICD-11 and LOINC importers do not confer redistribution rights. A user
must accept the publisher terms and supply the source artifact or credentials. The exact
sync scope, quality limits, and rights-bearing fields are documented in
[`docs/SOURCE_DATA.md`](docs/SOURCE_DATA.md).

Spider evaluation data is not distributed by this repository. Follow the dataset owner's terms when downloading it
through the instructions in `docs/SQL_AST.md`.

Python packages installed from the requirement files and browser libraries loaded by the frontend retain their
upstream licenses. Before publishing a model bundle or container image, preserve the notices shipped by those
dependencies and review the exact artifact set being distributed.
