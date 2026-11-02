"""The names the reader fills: the name set's coordinates, one head output each, and the closed heads of a question.

The name set is engine/data/named_coordinates.json, written by training/factory/build_names.py. The head holds its structure tier and its vocabulary pack, in sorted order, plus any schema.org name a training set's labels use: 1,521 properties would cost index size for names the labels barely reach (7 uses on validation). A bundle records its own list and the sha of that list, so an index built by one head cannot be read by another.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from engine.program.names import (A_CODE, A_COMPANY, A_COUNT, A_DATE, A_LIST, A_MONEY, A_NAME, A_NUMBER, A_PERCENT, A_PERSON, A_PLACE, A_QUANTITY, AGGREGATES, CALC_OPS, COMPARISONS, DIRECTIONS, FORMS, RANK_ENTITIES, ROW_OPS, SIGNED, SIZES, SLOT_AGGREGATE, SLOT_ANSWER_TYPE, SLOT_BLANK, SLOT_CALC_OP, SLOT_COMPARISON, SLOT_CONDITIONS, SLOT_DIRECTION, SLOT_ENTITY, SLOT_FORM, SLOT_ROW_OP, SLOT_SIZE, WORD_ROLES, coarse_type, slot_of)

NAME_SET_PATH = Path(__file__).resolve().parents[1] / "data" / "named_coordinates.json"
NONE = "none"
LEFT = "left"
RIGHT = "right"
MAX_CONDITIONS = 6
# What a question asks for, as finely as it says: what a program can state is coarse (engine/program/names.py:coarse_type), and the head's classes add up to that.
ANSWER_READINGS = (A_NUMBER, A_MONEY, A_QUANTITY, A_COUNT, A_PERCENT, A_DATE, A_NAME, A_PERSON, A_COMPANY, A_CODE, A_PLACE, A_LIST)
# The vocabulary's names that say which fine type a column holds: a label on the answer's column is a fine type's label, and at serving the same names are its evidence.
FINE_NAMES: dict[str, tuple[str, ...]] = {
    A_PERSON: ("person_name", "trader", "scheduler", "originator"),
    A_COMPANY: ("company_name", "counterparty", "buyer", "seller", "shipper", "supplier", "customer", "producer", "marketer", "agent", "vendor", "internal_entity", "regulator", "operator", "broker"),
    A_CODE: ("contract_id", "deal_id", "counterparty_code", "invoice_number", "code_general", "meter", "company_code", "project_code", "purchase_order", "reference_number", "general_ledger_account", "cost_center", "rate_schedule"),
    A_PLACE: ("location_general", "state_region", "city", "country", "delivery_point", "receipt_point", "hub", "pricing_point", "zone", "citygate", "region_trading", "storage_field", "power_plant", "plant", "control_area"),
    A_MONEY: ("amount_money", "invoice_amount", "payment_amount", "outstanding_balance", "accounts_receivable", "accounts_payable", "write_off", "accrual", "budget", "actual_amount", "revenue", "sales_amount", "purchase_amount", "expense", "operating_expense", "capital_expenditure", "depreciation", "gross_margin", "net_income", "ebit", "cash_flow", "tax_amount", "fee", "total_cost", "extended_amount", "notional_amount", "mark_to_market", "profit_and_loss", "daily_pnl", "unrealized_gain", "realized_gain", "demand_charge", "commodity_charge", "fuel_charge", "brokerage_fee", "credit_limit", "credit_exposure", "transport_cost", "debit", "credit_entry", "collateral", "letter_of_credit", "margin_call", "price_general"),
    A_QUANTITY: ("volume", "daily_volume", "monthly_volume", "total_volume", "mmbtu", "dekatherm", "mcf", "barrel", "ton", "quantity_count", "contract_quantity", "scheduled_volume", "actual_volume", "allocated_volume", "nomination", "capacity", "storage_inventory", "injection", "withdrawal", "throughput", "generation", "load", "megawatt", "megawatt_hour", "position", "long_position", "short_position", "net_position", "open_position", "burn", "swing_quantity"),
}
FINE_OF_NAME = {f"biz:{name}": fine for fine, names in FINE_NAMES.items() for name in names}

# One choice per question. A head with NONE may stay silent; the rest always say something.
CLOSED_HEADS: dict[str, tuple[str, ...]] = {
    SLOT_FORM: FORMS,
    SLOT_AGGREGATE: (NONE, *AGGREGATES),
    SLOT_DIRECTION: (NONE, *DIRECTIONS),
    SLOT_SIZE: SIZES,
    SLOT_ENTITY: (NONE, *RANK_ENTITIES),
    SLOT_COMPARISON: (NONE, *COMPARISONS),
    SLOT_ROW_OP: (NONE, *ROW_OPS),
    SLOT_CALC_OP: (NONE, *CALC_OPS),
    SLOT_ANSWER_TYPE: ANSWER_READINGS,
    SLOT_BLANK: (NONE, "blank", "filled", "number"),
    SLOT_CONDITIONS: tuple(str(count) for count in range(MAX_CONDITIONS + 1)),
}
_DEFAULTS = {SLOT_SIZE: SIGNED, SLOT_CONDITIONS: "0"}
# One choice per word.
WORD_ROLE_CLASSES = (NONE, *WORD_ROLES)
CALC_SIDES = (NONE, LEFT, RIGHT)


def fine_answer_type(coarse: str, names_of_answer: Iterable[str]) -> str:
    """A gold program's coarse type made finer by the named labels of what it answers with, where those agree with it. No such label leaves the coarse type."""
    for name in names_of_answer:
        fine = FINE_OF_NAME.get(name)
        if fine is not None and coarse_type(fine) == coarse:
            return fine
    return coarse


def closed_labels(coordinates: Iterable[str]) -> dict[str, str]:
    """A gold program's coordinates as one class per closed head. In a calc, its first aggregate, comparison and the like: the left side's, as the program states them first."""
    out = {head: _DEFAULTS.get(head, NONE) for head in CLOSED_HEADS}
    seen: set[str] = set()
    for name in coordinates:
        slot = slot_of(name)
        if slot not in CLOSED_HEADS or slot in seen:
            continue
        body = name.split(":", 1)[1]
        value = body.rsplit("=", 1)[1] if slot == SLOT_CONDITIONS else body.split("@", 1)[0]
        if value in CLOSED_HEADS[slot]:
            out[slot] = value
            seen.add(slot)
    return out


@dataclass(frozen=True)
class NameSet:
    """The head's coordinates in order. at() is a coordinate's output index; a name the head lacks has none."""
    names: tuple[str, ...]
    positions: dict[str, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if len(set(self.names)) != len(self.names):
            raise ValueError("a name twice in a name set")
        object.__setattr__(self, "positions", {name: at for at, name in enumerate(self.names)})

    def __len__(self) -> int:
        return len(self.names)

    def at(self, name: str) -> int | None:
        return self.positions.get(name)

    @property
    def sha(self) -> str:
        return hashlib.sha256("\n".join(self.names).encode("utf-8")).hexdigest()


def load_name_set(path: str | Path = NAME_SET_PATH, extra: Iterable[str] = ()) -> NameSet:
    """The head's names from the name set: the structure tier, the vocabulary, and the extra names a training set's labels use."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("unsupported name set")
    tiers = document["tiers"]
    names = {row["name"] for row in tiers["structure"]["names"]} | {row["name"] for row in tiers["meaning"]["vocabulary"]["names"]} | set(extra)
    return NameSet(tuple(sorted(names)))


def meaning_groups(path: str | Path = NAME_SET_PATH) -> dict[str, tuple[str, ...]]:
    """The vocabulary's groups: the classes a table can be read as, each the superposition of its names."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    groups: dict[str, list[str]] = {}
    for row in document["tiers"]["meaning"]["vocabulary"]["names"]:
        groups.setdefault(row["group"], []).append(row["name"])
    return {group: tuple(sorted(members)) for group, members in sorted(groups.items())}


__all__ = ["ANSWER_READINGS", "CALC_SIDES", "CLOSED_HEADS", "FINE_NAMES", "FINE_OF_NAME", "LEFT", "MAX_CONDITIONS", "NAME_SET_PATH", "NONE", "NameSet", "RIGHT", "WORD_ROLE_CLASSES", "closed_labels", "fine_answer_type", "load_name_set", "meaning_groups"]
