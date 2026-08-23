"""Build structured contrastive data for the shared calculation representation.

The corpus does not supervise SQL strings or numeric answers. It aligns questions with named
calculation prototypes and aligns operand phrases with schema-column names. Runtime may use those
similarities for ordering, while deterministic specifications remain the intent and correctness gate.

Run from the repository root:

    python -m training.props.calculation_contrastive
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from engine.calculations.registry import CALCULATION_INTENT_PROTOTYPES


HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PREREASONER_TRAIN_DIR", str(HERE))) / "data"

SCENARIOS = (
    {
        "name": "currency",
        "intent": "currency",
        "train": (
            "convert total invoice value to USD",
            "report the sum of order amount in euros",
            "express total revenue in British pounds",
            "denominate aggregate payment amount in JPY",
            "what is the total sales value in US dollars",
            "convert summed transaction amount into CAD",
        ),
        "eval": (
            "show aggregate invoice amount converted to EUR",
            "state total turnover in Australian dollars",
            "give the summed payment value in CHF",
        ),
        "operands": (
            ("invoice value", "amount", ("quantity", "population", "rate_to_usd")),
            ("transaction amount", "payment_total", ("item_count", "exchange_rate", "country")),
        ),
    },
    {
        "name": "ratio",
        "intent": "ratio",
        "train": (
            "GDP per capita",
            "economic output per person",
            "ratio of gross income to population",
            "total revenue divided by customer count",
            "sales value per resident",
            "ratio of emissions to units produced",
        ),
        "eval": (
            "national output per inhabitant",
            "divide aggregate earnings by workforce size",
            "ratio of total claims to enrolled members",
        ),
        "operands": (
            ("economic output", "gdp", ("population", "year", "country_id")),
            ("resident count", "population", ("gdp", "tax_percent", "country")),
            ("customer count", "customer_count", ("revenue", "commission_fraction", "region")),
        ),
    },
    {
        "name": "tax",
        "intent": "rate_application",
        "train": (
            "calculate total tax amount",
            "apply the VAT percent to invoice amount",
            "sum tax due on transaction value",
            "total levy amount by country",
            "calculate tax charge on net sales",
            "what tax amount is payable on order value",
        ),
        "eval": (
            "compute the fiscal charge on taxable amount",
            "apply each jurisdiction tax percentage to sales",
            "sum the VAT due across invoices",
        ),
        "operands": (
            ("taxable amount", "amount", ("tax_percent", "country", "effective_date")),
            ("tax percentage", "tax_percent", ("amount", "population", "currency")),
        ),
    },
    {
        "name": "commission",
        "intent": "rate_application",
        "train": (
            "calculate total commission amount",
            "apply commission fraction to payment amount",
            "sum merchant commission on transaction value",
            "commission charge by payment instrument",
            "total processing commission due",
            "calculate the commission cost on sales amount",
        ),
        "eval": (
            "compute merchant fee amount for each instrument",
            "apply the commission percentage to card volume",
            "sum payment commission due",
        ),
        "operands": (
            ("payment amount", "transaction_amount", ("commission_fraction", "instrument", "country")),
            ("commission percentage", "commission_percent", ("transaction_amount", "currency", "payment_id")),
        ),
    },
    {
        "name": "interest",
        "intent": "rate_application",
        "train": (
            "calculate annual simple interest amount",
            "apply the one year interest percent to principal",
            "total annual simple interest on loan balance",
            "sum one-year financing interest charge",
            "annual simple interest due on principal amount",
            "calculate one year simple interest cost",
        ),
        "eval": (
            "compute the yearly simple financing charge",
            "apply annual interest percentage to outstanding principal",
            "sum one-year simple interest due",
        ),
        "operands": (
            ("outstanding principal", "principal_amount", ("interest_percent", "term_months", "loan_id")),
            ("annual interest percentage", "interest_percent", ("principal_amount", "country", "start_date")),
        ),
    },
)


def _intent_row(scenario, question: str, split: str, index: int) -> dict:
    positive = CALCULATION_INTENT_PROTOTYPES[scenario["intent"]]
    negatives = tuple(
        prototype for name, prototype in CALCULATION_INTENT_PROTOTYPES.items()
        if name != scenario["intent"]
    )
    return {
        "id": f"{scenario['name']}:intent:{split}:{index}",
        "kind": "intent",
        "label": scenario["intent"],
        "query": question,
        "positive": positive,
        "negatives": list(negatives),
    }


def _operand_rows(scenario, question: str, split: str, index: int) -> list[dict]:
    rows = []
    for operand_index, (phrase, positive, negatives) in enumerate(scenario["operands"]):
        rows.append({
            "id": f"{scenario['name']}:operand:{split}:{index}:{operand_index}",
            "kind": "operand",
            "label": scenario["name"],
            "query": f"{phrase} | {question}",
            "positive": positive,
            "negatives": list(negatives),
        })
    return rows


def build_rows() -> tuple[list[dict], list[dict]]:
    train, evaluation = [], []
    for scenario in SCENARIOS:
        for split, questions, destination in (
            ("train", scenario["train"], train),
            ("eval", scenario["eval"], evaluation),
        ):
            for index, question in enumerate(questions):
                destination.append(_intent_row(scenario, question, split, index))
                destination.extend(_operand_rows(scenario, question, split, index))
    train_queries = {row["query"] for row in train}
    eval_queries = {row["query"] for row in evaluation}
    overlap = train_queries & eval_queries
    if overlap:
        raise ValueError(f"calculation contrastive query leakage: {sorted(overlap)[:3]}")
    return train, evaluation


def write_rows(data_dir: Path = DATA) -> tuple[Path, Path]:
    train, evaluation = build_rows()
    data_dir.mkdir(parents=True, exist_ok=True)
    train_path = data_dir / "calculation_contrastive_train.jsonl"
    eval_path = data_dir / "calculation_contrastive_eval.jsonl"
    for path, rows in ((train_path, train), (eval_path, evaluation)):
        path.write_text(
            "".join(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n" for row in rows),
            encoding="utf-8",
        )
    return train_path, eval_path


def main() -> None:
    train_path, eval_path = write_rows()
    train = sum(1 for _ in train_path.open(encoding="utf-8"))
    evaluation = sum(1 for _ in eval_path.open(encoding="utf-8"))
    print(f"calculation contrastive corpus: {train} train, {evaluation} held out")
    print(f"  {train_path}")
    print(f"  {eval_path}")


if __name__ == "__main__":
    main()
