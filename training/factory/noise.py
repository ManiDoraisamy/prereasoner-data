"""Typos and trade shorthand, by script: the gold stays what it was, the words get messier, and the share is exact. Deterministic per question id. A word's position is kept, so a link still points at it."""
from __future__ import annotations

import hashlib
import random
import re

# Shorthand seen in real headers and desks (corpora.txt 1.4: ctrpty, rec, del; the rest is common trade writing). A list of the trade's own short forms, not of any sheet's.
SHORTHAND = {
    "counterparty": "cpty", "counterparties": "cptys", "customer": "cust", "customers": "custs", "delivery": "del", "deliveries": "dels", "receipt": "rec", "receipts": "recs", "volume": "vol", "volumes": "vols", "quantity": "qty", "quantities": "qtys", "price": "px",
    "prices": "pxs", "transport": "transp", "transportation": "transp", "nomination": "nom", "nominations": "noms", "average": "avg", "total": "tot", "totals": "tots", "difference": "diff", "number": "no", "account": "acct", "accounts": "accts", "position": "pos", "positions": "pos",
    "month": "mth", "months": "mths", "year": "yr", "years": "yrs", "contract": "ctr", "contracts": "ctrs", "pipeline": "pipe", "balance": "bal", "demand": "dmd", "storage": "stor", "amount": "amt", "amounts": "amts", "management": "mgmt", "company": "co", "companies": "cos", "budget": "bud",
    "forecast": "fcst", "actual": "act", "actuals": "acts", "invoice": "inv", "invoices": "invs", "payment": "pmt", "payments": "pmts", "expense": "exp", "expenses": "exps", "revenue": "rev", "revenues": "revs", "maximum": "max", "minimum": "min", "percent": "pct", "percentage": "pct",
    "physical": "phys", "financial": "fin", "commodity": "cmdty", "commodities": "cmdties", "trader": "trdr", "traders": "trdrs", "settlement": "settle", "settlements": "settles", "schedule": "sched", "scheduled": "sched", "imbalance": "imbal", "imbalances": "imbals", "capacity": "cap",
    "gas": "gas", "electric": "elec", "electricity": "elec", "generation": "gen", "megawatt": "mw", "megawatts": "mw", "department": "dept", "division": "div", "date": "dt", "last": "lst", "versus": "vs", "against": "vs", "between": "btw", "quarter": "qtr", "quarters": "qtrs",
}
SHARE = 0.15
_WORD = re.compile(r"[A-Za-z]+")


def _roll(question_id: str) -> random.Random:
    return random.Random(int.from_bytes(hashlib.blake2b(question_id.encode("utf-8"), digest_size=8).digest(), "little"))


def _typo(word: str, rng: random.Random) -> str:
    at = rng.randrange(1, len(word) - 1)
    kind = rng.randrange(3)
    if kind == 0:
        return word[:at] + word[at + 1] + word[at] + word[at + 2:]
    if kind == 1:
        return word[:at] + word[at + 1:]
    return word[:at] + word[at] + word[at:]


def messy(question_id: str, question: str) -> tuple[str, dict | None]:
    """The question as typed, and what changed: {kind, word, became}, or None for most questions. A word inside a quoted value keeps, as a value's own spelling is what a reader must still reach."""
    rng = _roll(question_id)
    if rng.random() >= SHARE:
        return question, None
    words = list(_WORD.finditer(question))
    short = [m for m in words if m.group(0).lower() in SHORTHAND and SHORTHAND[m.group(0).lower()] != m.group(0).lower()]
    long_enough = [m for m in words if len(m.group(0)) >= 5]
    if short and (rng.random() < 0.5 or not long_enough):
        match = short[rng.randrange(len(short))]
        became, kind = SHORTHAND[match.group(0).lower()], "shorthand"
    elif long_enough:
        match = long_enough[rng.randrange(len(long_enough))]
        became, kind = _typo(match.group(0), rng), "typo"
    else:
        return question, None
    return question[:match.start()] + became + question[match.end():], {"kind": kind, "word": match.group(0), "became": became}


__all__ = ["SHORTHAND", "messy"]
