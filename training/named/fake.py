"""A small fake of what the factory will hand training: workbooks of a few families, whose headers differ family to family and whose questions use neither, each row in the factory's own format, the programs run through engine/program. And a tiny hashing backbone, so the whole pipeline runs in seconds with no model file. Tests and pipeline checks only: nothing measured on this says anything about accuracy.
"""
from __future__ import annotations

import hashlib

import torch
from torch import nn

from engine.named.backbone import question_words
from engine.named.names import NameSet
from engine.program.catalog import Catalog
from engine.program.execute import run
from engine.program.language import Agg, Col, Equals, Rank, Show, answer_type_of, coordinates
from engine.program.names import ANSWER_TYPES, AVG, COUNT, ENTITY_GROUP, HIGHEST, SUM, WORD_ROLES
from engine.program.semantics import fold
from engine.program.store import RequestTables
from engine.sheet import compile_workbook, workbook_from_json
from engine.sheet.names import COLUMN_ROLES, LINE_ROLES, VALUE_KINDS
from training.named.data import TabExample

MEANINGS = ("schema:name", "schema:location", "schema:weight", "schema:width", "schema:startTime", "schema:leiCode")      # basis properties (engine/data/named_coordinates.json)
# Each family's own words for the same columns: the reader must carry the meaning, not the spelling.
FAMILY_HEADERS = (("Counterparty", "Region", "Volume MMBtu", "Price", "Trade Date", "Deal"),
                  ("Shipper", "Area", "Qty", "Px", "Flow Day", "Ticket"),
                  ("Customer", "Zone", "Quantity", "Rate", "As Of", "Contract No"),
                  ("Company", "Market", "Vol", "Cost", "Day", "ID"),
                  ("Vendor", "Hub", "Units", "Unit Price", "Date", "Reference"),
                  ("Party", "Territory", "Nominated", "Index", "Gas Day", "Trade"))
NAMES = ("Aquila", "Duke", "Reliant", "Mirant", "Dynegy", "Calpine", "Sempra", "Entergy")
PLACES = ("East", "West", "Gulf", "Midcon", "Rockies")
ASKED = {"quantity": ("volume", "how", "much", "gas"), "price": ("price", "paid"), "party": ("company", "who"), "place": ("region", "where")}


def fake_name_set() -> NameSet:
    structure = [f"column_role:{role}" for role in COLUMN_ROLES] + [f"value_kind:{kind}" for kind in VALUE_KINDS] + [f"line_role:{role}" for role in LINE_ROLES] + [f"word_role:{role}" for role in WORD_ROLES] + [f"answer_type:{name}" for name in ANSWER_TYPES]
    return NameSet(tuple(sorted(set(structure))) + MEANINGS, len(set(structure)))


class HashBackbone(nn.Module):
    """A word table over hashed words and letter trigrams. Same two calls as engine/named/backbone.py's TextBackbone; a bundle reloads with it passed in."""

    def __init__(self, dim: int = 32, buckets: int = 2048):
        super().__init__()
        self.name = "hash"
        self.dim = dim
        self.buckets = buckets
        self.table = nn.EmbeddingBag(buckets, dim, mode="mean")
        self.out = nn.Linear(dim, dim)
        with torch.no_grad():
            generator = torch.Generator().manual_seed(7)
            self.table.weight.copy_(torch.randn(buckets, dim, generator=generator) * 0.1)

    def _ids(self, text: str) -> list[int]:
        low = text.casefold()
        pieces = [*low.split(), *(low[at:at + 3] for at in range(max(0, len(low) - 2)))] or [""]
        return [int.from_bytes(hashlib.blake2b(piece.encode("utf-8", "surrogatepass"), digest_size=4).digest(), "little") % self.buckets for piece in pieces]

    def _vectors(self, texts: list[str]) -> torch.Tensor:
        device = self.out.weight.device
        ids, offsets = [], []
        for text in texts:
            offsets.append(len(ids))
            ids.extend(self._ids(text))
        return self.out(self.table(torch.as_tensor(ids, dtype=torch.long, device=device), torch.as_tensor(offsets, dtype=torch.long, device=device)))

    def encode_texts(self, texts: list[str], *, max_tokens: int = 32, batch: int = 32) -> tuple[list[str], torch.Tensor]:
        distinct = sorted(set(texts), key=lambda text: (len(text), text))
        return distinct, (self._vectors(distinct) if distinct else torch.zeros((0, self.dim)))

    def encode_questions(self, questions: list[str], *, max_tokens: int = 64) -> list[tuple[list[str], torch.Tensor]]:
        out = []
        for question in questions:
            words = question_words(question)
            out.append((words, self._vectors(words) if words else torch.zeros((0, self.dim))))
        return out


def fake_catalog(family: int, *, lines: int = 12) -> Catalog:
    """One workbook of a family: a tab of deals, its figures and names a function of the family and line."""
    headers = list(FAMILY_HEADERS[family % len(FAMILY_HEADERS)])
    rows = [headers]
    for line in range(lines):
        seed = family * 31 + line * 7
        rows.append([NAMES[seed % len(NAMES)], PLACES[seed % len(PLACES)], float(100 + (seed * 13) % 900), round(2.0 + ((seed * 3) % 50) / 10.0, 2), f"2001-{1 + seed % 12:02d}-{1 + seed % 27:02d}", f"D{1000 + seed}"])
    return Catalog.from_model(compile_workbook(workbook_from_json({"name": f"fake{family}.xlsx", "tabs": [{"name": "Deals", "rows": rows}]})))


def _span(words: list[str], asked: list[str]) -> list[int]:
    return [at for at, word in enumerate(words) if word in asked]


def _column_link(words: list[str], asked: list[str], position: int, name: str, role: str, table: str, meaning: str) -> dict:
    return {"words": " ".join(asked), "span": _span(words, asked), "ref": str(position), "table": table, "kind": "column", "role": role, "position": position, "column": name, "coordinates": [meaning], "own_words": False}


def _value_link(words: list[str], asked: list[str], position: int, name: str, value: str, table: str, meaning: str) -> dict:
    return {"words": " ".join(asked), "span": _span(words, asked), "ref": f"V{position}", "table": table, "kind": "value", "role": "filter", "position": position, "column": name, "target": value, "coordinates": [meaning], "own_words": True}


def _row(family: int, at: int, question: str, program, links: list[dict], catalog: Catalog, tables: RequestTables, tab: str) -> dict:
    rows = run(program, tables).rows
    return {"source": "fake", "side": "fake", "family": f"family{family}", "mailbox": "fake", "file": f"fake{family}.xlsx", "book": f"book{family}", "tab": tab, "role": "fake", "id": f"fake{family}.{at}", "question": question, "written": question, "noise": None, "form": type(program).__name__.lower().replace("show", "show"), "program": repr(program), "coordinates": list(coordinates(program, catalog.column_kind)), "answer_type": answer_type_of(program, catalog.column_kind), "answer": [list(row) for row in rows], "links": links, "words": len(question.split()), "short": True, "no_header_word": True, "subject_only": False, "header_words": [], "label": "gold", "second_reading": {}}


def fake_rows(family: int, catalog: Catalog) -> list[dict]:
    """Questions on a family's tab, in words its headers do not use."""
    table = catalog.tables[0]
    tables = RequestTables(catalog)
    name, place, quantity, price = (table.columns[position].name for position in range(4))
    t = table.id
    party, region, volume, cost = Col(t, 0), Col(t, 1), Col(t, 2), Col(t, 3)
    rows, at = [], 0
    seen_names = sorted({text for text in table.texts[0] if text})
    seen_places = sorted({text for text in table.texts[1] if text})
    for value in seen_names[:4]:
        q = f"total volume for {value}"
        w = question_words(q)
        rows.append(_row(family, at, q, Agg(SUM, volume, t, (Equals(party, fold(value)),)), [_column_link(w, ["volume"], 2, quantity, "measure", t, MEANINGS[2]), _value_link(w, [fold(value)], 0, name, value, t, MEANINGS[0])], catalog, tables, table.tab))
        at += 1
        q = f"how many deals did {value} do"
        w = question_words(q)
        rows.append(_row(family, at, q, Agg(COUNT, None, t, (Equals(party, fold(value)),)), [_value_link(w, [fold(value)], 0, name, value, t, MEANINGS[0])], catalog, tables, table.tab))
        at += 1
    for value in seen_places[:3]:
        q = f"average price in the {value} region"
        w = question_words(q)
        rows.append(_row(family, at, q, Agg(AVG, cost, t, (Equals(region, fold(value)),)), [_column_link(w, ["price"], 3, price, "measure", t, MEANINGS[3]), _value_link(w, [fold(value)], 1, place, value, t, MEANINGS[1])], catalog, tables, table.tab))
        at += 1
    q = "which company has the highest volume"
    w = question_words(q)
    rows.append(_row(family, at, q, Rank(ENTITY_GROUP, t, HIGHEST, (), volume, key=party, function=SUM), [_column_link(w, ["company"], 0, name, "key", t, MEANINGS[0]), _column_link(w, ["volume"], 2, quantity, "measure", t, MEANINGS[2])], catalog, tables, table.tab))
    at += 1
    q = f"which region is {seen_names[0]} in"
    w = question_words(q)
    rows.append(_row(family, at, q, Show(t, (region,), (Equals(party, fold(seen_names[0])),)), [_column_link(w, ["region"], 1, place, "key", t, MEANINGS[1]), _value_link(w, [fold(seen_names[0])], 0, name, seen_names[0], t, MEANINGS[0])], catalog, tables, table.tab))
    for row in rows:
        row["form"] = next(name.split(":")[1] for name in row["coordinates"] if name.startswith("form:"))
    return rows


def fake_examples(families: int = 6) -> list[TabExample]:
    """One tab per family, every row and label in the factory's format."""
    out = []
    for family in range(families):
        catalog = fake_catalog(family)
        table = catalog.tables[0]
        labels = {f"{table.id}.{position}": {"coordinates": [MEANINGS[position]], "raw": MEANINGS[position].split(":")[1], "conf": 8} for position in range(len(MEANINGS))}
        rows = fake_rows(family, catalog)
        out.append(TabExample(f"book{family}|{table.tab}", f"family{family}", f"fake{family}.xlsx", table.tab, "fake", [(t.id, t.kind, t.table) for t in catalog.tables], rows, labels))
    return out


__all__ = ["HashBackbone", "MEANINGS", "fake_catalog", "fake_examples", "fake_name_set", "fake_rows"]
