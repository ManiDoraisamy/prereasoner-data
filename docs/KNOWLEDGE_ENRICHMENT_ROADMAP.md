# Market-Led Semantic and Knowledge-Enrichment Plan

Status: **SOURCE MATERIALIZATION COMPLETE FOR NINE SOURCES; GUARDED M0 INTEGRATION IMPLEMENTED.**
Nine source-owned schemas now hold validated active snapshots: IANA, CLDR, Google
libphonenumber, GeoNames, ECB, EC TEDB, Nager.Date, CDC/NCHS, and NIH/NLM CDE. WHO ICD-11
and LOINC importers are implemented but correctly create no schema without licensed source
access. Exact releases, tables, counts, scope, and limitations live in
[`SOURCE_DATA.md`](SOURCE_DATA.md). The runtime can select and materialize an activated,
eligible snapshot into the existing typed planner. `iana_country` is the first code-approved
logical dataset; a separate deployment allowlist remains empty by default. The legacy
Wikidata rename remains a coordinated migration. This status does not claim production
enrichment accuracy.

`engine/enrichment/` now has one canonical `DatasetDefinition` contract for embedded and
PostgreSQL-backed references, typed source capabilities, cardinality and ambiguity policy,
temporal and row-usage contracts, qualified relations, release/schema/contract snapshot
pins, a bounded read-only `SnapshotStore`, deterministic requested-attribute extraction,
and policy-aware source adapters with typed match/ambiguity/denial outcomes. The canonical
schema graph and SQL AST now preserve tuple foreign keys as one `AND` join. Seventeen
definitions are registered: fifteen are `DISABLED`, the embedded ISO currency fixture is
`EVALUATION`-only, and `iana_country` is `ACTIVE` but still requires a deployment key.
Registration alone is not serving activation. Seven immutable domain profiles, conservative role evidence, compatible-role
dataset gates, request-local materialization, trusted-edge propagation, and complete replay
manifests are implemented and wired behind those activation states. The generated contract
corpus, 35-case public-template development corpus, and IANA activation gate are green.
True opted-in held-out data and application of grants to the production runtime role remain
open; default production answers are unchanged.

Schema.org is pinned to **v30.0 (2026-03-19)**. A vocabulary upgrade is a reviewed
registry change and must update this document, registry hashes, fixtures, and compatibility
tests together.

## 1. Product-market scope

This plan is based on the products' official positioning, template catalogs, Marketplace
listings, and observed customer workflows. Class count is not a delivery metric.

| Product | Primary market | Recurring workflows | Evidence |
|---|---|---|---|
| Formesign | Outpatient healthcare first; then employee compliance, research consent, and workplace safety | patient intake, assessments, consent, signed documents, approval chains, acknowledgements, inspections | [product](https://formesign.com/compliance/), [healthcare listing](https://workspace.google.com/marketplace/app/formesign_hipaa_compliance_for_google_fo/845888525052), [templates](https://formesign.com/intake-forms/intake.html) |
| Neartail | Meal-prep businesses, canteens, restaurants, bakeries, grocery, and small food retail | menus, variants, prices, order lines, payments, inventory, pickup/delivery, production and packing | [order product](https://neartail.com/google-order-forms/), [template catalog](https://neartail.com/order-forms/restaurant.html), [Marketplace listing](https://workspace.google.com/marketplace/app/neartail_order_form/743172720058) |
| Formfacade | Horizontal customer-facing forms for SMBs, education, nonprofits, events, and professional services | leads, inquiries, applications, registrations, assessments, file collection, prefill, lightweight CRM | [product](https://formfacade.com/website/), [Marketplace listing](https://workspace.google.com/marketplace/app/formfacade_embed_in_website/743872305260), [scoring use cases](https://workspace.google.com/marketplace/app/formfacade_assign_points/706030115252) |

The implementation order follows the strongest evidence:

1. shared party, geography, time, currency, unit, and contact semantics;
2. healthcare intake and consent;
3. food ordering and canteens;
4. registrations, bookings, education, events, and memberships;
5. lead and client intake;
6. construction and field-safety workflows.

Entertainment, music, publication, organism, landmark, and software classes remain valid
legacy Wikidata resolver coverage. They are not market requirements for this project and
must not determine enrichment priorities.

## 2. Terms and storage boundaries

Three meanings of "schema" must remain separate:

1. **Schema.org** names interoperable classes and properties.
2. A **PostgreSQL schema** is a storage and ownership namespace.
3. A **dataset schema** is a typed column contract for one materialized source.

There is no active PostgreSQL schema named `wikipedia`; the source is **Wikidata**, not
Wikipedia. The current `public` and `knowledgebase` names are legacy implementation names,
not the target architecture. They obscure source and lifecycle and must not be copied into
new code.

| PostgreSQL schema | Kind | Owner and purpose |
|---|---|---|
| `public` | PostgreSQL infrastructure | Extensions and explicitly reviewed shared SQL objects only; no application tables or imported data |
| `wikidata` | source data | Synchronized Wikidata entities, resolver index, type taxonomy, aliases, source releases, and planner projections |
| `iana` | source data | Pinned IANA country-code, canonical-zone, alias, representative-location, and country-zone data |
| `cldr` | source data | Pinned Unicode CLDR territory, currency, unit-conversion, preference, and localized display metadata |
| `google_libphonenumber` | source data | Numbering-region patterns and formats; never reachability or subscriber identity |
| `geonames` | source data | Worldwide postal rows and the scoped `cities5000` place extract |
| `ecb` | source data | Historical EUR reference exchange rates |
| `ec_tedb` | source data | Dated, category-sensitive EU VAT responses from the Commission TEDB service |
| `nager_date` | source data | Bounded community public-holiday snapshots |
| `cdc` | source data | Effective CDC/NCHS ICD-10-CM tabular hierarchy |
| `nlm_cde` | source data | Public NIH/NLM CDEs, forms, assessment structure, and source rights flags |
| `chat` | application state | Users, conversations, and persisted UI state |
| `c_<32hex>` | tenant data | One authorized conversation's uploads, selected reference copies, bridge tables, and inferred domain roles |
| `m_<md5(sub)>` | tenant data | One user's private master/reference tables |

**Schema count:** after the 2026-08-17 source imports, the configured database has 12 fixed
application/source namespaces: legacy `public`, legacy `knowledgebase`, `chat`, and the nine
active source schemas listed above. After the coordinated Wikidata migration it will still
have 12: infrastructure-only `public`, `wikidata`, `chat`, and those nine source schemas.
The `c_*` and `m_*` names are tenant schema families, not two physical schemas. Additional
source schemas are created only with their first successful sync.

The verified 2026-08-17 inventory after the expanded materialization contained 220 non-system
schemas: 12 fixed, 67 `c_*` conversation schemas, 8 `m_*` private-master schemas, and 133 legacy,
test, benchmark, or evaluation schemas. The last group needs a separately approved
retention cleanup; it was not modified by source synchronization. PostgreSQL system schemas
are not included in this count.

### 2.1 Source-schema ledger

| State | Source | PostgreSQL schema | Initial synchronized tables |
|---|---|---|---|
| Existing, rename required | Wikidata | `wikidata` | `release`, `country`, `administrative territorial entity`, other QID entity tables, `words`, `types`, aliases, and planner projections |
| Implemented and synchronized | IANA Time Zone Database | `iana` | `release`, `country_code`, `zone`, `zone_alias`, `zone_location`, `country_zone` |
| Implemented and synchronized | Unicode CLDR | `cldr` | `release`, `territory_code`, `territory_alias`, `territory_name`, `currency_code`, `currency_name`, `currency_symbol`, `currency_fraction`, `territory_currency`, `unit_prefix`, `unit_constant`, `unit_quantity`, `unit_conversion`, `unit_alias`, `unit_preference` |
| Implemented and synchronized | Google libphonenumber | `google_libphonenumber` | `release`, `territory`, `number_pattern`, `number_format` |
| Implemented and synchronized | GeoNames | `geonames` | `release`, `postal_code`, `place` |
| Implemented and synchronized | European Central Bank | `ecb` | `release`, `exchange_rate` |
| Implemented and synchronized | European Commission TEDB | `ec_tedb` | `release`, `schema_migration`, `response_status`, `vat_rate`, `vat_rate_cn_code`, `vat_rate_cpa_code` |
| Implemented and synchronized | Nager.Date | `nager_date` | `release`, `country`, `holiday`, `holiday_subdivision`, `holiday_type` |
| Implemented and synchronized | CDC/NCHS | `cdc` | `release`, `icd10cm_code` |
| Implemented and synchronized | NIH/NLM CDE Repository | `nlm_cde` | `release`, `cde`, `cde_designation`, `cde_permissible_value`, `form`, `form_element` |
| Importer ready; credentials required | WHO ICD-11 | `who` | `release`, `icd11_mms_entity`; schema absent until licensed import succeeds |
| Importer ready; licensed archive required | LOINC | `loinc` | `release`, `term`, `answer`, `answer_list_link`, `panel_form`, `part`, `part_link`; schema absent until licensed import succeeds |
| Candidate | Open Food Facts | `openfoodfacts` | `release`, `product`, `nutrition`, `allergen` |
| Candidate | GLEIF | `gleif` | `release`, `legal_entity`, `registration`, `relationship` |

Tax, holiday, assessment, and medical-code sources are therefore explicit, but their
coverage is not universal: TEDB is EU VAT only; Nager.Date is community-maintained and
bounded by year; NLM preserves per-form rights; CDC is the U.S. clinical modification of
ICD-10. There is still no generic `tax`, `holiday`, `assessment`, or `healthcare` schema.

Physical shared schemas are source-owned. Domain labels such as `finance`, `healthcare`,
`food`, `retail`, and `geo` belong to dataset metadata and planner roles, not PostgreSQL
namespaces. Do not create one schema or table per Schema.org class, product, or customer, and
do not add `world`, `wikipedia`, `schema_org`, or an empty source schema. Private operational
records remain in the authorized request or user namespace.

### 2.2 One physical owner per semantic contract

Before creating a source table, inventory the legacy `public` staging tables and
`knowledgebase` serving tables. Migrate both into `wikidata`; staging happens transactionally
through temporary tables and does not require a permanent second schema. "Country entity" and "current ISO
country-code record" are related but different contracts; a shared label is not enough to
justify a migration.

- legacy `knowledgebase."country"` migrates to `wikidata."country"`, the QID-keyed source
  entity table. It may contain historical, disputed, or non-ISO entities and remains owned
  by Wikidata entity resolution.
- legacy `public.country` is inspected during migration and then retired; its useful fields
  are already Wikidata-owned and belong in `wikidata`, not a generic staging namespace.
- `iana.country_code`, `iana.zone`, and `cldr.currency_code` retain their source identity.
- Cross-source relationships are explicit registry edges; no copied `geo.country_code` or
  `finance.currency_code` table is created.

The planner must treat a Wikidata entity relation and its linked operational code map as different
roles and must never expose them as interchangeable candidate tables. A compatibility view
is allowed only when two names implement the identical semantic contract during a true
caller migration. There is still exactly one writable owner for each contract, and existing
source rows are reused when their source, license, freshness, and fields pass validation.

### 2.3 Measured source-fitness baseline

The architecture is based on a read-only production inventory and fresh source probes run
on 2026-08-17. These measurements are regression fixtures for the first sync implementation,
not promises that an upstream row count will never change.

| Existing or sampled data | Measured result | Architectural consequence |
|---|---|---|
| live `public.country` | 209 rows; 203 have valid-shaped ISO2 and ISO3; 6 do not; `Netherlands` appears twice under different QIDs | useful Wikidata staging and QID bridge, not the membership authority for current ISO jurisdictions |
| fresh Wikidata country dry-run | 208 rows; 203 valid-shaped ISO pairs; 5 without them | live `Q21`/England is stale, proving that upsert-only refresh leaves removed source rows behind |
| live and fresh Wikidata currency | 161 rows; only 151 use alphabetic three-letter keys; Czech koruna appears under both `203` and `CZK`; QID-keyed and historical currencies are mixed in | never promote `public.currency` wholesale into an ISO currency dimension |
| live `public.timezone` / populated settlement timezone | 0 / 0 | no timezone reference exists to reuse |
| live `public.admin` | 175 rows from only 8 countries, no parent QIDs | not an ISO subdivision registry |
| live `knowledgebase."country"` | 27 lazily materialized QID rows, all also in `public.country` | preserve its open-world entity contract, but do not treat the present rows as complete coverage |
| live `knowledgebase."administrative territorial entity"` | 7,625 rows, including 441 dissolved entities | valuable historical/entity coverage, intentionally different from a current subdivision-code dimension |
| synchronized IANA tzdb 2026c | 249 country codes, 341 canonical zones, 257 aliases, 312 representative locations, and 423 country-zone rows covering 247 country codes | Bouvet Island and Heard/McDonald Islands legitimately have no representative zone; absence is not sync failure |
| synchronized CLDR 48.2 | 309 territory-code rows, 76,913 localized territory names, 183 current numeric currency-code mappings, 115,666 currency names, 48,708 symbols, 507 temporal territory-currency rows, and 410 unit metadata rows | CLDR contains historical, aggregate, exceptional, and private-use territory/currency identifiers; current operational subsets must be filtered, never assumed from table membership |
| IANA/CLDR cross-source audit | all 249 IANA country codes have CLDR alpha-3 and numeric mappings; 153 currencies are currently tender somewhere; 7 territories have two current tender currencies | cross-source joins are valid on explicit code keys, but country-to-currency is one-to-many and temporal |

Consequently, M0 synchronization uses complete staged replacement per source snapshot.
Incremental `ON CONFLICT DO UPDATE` into an active reference relation is forbidden: it does
not remove upstream deletions and cannot provide an immutable replay identity. Wikidata is
an optional QID bridge and entity source, while pinned code/rule datasets determine
operational membership.

The sync scope is explicit. IANA is a full sync of `iso3166.tab`, `zone1970.tab`, and the
default release `Zone`/`Link` records used by the five tables above; it is not a compiled
transition-history sync. CLDR is a full sync of the listed code, alias, currency, unit, and
localized territory/currency display structures across all 1,124 locale files in the 48.2
archive; unrelated CLDR calendars, collation, numbering, likely-subtag, and annotation data
are intentionally out of scope. Thus the implemented tables are full for their declared
source structures, while neither schema claims to mirror every file in its upstream release.

### 2.4 Post-sync architecture audit

A read-only audit of all 2.6 million synchronized data rows produced the following
implementation constraints. These are contracts, not observations that serving may ignore.

| Source capability | Measured shape | Required runtime contract |
|---|---|---|
| IANA country/timezone | 249 country codes; country-to-zone is one-to-many | exact dimension plus multi-row relation |
| CLDR territory/currency | 309 territory identifiers; seven territories have two current tender currencies | identifier-class filtering plus temporal one-to-many selection |
| libphonenumber | 12 calling codes are shared; calling code `1` covers 25 territories; region `001` has nine service codes | composite pattern lookup; never infer ownership or reachability |
| GeoNames postal | 88,181 duplicate `(country_code, postal_code)` keys cover 834,370 of 1,826,904 rows; one key has 646 places | context-required ambiguous lookup; never force one row |
| ECB rates | 220,107 business-day rows; daily currency coverage varies from 27 to 35 | release-pinned latest-on-or-before series with explicit EUR direction |
| EC TEDB | 1,125 rules, 88 categories, 7,000 CN/CPA links, and source-specific `EL`/`XI` jurisdictions | multi-dimensional temporal rule set with advisory provenance |
| Nager.Date | 131 country/date keys have multiple rows; data is bounded to 2025-2027 | bounded multi-row calendar with subdivision and type |
| CDC ICD-10-CM | 46,881 hierarchical codes, effective through 2026-09-30 | versioned terminology hierarchy; exact-code lookup only |
| NLM CDE | 850 copyrighted forms, 209 non-renderable forms, and 714 unresolved form-element references | rights-bearing document graph with permitted unresolved references |

The source schemas and immutable release ledgers remain the correct physical architecture.
The old unique embedded-table registry is not. Deterministic execution does not make an
ambiguous relationship unique; it only makes an incorrect policy repeatable.

## 3. Semantic model

### 3.1 Three semantic layers

The engine separates three meanings with different ownership:

- **Schema.org shell** defines interoperable class/property coordinates, inheritance, and permitted
  mappings. It is the semantic authority, not an instance or factual source.
- **Domain roles** describe private operational tables such as orders, patients, signers,
  leads, and inspections. They guide joins, typed AST expansion, and ranking. They do not
  create global reference data.
- **Reference and instance sources** add deterministic public facts such as country, postal area,
  timezone, unit, or currency metadata. Facts remain in their source schema; domain and
  Schema.org meaning lives in the registry. Wikidata supplies the largest current training
  instance pool and QID bridge but does not define the vocabulary. Every used source release is
  manifest-pinned.

`engine/domain_profiles.py` is the sole registry for domain profiles and internal
roles. `engine/enrichment/registry.py` remains the sole registry for external reference
datasets. Neither registry owns compose routing. `engine/routing.py:route()` remains
unchanged.

### 3.2 Domain profiles and permitted Schema.org mappings

The following inventory is exhaustive for the initial market-led semantic exercise.
Classes may appear in more than one profile and are counted only as vocabulary mappings,
not as required physical tables.

| Domain profile | Private table roles | Permitted Schema.org classes |
|---|---|---|
| `common_party_location` | people, customers, contacts, organizations, businesses, addresses, locations, services | [`Person`](https://schema.org/Person), [`Organization`](https://schema.org/Organization), [`LocalBusiness`](https://schema.org/LocalBusiness), [`ContactPoint`](https://schema.org/ContactPoint), [`PostalAddress`](https://schema.org/PostalAddress), [`Place`](https://schema.org/Place), [`Country`](https://schema.org/Country), [`AdministrativeArea`](https://schema.org/AdministrativeArea), [`City`](https://schema.org/City), [`GeoCoordinates`](https://schema.org/GeoCoordinates), [`Service`](https://schema.org/Service) |
| `food_commerce` | merchants, products, product groups, variants, offers, menus, menu items, orders, order items, invoices, payments, deliveries | [`Product`](https://schema.org/Product), [`ProductGroup`](https://schema.org/ProductGroup), [`Offer`](https://schema.org/Offer), [`Order`](https://schema.org/Order), [`OrderItem`](https://schema.org/OrderItem), [`Invoice`](https://schema.org/Invoice), [`PriceSpecification`](https://schema.org/PriceSpecification), [`UnitPriceSpecification`](https://schema.org/UnitPriceSpecification), [`PaymentChargeSpecification`](https://schema.org/PaymentChargeSpecification), [`DeliveryChargeSpecification`](https://schema.org/DeliveryChargeSpecification), [`QuantitativeValue`](https://schema.org/QuantitativeValue), [`MonetaryAmount`](https://schema.org/MonetaryAmount), [`ParcelDelivery`](https://schema.org/ParcelDelivery), [`Menu`](https://schema.org/Menu), [`MenuItem`](https://schema.org/MenuItem), [`FoodEstablishment`](https://schema.org/FoodEstablishment), [`Restaurant`](https://schema.org/Restaurant), [`Bakery`](https://schema.org/Bakery), [`GroceryStore`](https://schema.org/GroceryStore) |
| `registration_booking` | events, sessions, courses, schedules, registrations, reservations, memberships, schools, hotels | [`Event`](https://schema.org/Event), [`EducationEvent`](https://schema.org/EducationEvent), [`Course`](https://schema.org/Course), [`CourseInstance`](https://schema.org/CourseInstance), [`Schedule`](https://schema.org/Schedule), [`Reservation`](https://schema.org/Reservation), [`EventReservation`](https://schema.org/EventReservation), [`FoodEstablishmentReservation`](https://schema.org/FoodEstablishmentReservation), [`LodgingReservation`](https://schema.org/LodgingReservation), [`ProgramMembership`](https://schema.org/ProgramMembership), [`EducationalOrganization`](https://schema.org/EducationalOrganization), [`School`](https://schema.org/School), [`CollegeOrUniversity`](https://schema.org/CollegeOrUniversity), [`NGO`](https://schema.org/NGO), [`LodgingBusiness`](https://schema.org/LodgingBusiness), [`Hotel`](https://schema.org/Hotel) |
| `lead_crm` | leads, inquiries, campaigns, customer records, service requests | Reuses `Person`, `Organization`, `ContactPoint`, `Service`, `Offer`, and `PostalAddress` |
| `signature_approval` | documents, signature requests, signers, approval steps, consent and authorization records | [`DigitalDocument`](https://schema.org/DigitalDocument), [`DigitalDocumentPermission`](https://schema.org/DigitalDocumentPermission), [`AuthorizeAction`](https://schema.org/AuthorizeAction), plus `Person` and `Organization` |
| `healthcare_intake` | patients, providers, practices, intake forms, assessments, conditions, medications, procedures, tests | [`Patient`](https://schema.org/Patient), [`MedicalOrganization`](https://schema.org/MedicalOrganization), [`MedicalClinic`](https://schema.org/MedicalClinic), [`Physician`](https://schema.org/Physician), [`Hospital`](https://schema.org/Hospital), [`MedicalCondition`](https://schema.org/MedicalCondition), [`Drug`](https://schema.org/Drug), [`MedicalProcedure`](https://schema.org/MedicalProcedure), [`MedicalTest`](https://schema.org/MedicalTest), plus `DigitalDocument` and `AuthorizeAction` |
| `safety_compliance` | employees, sites, equipment, vehicles, inspections, incidents, findings, corrective actions, reports | [`Report`](https://schema.org/Report), [`Vehicle`](https://schema.org/Vehicle), plus `Person`, `Organization`, `Place`, `Product`, and `DigitalDocument` |

### 3.3 Internal roles without exact Schema.org classes

Schema.org is a web interoperability vocabulary, not an operational form, clinical, ERP,
or safety ontology. The following are first-class internal roles and must not be forced into
an approximate class:

`FormTemplate`, `FormSubmission`, `Lead`, `Application`, `Registration`, `Booking`,
`SignatureRequest`, `Signer`, `ApprovalStep`, `ConsentRecord`, `PatientIntake`,
`AssessmentDefinition`, `AssessmentResponse`, `SafetyInspection`, `Incident`, `Finding`,
`CorrectiveAction`, `MenuItemVariant`, `Fulfillment`, `PaymentRecord`, `DeliveryZone`, and
`TaxRule`.

An internal role may declare an exact Schema.org mapping where one exists, but the internal
role remains the planner contract. For example, `Booking` may map to a subtype of
`Reservation`, while `SignatureRequest` has no exact Schema.org class.

### 3.4 Exhaustive class-to-storage index

`Request-private` means rows remain in `c_<32hex>` or `m_<md5(sub)>`; the class is a
semantic role, not a shared catalog table. A listed domain table supplies optional public
attributes and never becomes the owner of the customer's operational record.

| Schema.org class | Domain owner | Internal table role | Storage owner or optional shared augmentation |
|---|---|---|---|
| `AdministrativeArea` | common | `administrative_area` | request-private; Wikidata may resolve entities; no subdivision-code source is approved yet |
| `AuthorizeAction` | signature, healthcare | `consent_authorization` | request-private only |
| `Bakery` | food commerce | `merchant` | request-private only |
| `City` | common | `city` | request-private; Wikidata may resolve entities; `geonames` may supply exact-key locality metadata |
| `CollegeOrUniversity` | registration | `education_provider` | request-private; legacy Wikidata table may resolve identity |
| `ContactPoint` | common, lead | `contact_point` | request-private; `google_libphonenumber` may supply possible-number and formatting metadata only |
| `Country` | common | `country` | request-private; `wikidata."country"` resolves entities, `iana.country_code` and `cldr.territory_code` supply code metadata |
| `Course` | registration | `course` | request-private only |
| `CourseInstance` | registration | `course_session` | request-private only |
| `DeliveryChargeSpecification` | food commerce | `delivery_charge` | request-private; a future approved tax source may augment calculation |
| `DigitalDocument` | signature, healthcare, safety | `document` | request-private only |
| `DigitalDocumentPermission` | signature | `document_permission` | request-private only |
| `Drug` | healthcare | `medication` | request-private only; no remote lookup |
| `EducationalOrganization` | registration | `education_provider` | request-private only |
| `EducationEvent` | registration | `education_event` | request-private only |
| `Event` | registration | `event` | request-private only |
| `EventReservation` | registration | `registration` | request-private only |
| `FoodEstablishment` | food commerce | `merchant` | request-private only |
| `FoodEstablishmentReservation` | registration, food commerce | `booking` | request-private only |
| `GeoCoordinates` | common | `coordinates` | request-private; `geonames.postal_code` may augment coordinates with source accuracy retained |
| `GroceryStore` | food commerce | `merchant` | request-private only |
| `Hospital` | healthcare | `provider_organization` | request-private; legacy Wikidata table may resolve identity |
| `Hotel` | registration | `lodging_provider` | request-private only |
| `Invoice` | food commerce | `invoice` | request-private; a future approved tax source may augment calculation |
| `LocalBusiness` | common, food commerce | `business` | request-private only |
| `LodgingBusiness` | registration | `lodging_provider` | request-private only |
| `LodgingReservation` | registration | `booking` | request-private only |
| `MedicalClinic` | healthcare | `provider_organization` | request-private only |
| `MedicalCondition` | healthcare | `condition` | request-private; `cdc.icd10cm_code` may resolve an explicit code, never infer a diagnosis |
| `MedicalOrganization` | healthcare | `provider_organization` | request-private only |
| `MedicalProcedure` | healthcare | `procedure` | request-private only; no remote lookup |
| `MedicalTest` | healthcare | `assessment_or_test` | request-private; `nlm_cde` can resolve explicit public CDE/form ids subject to rights flags |
| `Menu` | food commerce | `menu` | request-private only |
| `MenuItem` | food commerce | `menu_item` | request-private only |
| `MonetaryAmount` | food commerce | `money` | request-private; `cldr.currency_code` and `cldr.currency_fraction` supply metadata |
| `NGO` | registration, lead | `organization` | request-private; legacy Wikidata table may resolve identity |
| `Offer` | food commerce, lead | `offer` | request-private; a future approved tax source may augment calculation |
| `Order` | food commerce | `order` | request-private; a future approved tax source may augment calculation |
| `OrderItem` | food commerce | `order_item` | request-private only |
| `Organization` | common | `organization` | request-private; legacy Wikidata table may resolve identity |
| `ParcelDelivery` | food commerce | `fulfillment` | request-private only |
| `Patient` | healthcare | `patient` | request-private only; PHI boundary applies |
| `PaymentChargeSpecification` | food commerce | `payment_charge` | request-private; a future approved tax source may augment calculation |
| `Person` | common | `person` | request-private; legacy Wikidata table is not used for private identity |
| `Physician` | healthcare | `provider_person` | request-private only |
| `Place` | common, safety | `location_or_site` | request-private; `geonames` may augment exact postal/place keys with ambiguity checks |
| `PostalAddress` | common | `address` | request-private; `geonames.postal_code` may augment locality with country context |
| `PriceSpecification` | food commerce | `price_specification` | request-private; a future approved tax source may augment calculation |
| `Product` | food commerce, safety | `product_or_equipment` | request-private; gated `openfoodfacts.product` may augment exact GTINs |
| `ProductGroup` | food commerce | `product_group` | request-private only |
| `ProgramMembership` | registration | `membership` | request-private only |
| `QuantitativeValue` | food commerce | `quantity` | request-private; CLDR unit quantity, conversion, and preference tables supply unit metadata |
| `Report` | safety | `report` | request-private only |
| `Reservation` | registration | `booking` | request-private only |
| `Restaurant` | food commerce | `merchant` | request-private only |
| `Schedule` | registration | `schedule` | request-private; calendar and timezone references may augment dates |
| `School` | registration | `education_provider` | request-private; legacy Wikidata table may resolve identity |
| `Service` | common, lead | `service` | request-private only |
| `UnitPriceSpecification` | food commerce | `unit_price` | request-private; currency and unit references may augment metadata |
| `Vehicle` | safety | `equipment` | request-private; legacy Wikidata table may resolve public vehicle models |

### 3.5 Legacy Wikidata mappings

The 27 mappings in `engine/data/families.json` remain supported by the entity resolver and
migrate from legacy `knowledgebase` to `wikidata` as one coordinated caller migration. They
are a separate legacy capability, not part of the market-led domain-profile acceptance gate.
Operational code maps link to those entities by QID where available; they do not replace the
entity tables. There must never be two independently writable or planner-visible owners for
the same semantic contract.

## 4. Shared reference datasets

### 4.1 Initial reference scope

Only datasets with repeated value across the product markets enter the initial shared
reference layer.

These table names describe operational reference contracts, not instructions to duplicate
the Wikidata entity graph. The source-fitness baseline in section 2.3 determines which
existing fields can be retained. Wikidata QIDs are optional bridges and never decide
membership in an ISO, CLDR, or IANA code set.

| Priority | Physical source table | Key | Payload | Schema.org interpretation |
|---|---|---|---|---|
| M1 | `iana.country_code` | `(release_id, alpha2)` | IANA country name | `DefinedTerm`; bridge to `Country` |
| M1 | `iana.zone` | `(release_id, timezone_id)` | canonical timezone identifier | `DefinedTerm` |
| M1 | `iana.zone_alias` | `(release_id, alias_id)` | target timezone identifier | alias bridge |
| M1 | `iana.zone_location` | `(release_id, timezone_id)` | representative coordinates and comment from `zone1970.tab` | timezone location metadata |
| M1 | `iana.country_zone` | `(release_id, country_alpha2, timezone_id)` | complete exploded country-zone membership | bridge from `Country` to representative zones |
| M1 | `cldr.territory_code` | `(release_id, territory_code)` | numeric, alpha-3, and FIPS mappings where published | `DefinedTerm`; bridge to `Country` only after operational filtering |
| M1 | `cldr.territory_alias` | `(release_id, territory_code)` | replacement and reason | historical/deprecated alias bridge |
| M1 | `cldr.territory_name` | `(release_id, locale, territory_code, alt)` | localized name and draft state | localized place/territory label |
| M1 | `cldr.currency_code` | `(release_id, currency_code)` | current numeric mapping | `DefinedTerm`; metadata for `MonetaryAmount` |
| M1 | `cldr.currency_name` | `(release_id, locale, currency_code, plural_count, alt)` | localized singular/plural name and draft state | display metadata for `MonetaryAmount` |
| M1 | `cldr.currency_symbol` | `(release_id, locale, currency_code, alt)` | localized symbol and draft state | display metadata; never an identity key |
| M1 | `cldr.currency_fraction` | `(release_id, currency_code)` | standard/cash digits and rounding | calculation metadata for `MonetaryAmount` |
| M1 | `cldr.territory_currency` | `(release_id, territory_code, source_order)` | currency, validity bounds, and tender flag | temporal territory-currency relation |
| M1 | `cldr.unit_prefix` | `(release_id, prefix)` | symbol and base-10/base-2 power expressions | unit construction metadata |
| M1 | `cldr.unit_constant` | `(release_id, constant)` | exact source expression, status, and description | conversion expression input |
| M1 | `cldr.unit_quantity` | `(release_id, base_unit)` | quantity and status | `DefinedTerm`, `QuantitativeValue` |
| M1 | `cldr.unit_conversion` | `(release_id, source_unit)` | base unit, factor/offset expressions, special function, and systems | typed conversion metadata |
| M1 | `cldr.unit_alias` | `(release_id, unit_code)` | replacement and reason | unit alias bridge |
| M1 | `cldr.unit_preference` | `(release_id, category, usage, source_order)` | regions, threshold, skeleton, and preferred unit | locale-independent regional unit preference |

The CLDR unit tables are required before food-commerce arithmetic. Quantities sold by
count, weight, and volume must not be aggregated or compared as interchangeable values.
Factor and offset fields are source expressions, not floating-point values; a later typed
evaluator must parse only the documented CLDR grammar before conversions enter planning.

### 4.2 Market-adjacent datasets

These require a separate demand, source, license, and correctness gate:

| State | Physical table | Contract |
|---|---|---|
| Source-gated | source not selected: subdivision codes | legacy `public.admin` covers only 8 countries and CLDR does not claim complete ISO 3166-2 maintenance |
| Materialized, unwired | `nlm_cde.cde`, `nlm_cde.form`, and child tables | Public research CDE/form snapshot; per-form copyright and `no_render_allowed` are retained and must gate rendering |
| Materialized, unwired | `ec_tedb.vat_rate` and code tables | EU VAT only, date/category sensitive, non-binding; national law remains authoritative |
| Materialized, unwired | `nager_date.holiday` and child tables | Community-maintained, explicit 2025-2027 horizon; not a global legal authority |
| Materialized, unwired | `cdc.icd10cm_code` | U.S. ICD-10-CM hierarchy effective 2026-04-01 through 2026-09-30; terminology is not clinical reasoning |
| Importer ready, credential-gated | future `who.icd11_mms_entity` | WHO API credentials and CC BY-ND obligations required before the schema can exist |
| Importer ready, archive-gated | future LOINC tables | Licensed LOINC Complete archive required; includes standardized assessment panels and answer lists |
| Compiler-gated | future `iana.zone_transition` | tzdb source rules must be compiled reproducibly into tested non-overlapping intervals; the source archive does not publish this as a table |
| Demand-gated | `openfoodfacts.product` | useful for packaged goods only; exact GTIN required; Open Food Facts contract must pass redistribution review |
| Materialized, bounded serving path | `ecb.exchange_rate` plus derived `knowledgebase.exchange_rate` | active source history is projected to exact calendar-date cross rates with release provenance; the generic temporal registry remains disabled |
| Demand-gated | `gleif.legal_entity` | LEI has weak coverage for the portfolio's SMB, nonprofit, school, restaurant, and private-practice buyers |
| Materialized, unwired | `geonames.postal_code` and `geonames.place` | CC BY attribution and ambiguity/accuracy abstention are required in serving |
| Materialized, unwired | `google_libphonenumber.territory`, patterns, and formats | possibility/format only, never reachability, subscriber, or current carrier |

Card BIN, person identity/KYC, email identity, current phone carrier, patient identity,
prescription lookup, and request-time geocoding are out of scope.

### 4.3 Materialized source contracts

| Source schema and tables | Pinned source | Extraction contract |
|---|---|---|
| `iana.country_code`, `iana.zone`, `iana.zone_alias`, `iana.zone_location`, `iana.country_zone` | [IANA tzdb](https://www.iana.org/time-zones), release 2026c | fully ingest `iso3166.tab`, `zone1970.tab`, and default-release `Zone`/`Link` records; preserve aliases and every exploded country-zone row; do not claim transition intervals |
| all 14 `cldr` data tables in section 4.1 | [Unicode CLDR](https://cldr.unicode.org/index/downloads), release 48.2 | fully ingest the declared code, alias, currency, unit, and locale display elements; preserve numeric codes as text, historical temporal rows, draft/alt/plural distinctions, and conversion expressions; never infer identity from a symbol or display name |
| `google_libphonenumber.territory`, `number_pattern`, `number_format` | [Google libphonenumber](https://github.com/google/libphonenumber), release 9.0.31 | ingest primary phone metadata; key non-geographic region `001` by calling code; make no allocation or reachability claim |
| `geonames.postal_code`, `geonames.place` | [GeoNames exports](https://www.geonames.org/export/), snapshot 2026-08-17 | fully ingest `allCountries.zip` postal rows and the scoped `cities5000.zip` place extract; retain source accuracy and ambiguity |
| `ecb.exchange_rate` | [ECB euro reference rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html), history through 2026-08-14 | ingest every non-missing `(date, quote currency, units per EUR)` observation; never present as a transaction rate |
| all `ec_tedb` data tables | [European Commission TEDB](https://ec.europa.eu/taxation_customs/tedb/index.html), situation 2026-08-17 | preserve rate class/type/value, source effective date, category, CN/CPA codes, and comments from the full supported-member-state SOAP response |
| all `nager_date` data tables | [Nager.Date](https://date.nager.at/api), years 2025-2027 | snapshot all advertised countries and preserve subdivision/type relations; expose community provenance and year bounds |
| `cdc.icd10cm_code` | [CDC/NCHS ICD-10-CM](https://www.cdc.gov/nchs/icd/icd-10-cm/files.html), effective 2026-04-01 | fully ingest the tabular XML hierarchy, source ordering, instructional metadata, and effective interval |
| all `nlm_cde` data tables | [NIH/NLM CDE Repository](https://cde.nlm.nih.gov/api), snapshot 2026-08-17 | retrieve the complete anonymous public result through audited typed partitions; preserve raw documents, permissible values, form hierarchy, scoring fields, and rights flags |

IANA states that tzdb data is public domain unless a file says otherwise. Unicode data files
use the Unicode License; the sync artifact must retain the required notices. ISO's official
online collection remains the authority for ISO maintenance, but its downloadable
integration product is paid. M0 therefore uses the pinned redistributable IANA/CLDR
artifacts above and validates their cross-product invariants; it does not scrape the ISO
Online Browsing Platform.

## 5. Snapshot and dataset contracts

### 5.1 Source releases

Each source schema owns its release metadata. There is no separate `ref_catalog` schema and
no cross-source table that duplicates source ownership. The DDL below illustrates the
shared contract used by new source schemas; IANA and CLDR carry the same policy fields, and
Wikidata adopts it during its coordinated migration:

```sql
CREATE SCHEMA IF NOT EXISTS iana;

CREATE TABLE IF NOT EXISTS iana.release (
  release_id        text PRIMARY KEY,
  source_version    text NOT NULL,
  source_url        text NOT NULL,
  content_sha256    text NOT NULL,
  schema_version    integer NOT NULL,
  completeness      text NOT NULL CHECK (
    completeness IN ('full_source_artifact', 'full_declared_scope', 'bounded_snapshot')
  ),
  import_scope       jsonb NOT NULL,
  license_name       text NOT NULL,
  license_url        text NOT NULL,
  table_counts      jsonb NOT NULL,
  materialized_at   timestamptz NOT NULL DEFAULT now(),
  status            text NOT NULL CHECK (
    status IN ('staged', 'active', 'retired', 'rejected')
  ),
  UNIQUE (source_version, content_sha256)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_iana_release_active
  ON iana.release ((status))
  WHERE status = 'active';
```

Every synchronized source table includes `release_id` with an FK to its own schema's
`release` table. The implemented synchronizers parse and validate their complete declared
scope,
insert a content-addressed release as `staged`, verify every loaded table count, and switch
the active release in one transaction. A failure rolls back schema and data changes. Fresh
materialization uses the current DDL; an existing older materialization must first pass the
independent migration runner. The same active content is then idempotent. Synchronizers
never upsert active rows.
Active-release diffs and separate production database roles remain M0B work.

`engine/enrichment/registry.py` owns `DatasetDefinition`: qualified storage, source and
license decision, identity and lookup keys, cardinality, ambiguity, temporal behavior,
row-usage restrictions, eligibility, privacy class, activation, and release thresholds.
Each `<source>.release` table owns materialization state for that source only. The execution
manifest pins `(source_schema, release_id, schema_version, contract_hash)` and the registry
hash for every source it uses.

Schema migration is separate from source-content identity. The audit found the live
GeoNames database still carrying the older unscoped postal index while checked-in DDL named
a release-scoped index. `db.sync.migrations` now applies checksummed, transactional migrations
without downloading data or creating absent source schemas. CLDR, EC TEDB, and GeoNames are
at schema version 2; rerunning the migration command applies nothing.

### 5.2 Type rules

- Country, currency, unit, GTIN, LEI, postal, medical, and telephone identifiers are text.
- Exact source keys are normalized offline; serving never mutates source identity silently.
- A series table carries explicit effective dates and direction.
- Source-specific extra columns are invisible to the planner until added to the registry,
  documentation, fixtures, and compatibility tests.
- Ambiguous rows are marked and abstain unless the request provides sufficient context.

### 5.3 Qualified relations and database roles

The registry stores `schema_name` and `table_name` as separate allowlisted lowercase SQL
identifiers. Serving never accepts a client-supplied relation name, resolves a reference
through `search_path`, or constructs a qualified name from unchecked text. Dataset names
remain logical identities and are not executable SQL.

Production uses separate database principals:

- the sync role owns and writes only approved source schemas;
- the serving role has `SELECT` only on active source releases and required source tables;
- application writes remain limited to `chat`, authorized `c_<32hex>`, and authorized
  `m_<md5(sub)>` namespaces;
- migrations grant new source relations explicitly; `public` is not implicitly trusted
  through `search_path`.

The repository's current single-role development setup is not a production permission
contract. Role DDL and an integration test that proves serving cannot mutate snapshots or
source facts are M0B deliverables.

## 6. Runtime architecture

```text
question + authorized private tables
  -> request-intent evidence
  -> deterministic domain-role evidence
  -> domain-profile candidates
  -> conservative value typing
  -> eligible reference datasets
  -> active snapshot and exact key-coverage checks
  -> request-local reference copies + trusted explicit FK edges
  -> existing relation graph
  -> existing typed AST beam and ranker
  -> execution
  -> execution manifest + warnings
```

### 6.1 Named owners

| Responsibility | Single owner | Interface |
|---|---|---|
| Domain profiles and internal roles | `engine/domain_profiles.py` | `DomainProfile`, `RoleDefinition`, `profiles()`, `DOMAIN_PROFILE_VERSION` |
| Deterministic role evidence | `engine/domain_typing.py` | `detect_roles(tables)`, `detect_profiles(tables)` |
| Reference policy | `engine/enrichment/registry.py` | `DatasetDefinition`, typed policies, `REGISTRY_VERSION` |
| Requested intent | `engine/enrichment/intents.py` | `requested_attributes(question) -> frozenset[str]` plus inspectable phrase/rule evidence |
| Value typing | `engine/enrichment/value_types.py` | `detect_column(values) -> frozenset[str]` |
| Reference selection | `engine/enrichment/select.py` | `select_datasets(tables, request_evidence, snapshots)` |
| Snapshot access | `engine/enrichment/store.py` | `active_snapshot(definition)`, bounded `load_by_keys()` |
| Source policy adaptation | `engine/enrichment/adapters.py` | typed `MATCHED`, `NOT_FOUND`, `AMBIGUOUS`, `POLICY_BLOCKED`, or `INELIGIBLE` outcome |
| Request-local orchestration | `engine/enrichment/runtime.py` | activation + intent + role + type + snapshot + coverage -> tabs, edges, manifest |
| Explicit edge validation | `engine/relations.py` | `relate(tables, explicit_fks=())` validates and merges trusted tuple edges |
| Planner ingestion | `engine/tables.py:TableQuery.ingest` | explicit internal argument only; client table payloads cannot declare edges |
| Tuple FK and SQL join | `engine/sql_schema.py`, `engine/sql_ast.py` | one logical FK with ordered column pairs; render one atomic `ON ... AND ...` clause |
| AST profile expansion | `engine/sql_profile_expansion.py` | consume domain role/profile evidence without a second planner |
| Serving orchestration | `engine/server.py:_post_world` | private references -> guarded enrichment -> `MODEL.serve`; attaches provenance only when used |
| Replay identity | `engine/enrichment/registry.py:ExecutionManifest` | full SHA-256 identity in `provenance.enrichment` |
| Offline evaluation | `regress/enrichment.py` | selection, role, candidate-pool, and top-1 metrics |
| Source ingestion | `db/sync/sources/<source>/sync.py` | fetch pinned release, stage, validate, activate; never imported by serving |

No new planner, router, model registry, or serving fallback is introduced.

### 6.2 Activation contract

A domain role requires compatible evidence from table/column names, values, relationships,
and request intent. A weak token such as `code`, `name`, `value`, `status`, `date`, or
`amount` cannot establish a role alone.

A reference dataset requires all of:

1. an explicit requested attribute;
2. a compatible domain role where the dataset is domain-specific;
3. a conservatively detected key type;
4. an active approved snapshot;
5. exact row coverage meeting its registry threshold;
6. a validated tuple edge whose declared `ONE`/`MANY` cardinality matches the source;
7. available request table budget.

`REQUIRE_CONTEXT` datasets additionally need sufficient disambiguating columns or they
abstain. A `MANY` dataset may return multiple rows only when the question and AST shape can
represent that multiplicity. Row denial columns are evaluated before rendering or planner
materialization.

Failure at any step abstains with a provenance warning. Serving never fetches a remote
source or silently falls back to fuzzy matching.

### 6.3 Privacy contract

- Domain typing runs locally inside the authorized request boundary.
- PAN, PHI, full email, full phone, patient name, free text, signatures, and uploaded files
  never leave that boundary for enrichment.
- Healthcare domain roles can guide local SQL structure, but cannot activate remote lookup.
- Telemetry exports only opted-in aggregates after local classification, allowlisting,
  minimum cohort enforcement, retention limits, and deletion procedures.
- Client payloads cannot declare trusted roles, explicit FKs, or reference snapshots.

## 7. Build sequence

Implementation status on 2026-08-17:

| Work item | Status |
|---|---|
| Common immutable release contract and publisher-specific parsers | complete for nine active public sources |
| IANA, CLDR, libphonenumber, GeoNames, ECB, TEDB, Nager.Date, CDC, and NLM materialization | complete; exact active releases in `SOURCE_DATA.md` |
| WHO ICD-11 and LOINC import paths | complete; materialization blocked only by absent licensed credentials/archive |
| Parser/validation regression suite | complete; 18 source-shape, migration, rollback, grant-plan, and credential-boundary tests |
| Versioned source migration runner and schema-v2 audit corrections | complete for CLDR, EC TEDB, and GeoNames; applied live and idempotence-verified |
| Typed source capabilities, qualified definitions, snapshot pins, and bounded store | implemented; `iana_country` code-approved, other source definitions disabled |
| Legacy `public`/`knowledgebase` to `wikidata` migration | pending; requires coordinated caller migration |
| Requested-attribute extraction and same-profile generic-query contrastives | implemented and serving-wired behind two-key activation |
| Policy-aware source adapters | implemented and conditionally wired for activation, eligibility, temporal gate, ambiguity/context, rights denial, warnings, and provenance |
| Composite trusted edges through relation ingestion, schema graph, AST validation, and SQL rendering | implemented; scalar API compatibility retained |
| Seven domain profiles and deterministic role evidence | implemented; versioned registry and conservative abstention |
| Serving-shaped contract corpus | implemented; 25 positives and >=100 negatives per profile, plus strict candidate-pool/top-1 fixture |
| Public product-template development corpus | implemented; 35 source-cited cases, 5 per profile, profile and role precision/recall 1.0 |
| Request-local materialization, trusted-edge propagation, and response manifests | implemented behind registry approval plus deployment allowlist |
| IANA country-name release gate | passed hermetically: 25 positives, 100 negatives, 1.0 selection/pool/top-1, zero harmful selections, deterministic replay; live materialization spike passed |
| Least-privilege grants and validated-release rollback | implemented; real database grant/write-denial audit passed transactionally; production role application pending |
| Opted-in product-held-out evaluation | strict metadata-only loader implemented; actual consented corpus pending and still required for domain-profile production claims |
| Compiled IANA transitions and generic temporal AST | gated; not started; ECB uses an exact-date offline projection instead of this capability |

The source-storage slice of M1 was deliberately performed before planner integration so the
architecture could be corrected from actual data. This does not waive the M0A-C gates
before any source table influences an answer.

### M0A - Domain contracts and evaluation corpus

1. Implemented: `engine/domain_profiles.py` has the seven profiles in section 3 and one
   content-derived profile version.
2. Implemented: deterministic role evidence uses table names and column shape without an
   LLM, request-time network access, or compose routing. Exact longer roles suppress nested
   generic aliases (`patient_intake` does not also become `patient`).
3. Partially implemented: the checked-in privacy-safe contract corpus uses independently
   declared product-shaped fixtures plus generated row instances. The separate 35-case
   development corpus is based on public Formesign, Neartail, and Formfacade template pages.
   `regress.product_templates` also validates a consent-bound metadata-only private format,
   rejects row values, and hashes the corpus for replay. Actual opted-in held-out metadata
   has not been supplied and must not be replaced by the public fixtures.
4. Implemented in the contract corpus: cross-profile negatives and ambiguous generic tables.
   Add real renamed keys, multi-role tables, and product-template collisions to the held-out set.

Gate: every role has at least 25 positive and 100 negative cases; profile precision is at
least 0.99 and profile recall at least 0.95 on the held-out corpus.

### M0B - Storage, intent, and explicit-edge wiring

1. Extend the implemented parser dry runs into a no-write licensing/provenance regression
   audit; fail closed when source shape or licensing metadata changes.
2. Keep the implemented per-source `release` contract and source DDL. Use the implemented
   versioned migration runner so schema/index changes do not depend on downloading the source again.
   `db/init.sql` must not create empty source schemas speculatively.
3. Implemented as a bounded world projection, not generic temporal-registry activation:
   `db.sync.build_exchange_rate` selects the ledger-active ECB release, materializes exact
   calendar-date cross rates, preserves source date and release ID, and the world planner proves
   direction and typed arithmetic. Hermetic generic-planner tests may still use explicit local
   rate tables; no embedded rate snapshot is a source of production facts.
4. Implemented: requested-attribute extraction returns inspectable phrase/rule evidence;
   contrastives keep ordinary own-data grouping such as `amount by currency`, `customers by
   country`, and `orders by postal code` unchanged.
5. Implemented: trusted tuple edges flow through `relations.py`, `TableQuery.ingest`,
   `SchemaGraph`, AST validation, and SQL rendering. Composite predicates remain one atomic
   edge; scalar callers retain their existing fields and signatures.
6. Implemented: qualified relations are registry-validated. `db.reference_grants` derives
   least-privilege reads from code-approved definitions and passed a real transactional
   write-denial audit. Apply it to the actual non-superuser production role before enablement.
7. Implemented: serving emits a complete manifest and abstention warnings when enrichment participates.
8. Implemented: `ec_tedb.response_status` has nullable optional capability flags; CLDR
   validity values are typed dates; GeoNames lookup indexing is release-scoped.
9. Implemented: source adapters fail closed on disabled or deployment-omitted datasets and
   unsupported temporal semantics, preserve multi-row ambiguity, apply GeoNames context,
   enforce NLM render-denial flags, and emit pinned source/license provenance.
10. Implemented: `EnrichmentRuntime` materializes bounded matched rows as
    request-local planner tabs, passes trusted edges through the existing serving stack, and
    emits source/profile/model/private-reference replay identity. `iana_country` requires both
    registry approval and `ENRICHMENT_ACTIVE_DATASETS=iana_country`; the default empty allowlist
    selects nothing. There is no production registry definition for static FX rates; the
    separately documented ECB world projection is source-derived and release-labelled.

Gate: no-intent requests are byte-for-byte unchanged; renamed keys use only validated
explicit edges; routing transitions are unchanged; serving performs zero network calls.

### M0C - Serving-faithful evaluation

Required gates per profile and per reference dataset:

- selection precision >= 0.99;
- selection recall >= 0.95;
- exact row coverage >= 0.90;
- harmful-selection rate <= 0.005;
- strict AST candidate-pool recall >= 0.95 on correctly recognized cases;
- no statistically significant top-1 regression on no-profile/no-enrichment own data;
- deterministic replay produces the same candidate order and SQL;
- zero privacy-boundary or request-time-network violations.

### M1 - Common deterministic foundations

The public source tables in `SOURCE_DATA.md` are synchronized. Next, link unique Wikidata
QIDs through registry edges where useful, but do not copy source rows into domain-named
tables. Materialization does not grant planner visibility: each source must still clear M0C
before serving activation. Phone output describes numbering-plan metadata, not reachability
or current carrier ownership.

### M2 - Postal and geo

Ship country-qualified postal keys. Ambiguous rows abstain without sufficient locality or
region context. Do not request-time geocode free text.

### M3 - Healthcare intake and signature profiles

Support patient/provider/intake/assessment/document/signature/approval relationships using
request-local data only. Add standardized assessment scoring only for reviewed, versioned,
licensed definitions with explicit missing-answer behavior. No medical diagnosis, advice,
or remote PHI lookup is introduced.

### M4 - Food-commerce profile

Support merchant/menu/product-group/variant/order/order-item/payment/fulfillment joins.
Typed arithmetic must preserve currency and unit dimensions. Add tax only after
jurisdiction, category, temporal validity, inclusive/exclusive price, and rounding contracts
pass review. GTIN remains independently demand-gated.

### M5 - Registration, booking, lead, and membership profiles

Support event/session/course/schedule/reservation/member/application/lead relationships.
Keep applications, leads, and form submissions as internal roles rather than inventing
Schema.org classes.

### M6 - Safety and compliance profile

Support site/equipment/employee/inspection/incident/finding/corrective-action/report joins.
Release requires field-workflow fixtures covering timestamps, locations, repeated
inspections, photo metadata, signatures, and approval chains. Binary files remain outside
SQL candidate generation.

### Gated research

Generic temporal-source activation remains gated. The production ECB world path now covers
typed arithmetic, target direction over the EUR base, per-row exact-date composite joins,
previous-business-day projection, source-release provenance, missing-row abstention, and
SQLite/Postgres tests. This does not activate arbitrary latest-prior joins or VAT/interest
temporal policies. Product GTIN and legal-entity work remain
demand-gated. Medical terminology is materialized where public and import-ready where
licensed, but planner use still requires explicit-code intent, privacy, and harm evaluation.

## 8. Versioning, testing, and release

Before enrichment affects candidate construction, `ExecutionManifest` records the request
hash, engine build, planner/ranker configuration hashes, model artifact hash, Schema.org
and registry versions, domain-profile version, selected snapshot IDs, selected private-
reference hashes, and the supplied `as_of` value (empty when no temporal constraint was
requested). Source rows are immutable and release-qualified, so replay uses those exact
snapshot pins rather than an unrepeatable live transaction identifier.

Every milestone runs:

```powershell
python -m tests.test_enrichment
python -m tests.test_source_sync
python -m tests.test_master_ingest
python -m tests.test_sql_ast
python -m tests.test_compose
python -m compileall -q engine db training tests orchestrator mcp_server regress
python -m tests.run_all
```

Planner changes require a serving-faithful Spider `whole_db` evaluation with top-1 accuracy
and candidate-pool recall. Domain and enrichment release evaluation remains separate because
Spider has no external reference-dataset or product-profile contract.

Activation is implemented as one atomic snapshot-status transition. `db.sync.releases`
reactivates only a previously validated `retired` release under an exclusive ledger lock and
verifies that exactly one release remains active. Removing a dataset from the deployment
allowlist makes enrichment abstain and leaves ordinary AST serving unchanged.

## 9. Definition of done

This exercise is complete only when:

1. M0A-C gates pass on held-out, serving-faithful market-profile data.
2. Every enabled domain role has one documented owner and tested evidence contract.
3. Every enabled reference dataset has an active immutable snapshot and validated typed
   table.
4. Every affected response records domain-profile and reference-snapshot provenance.
5. Requested intent, value type, domain role, eligibility, key coverage, explicit edge,
   candidate-pool, and top-1 tests pass.
6. The full suite and required Spider evaluation pass on the final code and artifacts.
7. Documentation lists deployed profiles, internal roles, Schema.org version and mappings,
   active tables, source contracts, privacy boundaries, and abstention behavior.
8. No duplicate registry, alternate planner, alternate router, stale embedded dataset,
   duplicate writable semantic contract, request-time connector, or unpinned generated model
   remains. Entity tables and linked code maps have distinct tested roles; any temporary
   compatibility view has one documented canonical owner and is not exposed as a second
   planner table.
