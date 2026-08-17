"""Hermetic knowledge-enrichment selection benchmark and acceptance rule.

This harness measures the offline selection layer, not the AST candidate beam or top-1
answer accuracy. Production release additionally requires the serving-wired candidate-pool,
ranking, execution, slice, and baseline gates described in the roadmap.
"""
from __future__ import annotations

from dataclasses import dataclass

from engine.enrichment.registry import ATTR_CURRENCY_METADATA, EmbeddedStorage, REGISTRY
from engine.enrichment.select import select_datasets


def _t(name, columns, rows):
    return {"name": name, "columns": columns, "rows": rows}


# Each case: (source_tables, request_evidence, expected selections).
BENCHMARK_CORPUS = [
    (  # multi-currency orders -> currency dataset should enrich the currency column
        [_t("orders", ["order_id", "amount", "currency"],
            [[1, 10, "USD"], [2, 20, "EUR"], [3, 30, "GBP"], [4, 40, "INR"]])],
        frozenset({ATTR_CURRENCY_METADATA}),
        {("currency_iso4217", "orders", "currency")},
    ),
    (  # a country column looks like 2-letter codes but is NOT a currency -> must abstain
        [_t("leads", ["name", "country"], [["a", "US"], ["b", "GB"], ["c", "IN"], ["d", "DE"]])],
        frozenset({ATTR_CURRENCY_METADATA}), set(),
    ),
    (  # free-text status column -> must abstain
        [_t("orders", ["order_id", "status"],
            [[1, "shipped"], [2, "pending"], [3, "shipped"], [4, "cancelled"]])],
        frozenset({ATTR_CURRENCY_METADATA}), set(),
    ),
    (  # invalid pseudo-codes (XXX/XTS/XAU/XPT) are not even currency-typed -> abstain on typing
        [_t("orders", ["order_id", "currency"], [[1, "XXX"], [2, "XTS"], [3, "XAU"], [4, "XPT"]])],
        frozenset({ATTR_CURRENCY_METADATA}), set(),
    ),
    (  # REAL ISO codes that are absent from the embedded snapshot -> currency-typed but NOT joinable -> abstain
        [_t("orders", ["order_id", "currency"], [[1, "MXN"], [2, "KRW"], [3, "RUB"], [4, "TRY"]])],
        frozenset({ATTR_CURRENCY_METADATA}), set(),
    ),
    (  # sparse overlap: one embedded code among many unembedded ones (conf ~0.17 < 0.9) -> abstain
        [_t("orders", ["order_id", "currency"],
            [[1, "USD"], [2, "MXN"], [3, "KRW"], [4, "RUB"], [5, "TRY"], [6, "IDR"]])],
        frozenset({ATTR_CURRENCY_METADATA}), set(),
    ),
    (  # single non-empty currency cell is too little evidence -> abstain (min-evidence floor)
        [_t("orders", ["order_id", "currency"], [[1, "USD"]])],
        frozenset({ATTR_CURRENCY_METADATA}), set(),
    ),
    (  # single valid currency among the keys -> enrich
        [_t("invoices", ["id", "ccy"], [[1, "USD"], [2, "USD"], [3, "EUR"]])],
        frozenset({ATTR_CURRENCY_METADATA}),
        {("currency_iso4217", "invoices", "ccy")},
    ),
]


@dataclass(frozen=True)
class Metrics:
    precision: float      # case-level: of selected (dataset,column), fraction that were expected
    selection_recall: float
    coverage: float       # ROW-level: of the rows in correctly-enriched columns, fraction that join the key
    harmful_rate: float
    positive_cases: int
    negative_cases: int
    passed: bool


def _key_vals(ds) -> set[str]:
    if not isinstance(ds.storage, EmbeddedStorage) or len(ds.lookup_key) != 1:
        return set()
    index = ds.columns.index(ds.lookup_key[0])
    return {str(row[index]).strip() for row in ds.storage.rows if row[index] is not None}


def _col_cells(tables, table_name, col) -> list[str]:
    for t in tables:
        if t.get("name") != table_name:
            continue
        cols = t.get("columns") or []
        if col in cols:
            ci = cols.index(col)
            return [str(r[ci]).strip() for r in (t.get("rows") or [])
                    if ci < len(r) and r[ci] is not None and str(r[ci]).strip() != ""]
    return []


def run_benchmark(registry=None, corpus=None) -> dict[str, Metrics]:
    reg = registry if registry is not None else REGISTRY
    cases = corpus if corpus is not None else BENCHMARK_CORPUS
    expected: set[tuple[int, str, str, str]] = set()
    actual: set[tuple[int, str, str, str]] = set()
    cov_matched: dict[str, int] = {name: 0 for name in reg}      # row-level coverage accumulators
    cov_total: dict[str, int] = {name: 0 for name in reg}
    for i, (tables, request_evidence, exp) in enumerate(cases):
        sel = select_datasets(tables, registry=reg, request_evidence=request_evidence)
        for ds_name, table_name, col in exp:
            expected.add((i, ds_name, table_name, col))
        for s in sel:
            actual.add((i, s.dataset.name, s.source_table, s.source_column))

    # Coverage is a property of every labeled-positive column, including one the selector
    # misses. It uses exact values because that is what the generated equality join executes.
    for i, (tables, _, _) in enumerate(cases):
        for case_index, ds_name, table_name, col in expected:
            if case_index != i or ds_name not in reg:
                continue
            kv = _key_vals(reg[ds_name])
            cells = _col_cells(tables, table_name, col)
            cov_total[ds_name] += len(cells)
            cov_matched[ds_name] += sum(1 for cell in cells if cell in kv)

    out: dict[str, Metrics] = {}
    for name, ds in reg.items():
        exp = {e for e in expected if e[1] == name}
        act = {a for a in actual if a[1] == name}
        tp = len(act & exp); fp = len(act - exp); fn = len(exp - act)
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        selection_recall = tp / (tp + fn) if (tp + fn) else 0.0
        coverage = cov_matched[name] / cov_total[name] if cov_total[name] else 0.0
        positive_case_ids = {item[0] for item in exp}
        negative_cases = len(cases) - len(positive_case_ids)
        harmful_case_ids = {item[0] for item in act - exp if item[0] not in positive_case_ids}
        harmful_rate = len(harmful_case_ids) / negative_cases if negative_cases else 0.0
        th = ds.thresholds
        passed = (len(positive_case_ids) >= th.min_positive_cases
                  and negative_cases >= th.min_negative_cases
                  and precision >= th.min_precision
                  and selection_recall >= th.min_selection_recall
                  and coverage >= th.min_coverage
                  and harmful_rate <= th.max_harmful_rate)
        out[name] = Metrics(precision, selection_recall, coverage, harmful_rate,
                            len(positive_case_ids), negative_cases, passed)
    return out
