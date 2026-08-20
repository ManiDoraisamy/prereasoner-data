"""Read active source releases and emit canonical Schema.org semantic instances.

Facts remain in publisher-owned PostgreSQL schemas.  This module is a read-only semantic
projection for training: it never creates a schema, changes a release, or copies mutable
facts into serving artifacts.
"""
from __future__ import annotations

from collections import Counter
import re
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable

from engine.schema_model import MAX_SAMPLE_VALUES, sample_values
from engine.schema_org import SchemaContract, schema_uri
from training.schema_org.instances import SemanticInstance


MAPPING_SCHEMA_VERSION = 2          # v2: deterministic presentation variants (name/aux-column diversity)
DEFAULT_ROWS_PER_MAPPING = 5_000
GROUP_SIZE = 8
VARIANT_EVERY = 4                   # 1 in N groups ALSO emits a presentation variant (canonical rows unchanged)


@dataclass(frozen=True)
class SourceMapping:
    source: str
    relation: str
    classes: tuple[str, ...]
    id_columns: tuple[str, ...]
    columns: tuple[tuple[str, tuple[str, ...]], ...]
    text_columns: tuple[str, ...] = ()
    static_properties: tuple[str, ...] = ()
    static_text: tuple[tuple[str, str], ...] = ()
    max_rows: int = DEFAULT_ROWS_PER_MAPPING

    def record(self) -> dict:
        return {
            "source": self.source, "relation": self.relation,
            "classes": self.classes, "id_columns": self.id_columns,
            "columns": self.columns, "text_columns": self.text_columns,
            "static_properties": self.static_properties, "static_text": self.static_text,
            "max_rows": self.max_rows,
        }


SOURCE_MAPPINGS = (
    SourceMapping("iana", "country_code", ("Country",), ("alpha2",), (
        ("alpha2", ("identifier",)), ("name", ("name",)),
    )),
    SourceMapping("iana", "zone", ("DefinedTerm",), ("timezone_id",), (
        ("timezone_id", ("termCode", "name")),
    )),
    SourceMapping("cldr", "territory_code", ("Country",), ("territory_code",), (
        ("territory_code", ("identifier",)), ("alpha3", ("alternateName",)),
    )),
    SourceMapping("cldr", "currency_code", ("DefinedTerm",), ("currency_code",), (
        ("currency_code", ("termCode", "name")), ("numeric_code", ("identifier",)),
    )),
    SourceMapping("cldr", "currency_name", ("DefinedTerm",),
                  ("locale", "currency_code", "plural_count", "alt"), (
        ("currency_code", ("termCode",)), ("name", ("name",)),
        ("locale", ("inLanguage",)),
    )),
    SourceMapping("cldr", "unit_conversion", ("DefinedTerm",), ("source_unit",), (
        ("source_unit", ("termCode", "name")), ("base_unit", ("isPartOf",)),
        ("description", ("description",)),
    )),
    SourceMapping("google_libphonenumber", "territory", ("DefinedTerm",),
                  ("territory_id", "country_calling_code"), (
        ("territory_id", ("termCode",)), ("country_calling_code", ("identifier",)),
    ), text_columns=("leading_digits", "international_prefix", "national_prefix")),
    SourceMapping("geonames", "place", ("Place", "GeoCoordinates"), ("geoname_id",), (
        ("geoname_id", ("identifier",)), ("name", ("name",)),
        ("ascii_name", ("alternateName",)), ("latitude", ("latitude",)),
        ("longitude", ("longitude",)), ("country_code", ("addressCountry",)),
        ("population", ("population",)), ("timezone_id", ("timezone",)),
        ("modified_on", ("dateModified",)),
    )),
    SourceMapping("geonames", "postal_code", ("PostalAddress", "GeoCoordinates"),
                  ("source_order",), (
        ("country_code", ("addressCountry",)), ("postal_code", ("postalCode",)),
        ("place_name", ("addressLocality", "name")),
        ("admin_name1", ("addressRegion",)), ("latitude", ("latitude",)),
        ("longitude", ("longitude",)),
    ), max_rows=8_000),
    SourceMapping("ecb", "exchange_rate", ("ExchangeRateSpecification", "UnitPriceSpecification"),
                  ("effective_date", "quote_currency"), (
        ("quote_currency", ("priceCurrency",)),
        ("units_per_eur", ("currentExchangeRate", "price")),
    ), text_columns=("effective_date",), static_properties=("currency",),
                  static_text=(("base_currency", "EUR"),)),
    SourceMapping("ec_tedb", "vat_rate", ("QuantitativeValue",), ("source_order",), (
        ("rate_percent", ("value",)), ("category_description", ("name",)),
        ("comment", ("description",)),
    ), text_columns=("member_state", "rate_class", "rate_type", "effective_date", "category_id"),
                  static_properties=("unitText",), static_text=(("unit", "percent"),)),
    SourceMapping("nager_date", "holiday", ("Event",), ("holiday_id",), (
        ("name", ("name",)), ("holiday_date", ("startDate", "endDate")),
        ("country_code", ("location",)),
    )),
    SourceMapping("cdc", "icd10cm_code", ("MedicalCode",), ("code",), (
        ("code", ("codeValue",)), ("description", ("name",)),
    ), text_columns=("parent_code", "effective_from", "effective_to"),
                  static_properties=("codingSystem", "inCodeSet"),
                  static_text=(("coding_system", "ICD-10-CM"),)),
    SourceMapping("nlm_cde", "cde", ("DefinedTerm",), ("tiny_id",), (
        ("tiny_id", ("termCode", "identifier")), ("preferred_name", ("name",)),
        ("preferred_definition", ("description",)),
    ), text_columns=("datatype", "steward_organization", "registration_status")),
    SourceMapping("nlm_cde", "form", ("DigitalDocument",), ("tiny_id",), (
        ("tiny_id", ("identifier",)), ("preferred_name", ("name",)),
        ("version", ("version",)),
    ), text_columns=("steward_organization", "registration_status")),
    SourceMapping("nlm_cde", "form_element", ("PropertyValueSpecification",),
                  ("form_tiny_id", "element_path"), (
        ("label", ("name",)), ("required", ("valueRequired",)),
        ("cde_tiny_id", ("identifier",)),
    ), text_columns=("element_type", "datatype"), max_rows=8_000),
)


def mapping_version(mappings=SOURCE_MAPPINGS) -> str:
    payload = json.dumps(
        {"schema_version": MAPPING_SCHEMA_VERSION,
         "mappings": [mapping.record() for mapping in mappings]},
        sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _active_release(cur, source: str) -> tuple[str, str] | None:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name='release')", (source,),
    )
    if not cur.fetchone()[0]:
        return None
    cur.execute(
        f'SELECT release_id, source_version FROM "{source}".release WHERE status=%s',
        ("active",),
    )
    return cur.fetchone()


def _column_names(mapping: SourceMapping) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        mapping.id_columns + tuple(name for name, _ in mapping.columns) + mapping.text_columns
    ))


def _chunks(rows: Iterable[dict], size: int = GROUP_SIZE):
    chunk = []
    for row in rows:
        chunk.append(row)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _source_columns(mapping: SourceMapping, rows: list[dict], contract: SchemaContract, *,
                    drop_static: bool, drop_text: bool) -> list[tuple[str, list[str], tuple[str, ...]]]:
    """(header, values, property URIs) per column, ready for facet packing.

    A `static_text` entry becomes a REAL column carrying the mapping's `static_properties`, so a label can
    never outlive the value that evidences it: previously `static_properties` were attached unconditionally
    while a variant that dropped the static text left e.g. a cdc instance labeled codingSystem/inCodeSet
    with 'ICD-10-CM' appearing nowhere in its text at all."""
    out: list[tuple[str, list[str], tuple[str, ...]]] = []
    if not drop_static:
        static = tuple(sorted({schema_uri(n) for n in mapping.static_properties}
                              & set(contract.properties)))
        for aux, value in mapping.static_text:
            out.append((aux.replace("_", " "), [str(value)], static))
            static = ()                              # attach the static labels once, to their own column
    declared = {column: properties for column, properties in mapping.columns}
    # PRESENTATION columns, deliberately not the SQL projection: _column_names includes id_columns so the
    # query can ORDER BY them, but several are synthetic ingest counters (`source_order`) that no real
    # upload contains. Rendering them teaches a value shape that will never be seen again.
    names = tuple(dict.fromkeys(
        tuple(c for c, _ in mapping.columns) + ((), mapping.text_columns)[not drop_text]
    ))
    for column in names:
        values = sample_values(row.get(column) for row in rows)
        if not values:
            continue
        uris = tuple(sorted({schema_uri(n) for n in declared.get(column, ())}
                            & set(contract.properties)))
        out.append((column.replace("_", " "), values, uris))
    return out


def source_instances(cur, contract: SchemaContract, *, mappings=SOURCE_MAPPINGS):
    """Whole-relation instances, FACETED so every label is evidenced inside the encoder's window.

    A single flat rendering of a wide relation blew past max_len=128 — ec_tedb texts ran a median of 372
    tokens and geonames 248 — so latitude/longitude/country/timezone were labeled on text the encoder never
    reads. That is wrong supervision, not weak: the head can only learn 'this table-name prefix means
    longitude', which then false-fires on the prefix and never fires on a real coordinate column. Facet 0
    keeps the whole-table shape (so the serving shape stays in distribution) and the remaining columns ride
    along in sibling facets that share the group."""
    version = mapping_version(mappings)
    for mapping in mappings:
        active = _active_release(cur, mapping.source)
        if active is None:
            continue
        release_id, _source_version = active
        columns = _column_names(mapping)
        quoted = ",".join(f'"{column}"' for column in columns)
        order = ",".join(f'"{column}"' for column in mapping.id_columns)
        cur.execute(
            f'SELECT {quoted} FROM "{mapping.source}"."{mapping.relation}" '
            f'WHERE release_id=%s ORDER BY {order} LIMIT %s',
            (release_id, mapping.max_rows),
        )
        rows = (dict(zip(columns, row)) for row in cur)
        for group in _chunks(rows):
            first_id = "/".join(str(group[0][c]) for c in mapping.id_columns)
            last_id = "/".join(str(group[-1][c]) for c in mapping.id_columns)
            # The group id is the SPLIT KEY and must be globally unique, so it carries the source: every
            # instance derived from these rows hangs off it after a '#'.
            base_id = f"{mapping.source}.{mapping.relation}:{first_id}..{last_id}"
            bucket = int(hashlib.sha256(("present\0" + base_id).encode("utf-8")).hexdigest()[:8], 16)
            renderings = [(base_id, f"{mapping.source}.{mapping.relation}", False, False)]
            if bucket % VARIANT_EVERY == 0:
                # PRESENTATION VARIANTS of the same rows, with table name and auxiliary columns varied
                # INDEPENDENTLY. Coupling them (canonical+aux vs renamed+no-aux) leaves the combination
                # "renamed table that still carries its aux column" absent from training, and the head then
                # keys the aux column's property on the table name instead of the column: `codingSystem`
                # fired 1.000 on `table cdc.icd10cm code | coding system: ICD-10-CM` and 0.001 on
                # `table diagnosis codes | coding system: ICD-10-CM` — same column, different table name.
                # THREE INDEPENDENT AXES: table name, publisher annotations (static_text), and context
                # columns (text_columns). Varying them together leaves real combinations untrained — a user
                # export of FX rates is renamed, keeps its date column, and carries no "base currency: EUR"
                # annotation, and that exact shape had never been seen. Emit the combinations a real upload
                # actually takes, all sharing the parent group so none can leak.
                bare = mapping.relation.replace("_", " ")
                variant_name = (bare, bare if bare.endswith("s") else bare + "s")[(bucket >> 2) % 2]
                renderings.append((base_id + "#v1", variant_name, True, True))    # renamed, bare columns
                renderings.append((base_id + "#v2", variant_name, False, False))  # renamed, everything kept
                renderings.append((base_id + "#v3", variant_name, True, False))   # renamed, context kept,
                                                                                  # annotations dropped
            for instance_id, table_name, drop_static, drop_text in renderings:
                packed = _source_columns(mapping, group, contract,
                                         drop_static=drop_static, drop_text=drop_text)
                for index, facet in enumerate(_facets(table_name, [], packed)):
                    if not facet:
                        continue
                    suffix = "" if index == 0 else (
                        f"#facet={index}" if "#" not in instance_id else f"+facet={index}")
                    instance = emit_instance(
                        columns=facet, text=_facet_text(table_name, facet), contract=contract,
                        source=mapping.source, release_id=release_id,
                        relation=f"{mapping.source}.{mapping.relation}",
                        instance_id=instance_id + suffix,
                        classes=mapping.classes, mapping_version=version,
                    )
                    if instance is not None:
                        yield instance


def _bridge_properties(path: str | Path, contract: SchemaContract) -> dict[str, frozenset[str]]:
    """Wikidata P-id -> the SET of schema.org properties it maps to, filtered against the ontology contract.

    Set-valued on purpose. The bridge legitimately carries several targets for one P-id (P571 is both
    startDate and dateCreated), and multi-target is already the convention in SOURCE_MAPPINGS
    (holiday_date -> startDate+endDate). A last-wins dict silently destroyed one target per conflicting
    P-id — and worse, let an INVALID target shadow a valid one: P625 -> GeoCoordinates (a schema.org
    class, rejected by the contract) shadowed P625 -> geo, so the most common geo property in the
    snapshot contributed nothing at all. Filtering here means an invalid target can never shadow."""
    import csv
    from collections import defaultdict
    found = defaultdict(set)
    with Path(path).open(encoding="utf-8") as source:
        for row in csv.reader(source):
            if len(row) < 2:
                continue
            uri = schema_uri(row[1])
            if uri in contract.properties:                  # a class or unknown term never enters the map
                found[row[0].rsplit("/", 1)[-1]].add(uri)
    return {pid: frozenset(targets) for pid, targets in found.items()}


@dataclass(frozen=True)
class WikidataMapping:
    """A populated capped type -> the schema.org classes it exemplifies. `classes=()` is a deliberate
    LABELED NEGATIVE pool: real entities that are none of the ontology's classes."""
    pool: str                                   # ID-safe pool name (enters relation + instance ids)
    type_qid: str
    classes: tuple[str, ...] = ()


# The corpus owns this mapping. It previously borrowed engine/data/families.json, which is owned by the
# 9-family router and keyed on canonical schema.org-aligned QIDs — but the capped snapshot was loaded
# against different LEAF qids, so 17 of its 27 classes resolved to ZERO entities and were silently skipped
# (source_adapters.py's zero-count guard). That hole is why entity properties starved. Every entry below is
# verified non-empty in capped.entity_type.
WIKIDATA_MAPPINGS = (
    WikidataMapping("Movie", "Q11424", ("Movie",)),
    WikidataMapping("MusicGroup", "Q215380", ("MusicGroup",)),
    WikidataMapping("School", "Q3914", ("School",)),
    WikidataMapping("Taxon", "Q16521", ("Taxon",)),
    WikidataMapping("Hospital", "Q16917", ("Hospital",)),
    WikidataMapping("PoliticalParty", "Q7278", ("PoliticalParty",)),
    WikidataMapping("WebSite", "Q35127", ("WebSite",)),
    WikidataMapping("AdministrativeArea", "Q56061", ("AdministrativeArea",)),
    WikidataMapping("City", "Q515", ("City",)),
    WikidataMapping("Country", "Q6256", ("Country",)),
    # remapped to the leaf qids the snapshot actually populated
    WikidataMapping("Street", "Q79007", ("Place",)),
    WikidataMapping("Neighborhood", "Q123705", ("Place",)),
    WikidataMapping("AcademicJournal", "Q737498", ("Periodical",)),
    WikidataMapping("Song", "Q7366", ("MusicRecording",)),
    WikidataMapping("University", "Q3918", ("CollegeOrUniversity",)),
    WikidataMapping("Software", "Q7397", ("SoftwareApplication",)),
    WikidataMapping("CarModel", "Q3231690", ("Product",)),        # a car MODEL is a product spec, not a Vehicle
    WikidataMapping("Restaurant", "Q11707", ("Restaurant",)),
    WikidataMapping("Language", "Q34770", ("Language",)),
    WikidataMapping("Bank", "Q22687", ("BankOrCreditUnion",)),
    WikidataMapping("SportsTeam", "Q12973014", ("SportsTeam",)),
    WikidataMapping("SportsOrganization", "Q4438121", ("SportsOrganization",)),
    WikidataMapping("SkiResort", "Q130003", ("SkiResort",)),
    # labeled negatives: real, well-populated entities with no honest schema.org class
    WikidataMapping("Horse", "Q726"),
    WikidataMapping("PowerStation", "Q159719"),
    WikidataMapping("AcademicDiscipline", "Q11862829"),
    WikidataMapping("LegalForm", "Q10541491"),
    WikidataMapping("Industry", "Q268592"),
)

_GENERIC_EVIDENCE: dict[str, frozenset[str]] = {}


def generic_evidence(contract: SchemaContract) -> frozenset[str]:
    """Properties that cannot ESTABLISH any class, DERIVED from the ontology rather than hand-listed.

    A property whose only declared domain is Thing applies to every entity in the vocabulary, so a facet
    showing nothing else is not evidence of a class: labeling it with the entity's class creates a positive
    the model cannot recall, and teaches the signature builder that universal properties are distinctive.
    Both happened — WebSite's learned signature was literally description(0.86) + name(0.85), and Place
    recall fell to 0.02 because 41% of its positives were name-and-description-only facets.

    This was a hand-picked set of six, and the omission it invited duly appeared: `identifier` is also
    domain-Thing, so 2,785 facets asserted PropertyValueSpecification / GeoCoordinates / Place / Country on
    an opaque id column alone — the very pathology the set exists to prevent. Asking the ontology yields
    those six plus identifier, additionalType, disambiguatingDescription, owner, potentialAction, sameAs and
    subjectOf, and stays correct when the vocabulary changes."""
    # emit_instance calls this once per candidate instance (~50k per build) and the scan is over 1,521
    # properties, so memoize on the contract identity (SchemaContract itself is unhashable).
    cached = _GENERIC_EVIDENCE.get(contract.contract_sha256)
    if cached is None:
        cached = _GENERIC_EVIDENCE[contract.contract_sha256] = frozenset(
            uri for uri in contract.property_order
            if tuple(contract.properties[uri].domains) == (schema_uri("Thing"),)
        )
    return cached

PER_PROPERTY_ENTITIES = 100     # per (pool, P-id): 10% validation share x 100 = ~10 = 2x the floor of 5
BASELINE_ENTITIES = 100         # per pool, qid-ordered: guarantees class-shape coverage independent of props
MAX_VALUES_PER_PROPERTY = 3
MAX_FACETS = 3
ENCODER_BUDGET_TOKENS = 112     # headroom under the encoder's max_len=128 (training AND serving)

_QID = re.compile(r"^Q\d+$")
_WD_TIME = re.compile(r"^([+-])(\d{4,})-(\d\d)-(\d\d)T")
_WD_QUANTITY = re.compile(r"^[+-]\d+(\.\d+)?$")


def _domain_ok(targets, classes, contract: SchemaContract) -> tuple[str, ...]:
    """Keep only bridge targets whose schema.org DOMAIN admits at least one of the instance's classes.

    Making the bridge set-valued stopped one target silently shadowing another, but it also means a P-id
    with two mappings now emits BOTH — and some are domain-wrong. P276 maps to `location` and to
    `itemLocation` (domain ArchiveComponent); emitting the latter on a Hospital or Restaurant is simply a
    false label, and because the two always co-occur no calibration can separate them, so the wrong one
    inherits the right one's precision. The ontology already knows which is admissible, so ask it.
    Class-free instances have nothing to check against and keep every target."""
    if not classes:
        return tuple(sorted(targets))
    admissible = set()
    for class_name in classes:
        # normalise HERE. Callers hold bare names in some paths and URIs in others, and a raw
        # `contract.classes[bare_name]` miss combined with the permissive fallback below turned this whole
        # filter into a silent no-op: it returned every target unfiltered, emitted `itemLocation` (domain
        # ArchiveComponent) on 1,091 Hospital/Place instances, and inflated the trained basis by 20
        # dimensions that had no admissible class. A lookup miss must never be indistinguishable from
        # "nothing to filter".
        item = contract.classes.get(schema_uri(class_name))
        if item is None:
            raise ValueError(f"unknown class in domain filter: {class_name!r}")
        admissible.update(item.compatible_properties)
    if not admissible:                      # class genuinely declares no compatible properties
        return tuple(sorted(targets))
    kept = tuple(sorted(target for target in targets if target in admissible))
    DROPPED["label_domain_inadmissible"] += len(set(targets)) - len(kept)
    return kept


def _label_map(cur) -> dict[str, str]:
    """qid -> human label, unioned across the snapshot's label-bearing tables. Used to render QID-valued
    properties as readable text; a value that cannot be resolved is DROPPED rather than shown as 'Q1234',
    because teaching an opaque identifier is memorising junk."""
    cur.execute(
        "SELECT qid,label FROM capped.entity WHERE label IS NOT NULL "
        "UNION SELECT qid,label FROM capped.type WHERE label IS NOT NULL"
    )
    return {qid: label for qid, label in cur.fetchall()}


def _property_headers(cur) -> dict[str, str]:
    """P-id -> the publisher's own property name ('P105' -> 'taxon rank'). Deriving the header from the
    schema.org target instead would be circular supervision: the head could read the answer off the header."""
    cur.execute("SELECT DISTINCT property_id, property_name FROM capped.type_property "
                "WHERE property_name IS NOT NULL")
    return {pid: name for pid, name in cur.fetchall()}


def _render_values(raw, labels: dict[str, str]) -> list[str]:
    """Render a Wikidata property value list into readable strings a real upload could plausibly contain.

    Wikidata's storage syntax is not a value shape any user uploads, and teaching it wastes capacity on
    tokens the model will never see again: reduced-precision timestamps are stored as +1949-00-00T00:00:00Z
    (month and day literally 00, which no date parser accepts), and quantities carry an explicit sign
    (+2, +692300). Render the meaning, drop what cannot be rendered honestly."""
    out = []
    for value in (raw or ())[:8]:
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if _QID.fullmatch(text):
            resolved = labels.get(text)
            if not resolved:
                DROPPED["value_unresolvable_qid"] += 1
                continue                                   # unresolvable QID -> drop, never teach the id
            text = resolved
        else:
            stamp = _WD_TIME.match(text)
            if stamp:
                sign, year, month, day = stamp.group(1), stamp.group(2), stamp.group(3), stamp.group(4)
                if len(year) > 4 or sign == "-":
                    DROPPED["value_out_of_range_date"] += 1
                    continue           # geological precision, or BCE (no upload carries a negative year,
                                       # and dropping the sign would render 900 BCE as 0900 CE)
                text = (f"{year}-{month}-{day}" if month != "00" and day != "00"
                        else f"{year}-{month}" if month != "00" else year)
            elif _WD_QUANTITY.fullmatch(text):
                text = text.lstrip("+")                    # '+692300' -> '692300'
        text = text.replace("\n", " ").strip()[:160]
        if text and text not in out:
            out.append(text)
        if len(out) == MAX_VALUES_PER_PROPERTY:
            break
    return out


_TOKENIZER: list = []                  # [tokenizer] once loaded

# Every bounded sweep in this module discards data. CLAUDE.md requires that a bound LOG what it dropped —
# silent truncation reads as "covered everything" when it did not. These counters are read by
# corpus.build() into the manifest so each cap has a denominator.
DROPPED: Counter = Counter()


def reset_drop_counts() -> None:
    DROPPED.clear()


def drop_counts() -> dict:
    return dict(sorted(DROPPED.items()))


def _visible(text: str) -> str:
    """The prefix of `text` the encoder actually reads.

    Truncation is by TOKENS (max_len=128 in both training and serving), and the char/token ratio swings
    with content — numeric-heavy rows run ~1.65 — so a character budget is only a proxy. Use the real
    tokenizer when it is available and fall back to the proxy offline."""
    if not _TOKENIZER:
        try:
            from transformers import AutoTokenizer
            _TOKENIZER.append(AutoTokenizer.from_pretrained("Qwen/Qwen2.5-0.5B"))
        except Exception as exc:                                # a corpus is a training artifact, never a
            raise RuntimeError(                                 # serving path: silently switching to a
                "the evidence gate needs the real tokenizer (Qwen/Qwen2.5-0.5B) — a character proxy "
                "produces a DIFFERENT corpus on a machine without it, so builds stop being reproducible"
            ) from exc
    tokenizer = _TOKENIZER[0]
    return tokenizer.decode(tokenizer(text, truncation=True, max_length=ENCODER_BUDGET_TOKENS)["input_ids"])


def emit_instance(*, columns, text: str, contract: SchemaContract, **fields):
    """The ONE way to construct a semantic instance. Returns the instance, or None if it is not emittable.

    Every emitter must come through here, because the evidence invariant is only checkable while the
    property's VALUES are still in hand — and a rule enforced by convention at each call site is a rule
    that gets forgotten. It was: the gate was added to three new emitters and omitted from the oldest one,
    which then labeled half its instances with properties the encoder could never read (`longitude` was
    91% blind) while reporting perfect held-out precision, because the head had learned the table-name
    prefix instead. Passing `columns` is mandatory, so a new emitter cannot silently skip the check."""
    if not _evidence_ok(text, columns):
        DROPPED["instance_unevidenced"] += 1
        return None
    properties = {uri for _header, _values, uris in columns for uri in uris}
    if not properties:
        DROPPED["instance_no_mapped_property"] += 1
        return None                                  # a facet of unmapped columns teaches nothing
    # A CLASS must be evidenced too, not just carried along from the row it came from. Splitting a wide
    # relation into facets gives each facet a partial view, and asserting the class on all of them creates
    # positives the model cannot possibly recall: `cdc.icd10cm_code#facet=1` showed a column of disease
    # descriptions and claimed MedicalCode, with no code and no coding system in the text. 41% of cdc
    # positives were that shape, and class recall fell to 0.60; for Place it fell to 0.02. Worse, those
    # instances teach the signature builder that `name`/`description` are class-distinctive — WebSite's
    # learned signature became literally description + name. A facet keeps its class only when it shows
    # something beyond the generic surface properties every entity has.
    if not (properties - generic_evidence(contract)):
        if fields.get("classes"):
            DROPPED["class_stripped_generic_only"] += 1
        fields = {**fields, "classes": ()}
    instance = SemanticInstance.create(text=text, properties=properties, **fields)
    instance.validate(contract)
    return instance


def _evidence_ok(text: str, columns) -> bool:
    """Every labeled property must have at least one of its values inside the part of the text the encoder
    actually reads. This is the invariant that makes the whole change worth doing: the previous corpus
    labeled instances with properties whose values appeared nowhere in the text, and — separately — the
    encoder truncates at max_len=128, so even rendered values past the budget were invisible (ec_tedb texts
    median 372 tokens, geonames 248). Wrong supervision either way."""
    visible = _visible(text)
    return all(any(value in visible for value in values) for _header, values, _props in columns if values)


def _fits(text: str) -> bool:
    """Does the whole rendered text survive the encoder's window? Pack against the budget we gate on."""
    if not _TOKENIZER:
        _visible("")                                            # prime the lazy tokenizer load (or raise)
    return len(_TOKENIZER[0](text)["input_ids"]) <= ENCODER_BUDGET_TOKENS


def _facets(header: str, base_columns, property_columns):
    """Pack property columns into <=MAX_FACETS renderable groups that each fit the encoder budget, with the
    identity columns (name/description) repeated in every facet so each facet stands alone."""
    facets, current = [], []
    for column in property_columns:
        candidate = current + [column]
        if current and not _fits(_facet_text(header, base_columns + candidate)):
            facets.append(current)
            current = [column]
        else:
            current = candidate
        if len(facets) == MAX_FACETS:
            DROPPED["columns_beyond_max_facets"] += len(property_columns) - property_columns.index(column)
            break
    if current and len(facets) < MAX_FACETS:
        facets.append(current)
    return facets or ([[]] if base_columns else [])


def _facet_text(header: str, columns) -> str:
    from engine.schema_model import summarize_table                  # the SERVING renderer; never re-render
    names = [name for name, _values, _props in columns]
    depth = max((len(values) for _n, values, _p in columns), default=0)
    rows = [[(values[i] if i < len(values) else None) for _n, values, _p in columns] for i in range(depth)]
    return summarize_table({"name": header, "columns": names, "rows": rows})


def wikidata_instances(cur, contract: SchemaContract, *, bridge_path: str | Path,
                       mappings=WIKIDATA_MAPPINGS):
    """Project the capped Wikidata snapshot into per-ENTITY semantic instances, without mutating it.

    Per-entity, not per-8-entity-group: a coarse instance spanning entities that each also become instances
    has no single split ancestor. Selection is property-STRATIFIED — a qid-ordered LIMIT over a 50k-row class
    saw only the alphabetical head and starved every property outside it."""
    bridge = _bridge_properties(bridge_path, contract)
    labels = _label_map(cur)
    headers = _property_headers(cur)
    version = mapping_version()
    name_uri, description_uri = schema_uri("name"), schema_uri("description")
    selected: dict[str, dict] = {}
    for mapping in mappings:
        cur.execute(
            "SELECT e.qid,e.label,e.description,e.properties FROM capped.entity e "
            "JOIN capped.entity_type t ON t.entity_qid=e.qid WHERE t.type_qid=%s ORDER BY e.qid",
            (mapping.type_qid,),
        )
        per_property, baseline = Counter(), 0
        for qid, label, description, properties in cur:
            keep = False
            if baseline < BASELINE_ENTITIES:
                baseline += 1
                keep = True
            for pid in sorted(properties or {}):
                if pid in bridge and per_property[pid] < PER_PROPERTY_ENTITIES:
                    if _render_values((properties or {}).get(pid), labels):
                        per_property[pid] += 1
                        keep = True
            if not keep:
                continue
            entry = selected.get(qid)
            if entry is None:                       # dedup by QID across pools; a multi-typed entity gets
                selected[qid] = {                   # ONE instance (and one split draw), classes merged
                    "qid": qid, "label": label, "description": description,
                    "properties": properties or {}, "pool": mapping.pool,
                    "classes": set(mapping.classes),
                }
            else:
                entry["classes"].update(mapping.classes)

    for qid in sorted(selected):
        entry = selected[qid]
        base_columns = []
        label = str(entry["label"] or "").strip()
        if not label or _QID.fullmatch(label):
            continue                                                  # no readable name -> nothing to learn
        base_columns.append(("name", [label[:160]], (name_uri,)))
        description = str(entry["description"] or "").strip()
        if description:
            base_columns.append(("description", [description[:160]], (description_uri,)))
        property_columns = []
        for pid in sorted(entry["properties"], key=lambda p: int(p[1:]) if p[1:].isdigit() else 0):
            targets = bridge.get(pid)
            if not targets:
                continue
            values = _render_values(entry["properties"][pid], labels)
            if values:
                admissible = _domain_ok(targets, entry["classes"], contract)
                if admissible:
                    property_columns.append((headers.get(pid, pid), values, admissible))
        header = f"wikidata {entry['pool']}"
        for index, facet in enumerate(_facets(header, base_columns, property_columns)):
            columns = base_columns + facet
            instance = emit_instance(
                columns=columns, text=_facet_text(header, columns), contract=contract,
                source="wikidata", release_id="capped.entity:live-audited",
                relation=f"wikidata.{entry['pool']}",
                instance_id=f"wikidata:{qid}" + (f"#facet={index}" if index else ""),
                classes=tuple(sorted(entry["classes"])), mapping_version=version,
            )
            if instance is not None:
                yield instance


COLUMN_EVERY = 8                    # 1 in N source row-groups also emits per-column instances
WIKIDATA_COLUMNS_PER_PROPERTY = 6   # cap per (pool, P-id); values drawn from disjoint entity blocks
COLUMN_BLOCK = MAX_SAMPLE_VALUES    # entities per column instance == what summarize_table renders
ANON_EVERY = 4                      # 1 in N wikidata column instances also gets a header-anonymised twin


def _column_text(table_name: str, header: str, values) -> str:
    from engine.schema_model import summarize_table                  # SERVING renderer; identical text
    return summarize_table({"name": table_name, "columns": [header], "rows": [[v] for v in values]})


def source_column_instances(cur, contract: SchemaContract, *, mappings=SOURCE_MAPPINGS):
    """One instance per (sampled row-group, mapped column) — the granularity the old family router types.

    Labels are CERTAIN: SourceMapping already declares each column's property tuple, so no inference is
    needed. Class-free by design: one distinctive column is not enough to assert a class, and these are
    exactly the negatives that teach that. They share their parent group, so they cannot leak."""
    version = mapping_version()
    for mapping in mappings:
        active = _active_release(cur, mapping.source)
        if active is None:
            continue
        release_id, _source_version = active
        columns = _column_names(mapping)
        quoted = ",".join(f'"{column}"' for column in columns)
        order = ",".join(f'"{column}"' for column in mapping.id_columns)
        cur.execute(
            f'SELECT {quoted} FROM "{mapping.source}"."{mapping.relation}" '
            f'WHERE release_id=%s ORDER BY {order} LIMIT %s',
            (release_id, mapping.max_rows),
        )
        rows = (dict(zip(columns, row)) for row in cur)
        for group in _chunks(rows):
            first_id = "/".join(str(group[0][c]) for c in mapping.id_columns)
            last_id = "/".join(str(group[-1][c]) for c in mapping.id_columns)
            base_id = f"{mapping.source}.{mapping.relation}:{first_id}..{last_id}"
            if int(hashlib.sha256(("col\0" + base_id).encode("utf-8")).hexdigest()[:8], 16) % COLUMN_EVERY:
                continue
            for column, properties in mapping.columns:
                targets = tuple(sorted({schema_uri(n) for n in properties} & set(contract.properties)))
                if not targets:
                    continue
                # limit=len(group): a source COLUMN instance may show every row of its group, unlike a table
                # summary which caps each column at MAX_SAMPLE_VALUES.
                values = sample_values((row.get(column) for row in group), limit=len(group))
                if len(values) < 2:
                    continue
                instance = emit_instance(
                    columns=[(column, values, targets)], contract=contract,
                    text=_column_text(f"{mapping.source}.{mapping.relation}", column, values),
                    source=f"{mapping.source}_columns", release_id=release_id,
                    relation=f"{mapping.source}.{mapping.relation}.{column}",
                    instance_id=f"{base_id}#col={column}",
                    classes=(), mapping_version=version,
                )
                if instance is not None:
                    yield instance


def wikidata_column_instances(cur, contract: SchemaContract, *, bridge_path: str | Path,
                              mappings=WIKIDATA_MAPPINGS):
    """Per-property COLUMN instances from the snapshot: a column of real values for one Wikidata property.

    This is what teaches the head to read a column of dates as birthDate or a column of ranks as taxonRank —
    the per-entity instances alone only ever show one value per property."""
    bridge = _bridge_properties(bridge_path, contract)
    labels = _label_map(cur)
    headers = _property_headers(cur)
    version = mapping_version()
    for mapping in mappings:
        cur.execute(
            "SELECT e.qid,e.properties FROM capped.entity e JOIN capped.entity_type t "
            "ON t.entity_qid=e.qid WHERE t.type_qid=%s ORDER BY e.qid",
            (mapping.type_qid,),
        )
        pending: dict[str, list] = {}
        emitted = Counter()
        for qid, properties in cur:
            for pid in sorted(properties or {}):
                if pid not in bridge or emitted[pid] >= WIKIDATA_COLUMNS_PER_PROPERTY:
                    continue
                rendered = _render_values((properties or {}).get(pid), labels)
                if not rendered:
                    continue
                block = pending.setdefault(pid, [])
                if rendered[0] not in block:
                    block.append(rendered[0])
                if len(block) < COLUMN_BLOCK:
                    continue
                index = emitted[pid]
                emitted[pid] += 1
                pending[pid] = []
                targets = _domain_ok(bridge[pid],
                                     {schema_uri(c) for c in mapping.classes}, contract)
                if not targets:
                    continue
                header = headers.get(pid, pid)
                group = f"wikidata:{mapping.pool}:{pid}:{index:03d}"
                instance = emit_instance(
                    columns=[(header, block, targets)], contract=contract,
                    text=_column_text(f"wikidata {mapping.pool}", header, block),
                    source="wikidata_columns", release_id="capped.entity:live-audited",
                    relation=f"wikidata.{mapping.pool}.{pid}", instance_id=group,
                    classes=(), mapping_version=version,
                )
                if instance is None:
                    continue
                yield instance
                if int(hashlib.sha256(group.encode("utf-8")).hexdigest()[:8], 16) % ANON_EVERY == 0:
                    # ANTI-SHORTCUT twin: a wikidata column header is a natural-language property label, so
                    # the head could learn header->URI and ignore the values entirely. The twin removes the
                    # header and shares the group, so it cannot leak.
                    twin = emit_instance(
                        columns=[("value", block, targets)], contract=contract,
                        text=_column_text(f"wikidata {mapping.pool}", "value", block),
                        source="wikidata_columns", release_id="capped.entity:live-audited",
                        relation=f"wikidata.{mapping.pool}.{pid}", instance_id=f"{group}#anon",
                        classes=(), mapping_version=version,
                    )
                    if twin is not None:
                        yield twin


def active_source_manifest(cur, *, mappings=SOURCE_MAPPINGS) -> dict:
    sources = sorted({mapping.source for mapping in mappings})
    active = {}
    absent = []
    for source in sources:
        row = _active_release(cur, source)
        if row is None:
            absent.append(source)
        else:
            active[source] = {"release_id": row[0], "source_version": row[1]}
    return {
        "schema_version": 1, "mapping_version": mapping_version(mappings),
        "active_sources": active, "absent_sources": absent,
        "mappings": [mapping.record() for mapping in mappings],
    }
