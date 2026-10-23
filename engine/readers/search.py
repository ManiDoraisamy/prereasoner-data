"""The readers' own-data candidates and their choice: the one search behind ``TableQuery.search_pool``.

1. Stage 1 (``schema_items``) scores every table and column for the question; the top tables are kept, a wide
   table's top columns (``prune``).
2. Each stage-2 model reads the question beside the kept tables (engine.readers.text.fit); its outputs are decoded
   into the most probable distinct readings (engine.readers.decode.candidates), and the deterministic builder
   (engine.readers.readings.build) makes each reading a typed-AST query.
3. The candidates of all stage-2 models are pooled, each SQL once, most probable first.
4. After the pool runs, the re-scorer reads the question beside each candidate that ran (its read-as line and
   result shape), and the readers' choice (choice.json) weighs its logit with what the pool says of the candidate:
   its log probability, its best rank, how many of the readers' readings build it, whether its result is empty
   (``rescore``). The choice's probability for the best candidate is its confidence; below the threshold the
   readers refuse it (``confidence``, ``confident``).

Weights live in engine/data/readers/{stage1,stage2_*,rescorer}, each a saved config, tokenizer and model.pt, beside
choice.json.
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

from engine.config import DATA_DIR
from engine.readers import decode as decoding
from engine.readers.read_as import read_as
from engine.readers.readings import build, value_kind
from engine.readers.text import TOP_TABLES, Schema, fit, table_text
from engine.sql_ast import render_query
from engine.sql_candidate import ScoredQuery

READERS_DIR = DATA_DIR / "readers"                 # installed only by training/readers/promote.py
MAX_COLUMNS = 20
BUDGET = 508
CANDIDATES = 16                                    # per stage-2 model
CHOICE_FEATURES = ("rescorer_logit", "score/10", "best_rank/16", "readers_share", "empty")


def choice_features(logit, score, rank, readers_share, rows):
    """What the readers' choice weighs of a candidate that ran (``CHOICE_FEATURES``): the re-scorer's logit, the
    candidate's log probability, its best rank among the readers, the share of the readers' readings that build it
    (more than 1 when one reader builds it twice), and whether its result is empty. training.readers.choice fits the
    weights on these same features of calibration pools."""
    return (logit, score / 10.0, rank / CANDIDATES, readers_share, float(rows == 0))


class ReadingSearch:
    def __init__(self, directory=READERS_DIR):
        from transformers import AutoTokenizer

        from engine.readers import models
        directory = Path(directory)
        self.tokenizer = AutoTokenizer.from_pretrained(directory / "stage1")
        self.stage1 = models.load("schema_items", directory / "stage1")
        # each stage-2 model with whether it reads the question's values beside their columns (reader.json)
        self.stage2 = [(models.load("readings", path),
                        json.loads((path / "reader.json").read_text(encoding="utf-8")).get("anchored", True)
                        if (path / "reader.json").exists() else True)
                       for path in sorted(directory.glob("stage2_*"))]
        self.rescorer = models.load("rescorer", directory / "rescorer")
        self.choice = json.loads((directory / "choice.json").read_text(encoding="utf-8"))
        if tuple(self.choice["features"]) != CHOICE_FEATURES:
            raise ValueError(f"choice.json weighs {self.choice['features']}, serving computes {CHOICE_FEATURES}")

    def schema_items(self, question, schema):
        """{table: (table probability, {column: used probability})}."""
        rows = []
        for table in schema.tables:
            text, spans = table_text(schema, table)
            rows.append({"question": question, "table": table, "text": text,
                         "columns": [{"start": s, "end": e, "name": c} for c, s, e in spans]})
        import torch

        from engine.readers import models

        scores = {}
        with torch.no_grad():
            for begin in range(0, len(rows), 16):
                chunk = rows[begin:begin + 16]
                ids, mask, index, span_mask, valid = models.schema_item_inputs(self.tokenizer, chunk)
                table_logit, column_logits = self.stage1(ids, mask, index, span_mask)
                table_p = torch.sigmoid(table_logit).tolist()
                column_p = torch.sigmoid(column_logits[..., 0])
                for b, row in enumerate(chunk):
                    scores[row["table"]] = (table_p[b], {c["name"]: float(column_p[b, i]) if valid[b, i] else 0.0
                                                         for i, c in enumerate(row["columns"])})
        return scores

    def prune(self, schema, scores):
        """(kept tables in schema order, columns of each, droppable tables lowest-ranked first)."""
        ranked = sorted(schema.tables, key=lambda t: (-scores[t][0], schema.tables.index(t)))[:TOP_TABLES]
        tables = [t for t in schema.tables if t in ranked]
        columns_of = {}
        for table in tables:
            columns = schema.columns[table]
            if len(columns) > MAX_COLUMNS:
                column_scores = scores[table][1]
                best = set(sorted(columns, key=lambda c: (-column_scores.get(c, 0.0), columns.index(c)))[:MAX_COLUMNS])
                columns = [c for c in columns if c in best]
            columns_of[table] = columns
        return tables, columns_of, list(reversed(ranked))[:-1]

    def row(self, question, schema, tables, columns_of, droppable, anchored=True):
        """The stage-2 input for the question over its kept tables, fitted to the token budget."""
        kept, (text, star, table_spans, column_spans) = fit(self.tokenizer, BUDGET, question, schema, tables,
                                                            columns_of, droppable, anchored)
        return {"question": question, "text": text, "star": star,
                "tables": [{"name": t, "span": table_spans[t]} for t in kept],
                "columns": [{"name": f"{t}.{c}", "span": column_spans[f"{t}.{c}"][0]}
                            for t in kept for c in columns_of[t]]}

    def pool(self, question, graph):
        """The readers' candidates for the question, most probable first: [ScoredQuery], each SQL once. A
        candidate's evidence is its read-as line; its features carry its rank among its model's readings.

        A request reads its question more than once (the decomposition probe, compose's leaves, the selection), each
        over its own copy of the same graph; the pool depends only on the question and the data, so it is read
        once and kept by both (``_POOLS``)."""
        key = (question, _signature(graph))
        with _LOCK:
            kept = _POOLS.get(key)
        if kept is not None:
            return list(kept)
        pool = self._pool(question, graph)
        _keep(_POOLS, key, tuple(pool))
        return pool

    def _pool(self, question, graph):
        schema = Schema(graph)
        kept = self.prune(schema, self.schema_items(question, schema))
        named = {f"{table}.{column}": values for (table, column), values in schema.anchors(question).items()
                 if len(values) > 1}
        from engine.readers import models

        pooled, seen, built = [], set(), {}
        for model, anchored in self.stage2:
            row = self.row(question, schema, *kept, anchored=anchored)
            output = models.read_outputs(model, self.tokenizer, row)
            for rank, (score, readings) in enumerate(decoding.candidates(output, row, graph, CANDIDATES)):
                for reading in (readings, *either(readings, named), *referenced(readings, graph)):
                    try:
                        query = build(reading, graph)
                        sql = render_query(query)
                    except Exception:              # noqa: BLE001 - a reading the builder cannot make into a query
                        continue
                    built[sql] = built.get(sql, 0) + 1
                    pooled.append((rank, -score, query, sql, read_as(reading)))
        ordered = []
        for rank, negative, query, sql, said in sorted(pooled, key=lambda item: (item[0], item[1])):
            if sql not in seen:
                seen.add(sql)
                ordered.append(ScoredQuery(query, sql, -negative, (said,),
                                           (("rank", float(rank)), ("readers", built[sql] / len(self.stage2)))))
        return ordered

    def rescore(self, question, pool, shapes):
        """The readers' choice score (log odds that it answers the question) for each candidate that ran, None for
        the others: the choice model (choice.json) over the re-scorer's logit and what the pool says of the
        candidate (``choice_features``). ``shapes`` holds each candidate's (rows, columns) when it ran, else None.
        The re-scorer's logits depend only on the question and what each candidate says and returned, so a
        request's second selection over the same pool (the compose probe's, then the delegate's) reads them once
        (``_SCORES``)."""
        ran = [i for i, shape in enumerate(shapes) if shape is not None]
        records = [{"read_as": pool[i].evidence[0], "rows": shapes[i][0], "columns": shapes[i][1],
                    "score": pool[i].score, "rank": dict(pool[i].features).get("rank", 0.0)} for i in ran]
        key = (question, tuple(pool[i].sql for i in ran), tuple(shapes[i] for i in ran))
        with _LOCK:
            logits = _SCORES.get(key)
        if logits is None:
            from engine.readers import models

            logits = tuple(models.rescore(self.rescorer, self.tokenizer, question, records))
            _keep(_SCORES, key, logits)
        scores = [None] * len(pool)
        for i, logit in zip(ran, logits):
            features = dict(pool[i].features)
            x = choice_features(logit, pool[i].score, features.get("rank", 0.0), features.get("readers", 0.0),
                                shapes[i][0])
            scores[i] = self.choice["intercept"] + sum(w * v for w, v in zip(self.choice["coef"], x))
        return scores

    def confidence(self, pool, scores, shapes, chosen):
        """The choice's probability that the chosen candidate answers the question: fitted on held-out databases,
        so it is the confidence refusal reads. None for a candidate the readers did not score."""
        if scores[chosen] is None:
            return None
        return 1.0 / (1.0 + math.exp(-scores[chosen]))

    def confident(self, confidence):
        """Whether ``confidence`` meets the choice's threshold; an unscored choice, or a choice fitted without a
        threshold (an evaluation that records every confidence), always does."""
        threshold = self.choice.get("threshold")
        return confidence is None or threshold is None or confidence >= threshold


def either(readings, named):
    """The either-or variant of a one-block reading whose conditions all compare one column with one value the
    question names, when the question names more of that column's values (``named``, matched on the data,
    text.Schema.anchors): a row holds one value of a column, so "the total amount from Paris and Lyon" asks for the
    rows of either city. Yields nothing for any other reading."""
    if readings["structure"] != "single":
        return
    block = readings["blocks"][0]
    columns = {column for column, _, _ in block["where"]}
    if len(columns) != 1 or any(operator != "=" for _, operator, _ in block["where"]) or block.get("share"):
        return
    (column,) = columns
    values = named.get(column, [])
    if len(values) > 1 and {value for _, _, value in block["where"]} < set(values):
        yield {**readings, "blocks": [{**block, "where": [[column, "=", value] for value in values], "conj": "OR"}]}


def referenced(readings, graph):
    """The membership read on the column its listed values are values of, when the reading compares values of two
    kinds. "List the product names that no customer from Paris has bought" was read by every reader as "product id
    is not among (the product names Paris customers bought)" (2026-10-23), which matches no product, so the builder
    refuses it (readings._same_kind); the names a purchase lists reference products.product_name, so the products
    not among them are the products whose name is not among them. The column is the listed column itself when it
    belongs to the compared column's table, else the one column of that table a foreign key pairs it with. Yields
    nothing for any other reading."""
    if readings["structure"] != "NESTED":
        return
    outer, inner = readings["blocks"]
    if len(inner["select"]) != 1 or inner["select"][0][1] != "plain":
        return
    listed = inner["select"][0][2]
    listed_column = graph.column_map.get(tuple(listed.split(".", 1))) if "." in listed else None
    if listed_column is None:
        return
    where, changed = [], False
    for column, operator, value in outer["where"]:
        compared = graph.column_map.get(tuple(column.split(".", 1))) if "." in column else None
        partner = None
        if (operator in ("IN", "NOT IN") and compared is not None
                and {value_kind(compared.ref), value_kind(listed_column.ref)} == {"number", "text"}):
            partner = _partner(graph, listed_column.ref, compared.ref.table)
        if partner is not None and value_kind(partner) == value_kind(listed_column.ref):
            column, changed = f"{partner.table}.{partner.name}", True
        where.append([column, operator, value])
    if changed:
        yield {**readings, "blocks": [{**outer, "where": where}, inner]}


def _partner(graph, listed, table):
    """The column of ``table`` whose values ``listed`` holds: itself when it is of that table, else the one column a
    single-column foreign key pairs it with (None when none or more than one does)."""
    if listed.table == table:
        return listed
    partners = set()
    for key in graph.foreign_keys:
        if not key.additional_columns:
            for mine, other in ((key.from_column, key.to_column), (key.to_column, key.from_column)):
                if mine == listed and other.table == table:
                    partners.add(other.name)
    if len(partners) != 1:
        return None
    return graph.column_map[(table, partners.pop())].ref


_SEARCH = None
_POOLS = {}
_SCORES = {}
POOLS_KEPT = 64
# Serving answers requests on threads: one lock guards the readers' load and both memos.
_LOCK = threading.Lock()


def _keep(memo, key, value):
    """Keep ``value`` under ``key``, dropping the oldest entries past ``POOLS_KEPT``."""
    with _LOCK:
        memo[key] = value
        while len(memo) > POOLS_KEPT:
            memo.pop(next(iter(memo)))


def _signature(graph):
    """What a pool depends on in a graph: its columns, types, keys and values."""
    return (tuple((c.ref.table, c.ref.name, c.ref.type, hash(tuple(map(str, c.values)))) for c in graph.columns),
            tuple((k.from_column.table, k.from_column.name, k.to_column.table, k.to_column.name)
                  for k in graph.foreign_keys))


def reading_search():
    """The process's one ReadingSearch (loading the readers takes seconds and gigabytes)."""
    global _SEARCH
    if _SEARCH is None:
        with _LOAD_LOCK:
            if _SEARCH is None:
                _SEARCH = ReadingSearch()
    return _SEARCH


_LOAD_LOCK = threading.Lock()
