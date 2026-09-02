"""Declarative publisher and Wikidata mappings for the Schema.org corpus."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

MAPPING_SCHEMA_VERSION = 2
DEFAULT_ROWS_PER_MAPPING = 5_000


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


@dataclass(frozen=True)
class WikidataMapping:
    pool: str
    type_qid: str
    classes: tuple[str, ...] = ()


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
    WikidataMapping("Street", "Q79007", ("Place",)),
    WikidataMapping("Neighborhood", "Q123705", ("Place",)),
    WikidataMapping("AcademicJournal", "Q737498", ("Periodical",)),
    WikidataMapping("Song", "Q7366", ("MusicRecording",)),
    WikidataMapping("University", "Q3918", ("CollegeOrUniversity",)),
    WikidataMapping("Software", "Q7397", ("SoftwareApplication",)),
    WikidataMapping("CarModel", "Q3231690", ("Product",)),
    WikidataMapping("Restaurant", "Q11707", ("Restaurant",)),
    WikidataMapping("Language", "Q34770", ("Language",)),
    WikidataMapping("Bank", "Q22687", ("BankOrCreditUnion",)),
    WikidataMapping("SportsTeam", "Q12973014", ("SportsTeam",)),
    WikidataMapping("SportsOrganization", "Q4438121", ("SportsOrganization",)),
    WikidataMapping("SkiResort", "Q130003", ("SkiResort",)),
    WikidataMapping("Horse", "Q726"),
    WikidataMapping("PowerStation", "Q159719"),
    WikidataMapping("AcademicDiscipline", "Q11862829"),
    WikidataMapping("LegalForm", "Q10541491"),
    WikidataMapping("Industry", "Q268592"),
)


def mapping_version(mappings=SOURCE_MAPPINGS) -> str:
    payload = json.dumps(
        {"schema_version": MAPPING_SCHEMA_VERSION,
         "mappings": [mapping.record() for mapping in mappings]},
        sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
