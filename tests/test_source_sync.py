"""Hermetic parser and validation tests for source-owned reference synchronization.

Run: python -m tests.test_source_sync
"""
from __future__ import annotations

import io
import sys
import tarfile
import zipfile
import json
import os
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from db.sync.sources.cdc.sync import parse_archive as parse_cdc
from db.sync.sources.cldr.sync import parse_archive as parse_cldr
from db.sync.sources.cldr.sync import validate as validate_cldr
from db.sync.sources.ec_tedb.sync import parse_snapshot as parse_tedb
from db.sync.sources.ecb.sync import parse_archive as parse_ecb
from db.sync.sources.google_libphonenumber.sync import parse_archive as parse_libphone
from db.sync.sources.geonames.sync import parse_archives as parse_geonames
from db.sync.sources.iana.sync import ZONE_SOURCE_FILES
from db.sync.sources.iana.sync import parse_archive as parse_iana
from db.sync.sources.nager_date.sync import parse_snapshot as parse_nager
from db.sync.sources.nlm_cde.sync import parse_snapshot as parse_nlm
from db.sync.sources.loinc.sync import parse_archive as parse_loinc
from db.sync.sources.who.sync import parse_snapshot as parse_who
from db.sync.migrations import MIGRATIONS, latest_schema_version
from db.sync._conn import _connection_kwargs
from db.sync.releases import activate_validated_release
from db.reference_grants import approved_reference_targets


def _iana_archive(*, unknown_zone: bool = False) -> bytes:
    french_zone = "Europe/Limbo" if unknown_zone else "Europe/Paris"
    files = {
        "iso3166.tab": "FR\tFrance\nUS\tUnited States\n",
        "zone1970.tab": (
            f"FR\t+4852+00220\t{french_zone}\n"
            "US\t+404251-0740023\tAmerica/New_York\tEastern\n"
        ),
        "backward": "Link\tEurope/Paris\tEurope/Monaco-Old\n",
    }
    files.update({name: "" for name in ZONE_SOURCE_FILES})
    files["europe"] = "Zone\tEurope/Paris\t0:09:21\t-\tLMT\t1891\n"
    files["northamerica"] = "Zone America/New_York -4:56:02 - LMT 1883\n"
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w:gz") as archive:
        for name, content in files.items():
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return target.getvalue()


def _cldr_archive() -> bytes:
    supplemental = """<supplementalData>
      <codeMappings>
        <territoryCodes type="FR" numeric="250" alpha3="FRA" fips10="FR"/>
        <currencyCodes type="EUR" numeric="978"/>
      </codeMappings>
      <currencyData>
        <fractions><info iso4217="DEFAULT" digits="2" rounding="0"/></fractions>
        <region iso3166="FR"><currency iso4217="EUR" from="1999-01-01"/></region>
      </currencyData>
    </supplementalData>"""
    metadata = """<supplementalData><metadata><alias>
      <territoryAlias type="FX" replacement="FR" reason="deprecated"/>
    </alias></metadata></supplementalData>"""
    units = """<supplementalData>
      <unitPrefixes><unitPrefix type="kilo" symbol="k" power10="3"/></unitPrefixes>
      <unitConstants><unitConstant constant="ft_to_m" value="0.3048"/></unitConstants>
      <unitQuantities><unitQuantity baseUnit="meter" quantity="length" status="simple"/></unitQuantities>
      <convertUnits><convertUnit source="foot" baseUnit="meter" factor="ft_to_m" systems="ussystem"/></convertUnits>
      <unitPreferenceData><unitPreferences category="length" usage="default">
        <unitPreference regions="001">meter</unitPreference>
      </unitPreferences></unitPreferenceData>
      <metadata><alias><unitAlias type="metre" replacement="meter" reason="deprecated"/></alias></metadata>
    </supplementalData>"""
    locale = """<ldml>
      <localeDisplayNames><territories><territory type="FR">France</territory></territories></localeDisplayNames>
      <numbers><currencies><currency type="EUR">
        <displayName>Euro</displayName><displayName count="one">euro</displayName><symbol>EUR</symbol>
      </currency></currencies></numbers>
    </ldml>"""
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        root = "cldr-release-test"
        archive.writestr(f"{root}/common/supplemental/supplementalData.xml", supplemental)
        archive.writestr(f"{root}/common/supplemental/supplementalMetadata.xml", metadata)
        archive.writestr(f"{root}/common/supplemental/units.xml", units)
        archive.writestr(f"{root}/common/main/en.xml", locale)
    return target.getvalue()


def test_iana_parser_preserves_source_boundaries():
    data = parse_iana(_iana_archive(), enforce_minimums=False)
    assert data.countries == (("FR", "France"), ("US", "United States"))
    assert data.zones == ("America/New_York", "Europe/Paris")
    assert data.aliases == (("Europe/Monaco-Old", "Europe/Paris"),)
    assert ("FR", "Europe/Paris") in data.country_zones
    assert ("America/New_York", "+404251-0740023", "Eastern") in data.locations


def test_iana_parser_rejects_unknown_zone():
    try:
        parse_iana(_iana_archive(unknown_zone=True), enforce_minimums=False)
        raise AssertionError("expected unknown-zone validation failure")
    except ValueError as exc:
        assert "unknown canonical timezone" in str(exc)


def test_cldr_parser_keeps_codes_displays_temporal_rows_and_units_separate():
    data = parse_cldr(_cldr_archive(), enforce_minimums=False)
    assert data.territory_codes == (("FR", "250", "FRA", "FR"),)
    assert data.territory_names == (("en", "FR", "", "", "France"),)
    assert data.currency_codes == (("EUR", "978"),)
    assert len(data.currency_names) == 2 and data.currency_symbols[0][-1] == "EUR"
    assert data.territory_currencies == (("FR", 0, "EUR", date(1999, 1, 1), None, True),)
    assert data.unit_conversions[0][:3] == ("foot", "meter", "ft_to_m")
    assert data.unit_preferences[0][-1] == "meter"


def test_cldr_validation_rejects_duplicate_semantic_keys():
    data = parse_cldr(_cldr_archive(), enforce_minimums=False)
    duplicate = replace(data, territory_names=data.territory_names + data.territory_names)
    try:
        validate_cldr(duplicate, enforce_minimums=False)
        raise AssertionError("expected duplicate-key validation failure")
    except ValueError as exc:
        assert "territory-name" in str(exc)


def _zip_file(name: str, content: str) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return target.getvalue()


def test_ecb_parser_expands_non_missing_rates_only():
    archive = _zip_file(
        "eurofxref-hist.csv", "Date,USD,JPY,\n2026-01-02,1.2,180,\n2026-01-01,N/A,179,\n"
    )
    data = parse_ecb(archive, enforce_minimums=False)
    assert data.rates == (
        ("2026-01-02", "USD", Decimal("1.2")),
        ("2026-01-02", "JPY", Decimal("180")),
        ("2026-01-01", "JPY", Decimal("179")),
    )


def test_cdc_parser_preserves_hierarchy_and_metadata():
    xml = """<ICD10CM.tabular><chapter><section><diag>
      <name>A00</name><desc>Parent</desc><includes><note>Included</note></includes>
      <diag><name>A00.0</name><desc>Leaf</desc></diag>
    </diag></section></chapter></ICD10CM.tabular>"""
    data = parse_cdc(_zip_file("icd10cm-tabular.xml", xml), enforce_minimums=False)
    assert data.codes[0][:6] == ("A00", "Parent", "", 0, 1, False)
    assert data.codes[1][:6] == ("A00.0", "Leaf", "A00", 1, 2, True)
    assert json.loads(data.codes[0][-1]) == {"includes": {"note": "Included"}}


def test_libphonenumber_parser_uses_calling_code_for_non_geographic_keys():
    xml = """<phoneNumberMetadata><territories>
      <territory id="001" countryCode="800" internationalPrefix="00">
        <generalDesc><possibleLengths national="8"/><nationalNumberPattern>\\d{8}</nationalNumberPattern></generalDesc>
      </territory>
      <territory id="001" countryCode="808" internationalPrefix="00">
        <generalDesc><possibleLengths national="8"/><nationalNumberPattern>\\d{8}</nationalNumberPattern></generalDesc>
      </territory>
    </territories></phoneNumberMetadata>"""
    archive = _zip_file("x/resources/PhoneNumberMetadata.xml", xml)
    data = parse_libphone(archive, enforce_minimums=False)
    assert {(row[0], row[1]) for row in data.territories} == {("001", "800"), ("001", "808")}
    assert {(row[0], row[1]) for row in data.number_patterns} == {("001", "800"), ("001", "808")}


def test_geonames_parser_preserves_source_rows_and_scope():
    postal = _zip_file(
        "allCountries.txt", "FR\t75001\tParis\tIle-de-France\t11\tParis\t75\t\t\t48.86\t2.34\t6\n"
    )
    place = _zip_file(
        "cities5000.txt",
        "2988507\tParis\tParis\tParis\t48.85341\t2.3488\tP\tPPLC\tFR\t\t11\t75\t\t\t2138551\t42\t42\tEurope/Paris\t2025-09-12\n",
    )
    data = parse_geonames(postal, place, enforce_minimums=False)
    assert data.postal_codes[0][:4] == (0, "FR", "75001", "Paris")
    assert data.places[0][0:3] == ("2988507", "Paris", "Paris")


def test_nager_parser_keeps_subdivision_and_type_relations():
    payload = {
        "countries": [{"countryCode": "FR", "name": "France"}],
        "holiday_sets": [{"countryCode": "FR", "year": 2026, "holidays": [{
            "date": "2026-01-01", "name": "New Year's Day", "countryCode": "FR",
            "nationalHoliday": True, "subdivisionCodes": ["FR-75"],
            "holidayTypes": ["Public", "Bank"],
        }]}],
    }
    data = parse_nager(json.dumps(payload).encode(), enforce_minimums=False)
    assert data.holidays[0][0] == "FR:2026:0"
    assert data.subdivisions == (("FR:2026:0", "FR-75"),)
    assert data.types == (("FR:2026:0", "Public"), ("FR:2026:0", "Bank"))


def test_tedb_parser_preserves_rate_category_and_codes():
    xml = """<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
      <env:Body><r:retrieveVatRatesRespMsg
       xmlns:r="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService"
       xmlns="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService:types">
        <additionalInformation/><vatRateResults><memberState>FR</memberState><type>REDUCED</type>
        <rate><type>REDUCED_RATE</type><value>5.5</value></rate>
        <situationOn>2026-07-01+02:00</situationOn>
        <cnCodes><code><value>01</value><description>Animals</description></code></cnCodes>
        <category><identifier>FOODSTUFFS</identifier><description>Food</description></category>
        <comment>Source comment</comment></vatRateResults>
      </r:retrieveVatRatesRespMsg></env:Body></env:Envelope>"""
    data = parse_tedb(xml.encode(), enforce_minimums=False)
    assert data.rates[0][1:8] == (
        "FR", "REDUCED", "REDUCED_RATE", Decimal("5.5"), "2026-07-01",
        "FOODSTUFFS", "Food",
    )
    assert data.cn_codes == ((0, "01", "Animals"),)
    assert data.country_metadata == ()
    assert data.response_status(("DE", "FR")) == (
        ("DE", True, 0, None, None, False),
        ("FR", True, 1, None, None, False),
    )


def test_tedb_parser_distinguishes_optional_metadata_from_absence():
    xml = """<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
      <env:Body><r:retrieveVatRatesRespMsg
       xmlns:r="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService"
       xmlns="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService:types">
        <additionalInformation><countries><country><isoCode>FR</isoCode>
          <cnCodeProvided>true</cnCodeProvided><cpaCodeProvided>false</cpaCodeProvided>
        </country></countries></additionalInformation>
        <vatRateResults><memberState>FR</memberState><type>STANDARD</type>
          <rate><type>DEFAULT</type><value>20</value></rate>
          <situationOn>2026-07-01</situationOn>
          <category><identifier>STANDARD</identifier><description>Standard</description></category>
        </vatRateResults>
      </r:retrieveVatRatesRespMsg></env:Body></env:Envelope>"""
    data = parse_tedb(xml.encode(), enforce_minimums=False)
    assert data.country_metadata == (("FR", True, False),)
    assert data.response_status(("FR",)) == (("FR", True, 1, True, False, True),)


def test_source_migration_registry_is_contiguous_and_checksummed():
    by_source = {}
    for migration in MIGRATIONS:
        by_source.setdefault(migration.source_schema, []).append(migration)
        assert len(migration.checksum) == 64
    assert set(by_source) == {"cldr", "ec_tedb", "geonames"}
    for source, migrations in by_source.items():
        assert [item.version for item in migrations] == list(
            range(2, latest_schema_version(source) + 1)
        )


def test_nlm_parser_keeps_rights_raw_documents_and_form_structure():
    payload = {
        "cdes": [{"tinyId": "c1", "version": "1", "nihEndorsed": True,
                  "designations": [{"designation": "Mood", "tags": ["Preferred Question Text"]}],
                  "definitions": [{"definition": "Mood score"}],
                  "valueDomain": {"datatype": "Number", "permissibleValues": []}}],
        "forms": [{"tinyId": "f1", "version": "2", "isCopyrighted": True,
                   "noRenderAllowed": True, "designations": [{"designation": "Assessment"}],
                   "formElements": [{"elementType": "question", "label": "Mood",
                                     "question": {"required": True, "datatype": "Number",
                                                  "cde": {"tinyId": "c1", "version": "1"}}}]}],
    }
    data = parse_nlm(json.dumps(payload).encode(), enforce_minimums=False)
    assert data.cdes[0][7:9] == ("Mood", "Mood score")
    assert data.forms[0][7:10] == (True, True, "Assessment")
    assert data.form_elements[0][1:9] == (
        "0", "", "question", "Mood", "c1", "1", "Number", True,
    )


def test_loinc_parser_keeps_terms_assessment_content_and_parts():
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        root = "Loinc_2.82"
        archive.writestr(f"{root}/LoincTable/Loinc.csv",
                         "LOINC_NUM,LONG_COMMON_NAME,COMPONENT,PROPERTY,TIME_ASPCT,SYSTEM,SCALE_TYP,METHOD_TYP,CLASS,STATUS,VersionLastChanged\n1234-5,Mood score,Mood,Score,Pt,^Patient,Qn,,SURVEY,ACTIVE,2.82\n")
        archive.writestr(f"{root}/AccessoryFiles/AnswerFile/AnswerList.csv",
                         "AnswerListId,AnswerStringId,SequenceNo,DisplayText,AnswerCode\nLL1,LA1,1,Never,0\n")
        archive.writestr(f"{root}/AccessoryFiles/AnswerFile/LoincAnswerListLink.csv",
                         "LoincNumber,AnswerListId,AnswerListLinkTypeName\n1234-5,LL1,Example\n")
        archive.writestr(f"{root}/AccessoryFiles/PanelsAndForms/PanelsAndForms.csv",
                         "PARENT_ID,LOINC_NUM,SEQUENCE,DISPLAY_NAME_FOR_FORM,ANSWER_ID\n,1234-5,1,Mood,LL1\n")
        archive.writestr(f"{root}/AccessoryFiles/PartFile/Part.csv",
                         "PartNumber,PartName,PartTypeName,Status\nLP1,Mood,COMPONENT,ACTIVE\n")
        archive.writestr(f"{root}/AccessoryFiles/PartFile/LoincPartLink_Primary.csv",
                         "LoincNumber,PartNumber,PartTypeName\n1234-5,LP1,COMPONENT\n")
    data = parse_loinc(target.getvalue(), enforce_minimums=False)
    assert data.terms[0][:3] == ("1234-5", "Mood score", "Mood")
    assert data.answers[0][:5] == ("LL1", "LA1", "1", "Never", "0")
    assert data.panels[0][:6] == (0, "", "1234-5", "1", "Mood", "LL1")
    assert data.parts[0][:4] == ("LP1", "Mood", "COMPONENT", "ACTIVE")


def test_who_parser_preserves_icd11_hierarchy_and_source_documents():
    payload = {"entities": [
        {"@id": "http://id.who.int/icd/release/11/test/mms", "title": {"@value": "MMS"},
         "child": ["http://id.who.int/icd/release/11/test/mms/1"]},
        {"@id": "http://id.who.int/icd/release/11/test/mms/1", "code": "1A00",
         "title": {"@value": "Cholera"},
         "parent": ["http://id.who.int/icd/release/11/test/mms"], "child": []},
    ]}
    data = parse_who(json.dumps(payload).encode(), enforce_minimums=False)
    by_code = {row[1]: row for row in data.entities}
    assert by_code["1A00"][2] == "Cholera"
    assert by_code["1A00"][6:8] == (1, True)


class _ReleaseCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []
        self.rowcount = 1

    def execute(self, statement, params=None):
        self.calls.append((statement, params))

    def fetchone(self):
        return self.rows.pop(0)


def test_release_rollback_accepts_only_validated_retired_snapshots():
    cursor = _ReleaseCursor([("retired",), (1,)])
    assert activate_validated_release(cursor, "iana", "2026b+sha256:old")
    assert len(cursor.calls) == 5

    active = _ReleaseCursor([("active",)])
    assert not activate_validated_release(active, "iana", "2026c+sha256:current")
    assert len(active.calls) == 2

    for status in ("staged", "rejected"):
        try:
            activate_validated_release(_ReleaseCursor([(status,)]), "iana", "bad")
            raise AssertionError
        except ValueError:
            pass


def test_reference_grant_plan_is_registry_derived_and_least_scope():
    assert approved_reference_targets({"iana_country"}) == {
        "iana": ("country_code", "release"),
    }
    for datasets in ({"cldr_currency"}, {"missing"}):
        try:
            approved_reference_targets(datasets); raise AssertionError
        except ValueError:
            pass


def test_sync_connection_credentials_override_serving_credentials():
    env = {
        "KB_PG_HOST": "serve-db", "KB_PG_PORT": "5432", "KB_PG_DB": "world",
        "KB_PG_USER": "serve_reader", "KB_PG_PASSWORD": "serve-secret",
        "SYNC_PG_HOST": "sync-db", "SYNC_PG_PORT": "5433", "SYNC_PG_DB": "reference",
        "SYNC_PG_USER": "sync_writer", "SYNC_PG_PASSWORD": "sync-secret",
        "SYNC_PG_SSLMODE": "require",
    }
    with patch.dict(os.environ, env, clear=True):
        kwargs = _connection_kwargs()
    assert kwargs == {
        "host": "sync-db", "port": 5433, "dbname": "reference",
        "user": "sync_writer", "password": "sync-secret",
        "sslmode": "require", "connect_timeout": 30,
    }


TESTS = [
    test_iana_parser_preserves_source_boundaries,
    test_iana_parser_rejects_unknown_zone,
    test_cldr_parser_keeps_codes_displays_temporal_rows_and_units_separate,
    test_cldr_validation_rejects_duplicate_semantic_keys,
    test_ecb_parser_expands_non_missing_rates_only,
    test_cdc_parser_preserves_hierarchy_and_metadata,
    test_libphonenumber_parser_uses_calling_code_for_non_geographic_keys,
    test_geonames_parser_preserves_source_rows_and_scope,
    test_nager_parser_keeps_subdivision_and_type_relations,
    test_tedb_parser_preserves_rate_category_and_codes,
    test_tedb_parser_distinguishes_optional_metadata_from_absence,
    test_source_migration_registry_is_contiguous_and_checksummed,
    test_nlm_parser_keeps_rights_raw_documents_and_form_structure,
    test_loinc_parser_keeps_terms_assessment_content_and_parts,
    test_who_parser_preserves_icd11_hierarchy_and_source_documents,
    test_release_rollback_accepts_only_validated_retired_snapshots,
    test_reference_grant_plan_is_registry_derived_and_least_scope,
    test_sync_connection_credentials_override_serving_credentials,
]


def main():
    failed = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nsource sync: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
