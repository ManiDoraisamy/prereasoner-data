"""Hermetic release gate for code-approved reference datasets: selection precision and recall, harmful selection and
deterministic replay. What the own-data path answers over the enriched request is not measured: it reads each table as a
tab and joins none (engine/program/answer.py)."""
from __future__ import annotations

from dataclasses import dataclass

from engine.enrichment import (
    EnrichmentRuntime, LoadedDataset, RuntimeIdentity, SnapshotPin,
)


@dataclass(frozen=True)
class ActivationMetrics:
    dataset: str
    positive_cases: int
    negative_cases: int
    selection_precision: float
    selection_recall: float
    harmful_selection_rate: float
    deterministic_replay: bool
    passed: bool
    failures: tuple[str, ...]


_COUNTRIES = {"FR": "France", "US": "United States", "JP": "Japan"}
_TABLES = (
    ("customers", "customer_id", "country_code"),
    ("orders", "order_id", "billing_country"),
    ("registrations", "registration_id", "country"),
    ("patient_intakes", "intake_id", "country_code"),
    ("incidents", "incident_id", "country"),
)
_POSITIVE_QUESTIONS = (
    "Show the country name for each {entity}",
    "Display the official country name for each {entity}",
    "List the full country name for each {entity}",
    "Give the country name associated with each {entity}",
    "Return the corresponding country name for every {entity}",
)
_NO_INTENT_QUESTIONS = (
    "Count {entity} by country",
    "List the country codes for the {entity}",
    "Group {entity} by country",
    "How many {entity} are in each country code?",
    "Sort the {entity} by country",
)


class _CountryStore:
    @staticmethod
    def active_snapshot(definition):
        return SnapshotPin("iana", "benchmark-2026c", 1, definition.definition_id)

    def load_by_keys(self, definition, snapshot, keys):
        wanted = {tuple(key) for key in keys}
        rows = tuple((code, name) for code, name in _COUNTRIES.items() if (code,) in wanted)
        return LoadedDataset(definition, snapshot, definition.columns, rows)


def _table(name: str, id_column: str, country_column: str, values=None) -> dict:
    values = tuple(values or _COUNTRIES)
    return {
        "name": name,
        "columns": [id_column, country_column],
        "rows": [[index, value] for index, value in enumerate(values, 1)],
    }


def run_iana_country_activation_benchmark() -> ActivationMetrics:
    identity = RuntimeIdentity("benchmark", "model", "planner", "ranker")
    runtime = EnrichmentRuntime(
        _CountryStore(), enabled_datasets={"iana_country"}, identity=identity,
    )
    positives = []
    for table_name, id_column, country_column in _TABLES:
        entity = table_name.replace("_", " ")
        for template in _POSITIVE_QUESTIONS:
            positives.append((_table(table_name, id_column, country_column), template.format(entity=entity)))

    negatives = []
    for table_name, id_column, country_column in _TABLES:
        entity = table_name.replace("_", " ")
        for template in _NO_INTENT_QUESTIONS:
            negatives.append((_table(table_name, id_column, country_column), template.format(entity=entity)))
        for template in _POSITIVE_QUESTIONS:
            question = template.format(entity=entity)
            negatives.append((_table(table_name, id_column, country_column, ("France", "Japan")), question))
            negatives.append((_table(table_name, id_column, country_column, ("FR",)), question))
            negatives.append((_table(table_name, id_column, country_column, ("ZZ", "XX")), question))

    selected = 0
    deterministic = True
    failures = []
    for index, (table, question) in enumerate(positives):
        first = runtime.prepare((table,), question)
        second = runtime.prepare((table,), question)
        deterministic &= first.added_tables == second.added_tables
        deterministic &= first.explicit_fks == second.explicit_fks
        deterministic &= (
            first.manifest is not None and second.manifest is not None
            and first.manifest.digest() == second.manifest.digest()
        )
        if not first.used:
            failures.append(f"positive-{index}: dataset was not selected")
            continue
        selected += 1

    harmful = 0
    for index, (table, question) in enumerate(negatives):
        if runtime.prepare((table,), question).used:
            harmful += 1
            failures.append(f"negative-{index}: harmful selection")

    tp, fp, fn = selected, harmful, len(positives) - selected
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    harmful_rate = harmful / len(negatives)
    passed = precision >= 0.99 and recall >= 0.95 and harmful_rate <= 0.005 and deterministic
    return ActivationMetrics(
        "iana_country", len(positives), len(negatives), precision, recall,
        harmful_rate, deterministic, passed, tuple(failures),
    )


def main() -> None:
    metrics = run_iana_country_activation_benchmark()
    print(metrics)
    raise SystemExit(0 if metrics.passed else 1)


if __name__ == "__main__":
    main()
