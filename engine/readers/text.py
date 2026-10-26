"""How both reading models see a schema: one serialization that training examples and serving share. It reads
only what the engine receives: tables of rows, the column types the engine infers from them
(engine.sql_schema.SchemaGraph), and the foreign keys the graph holds. So an uploaded sheet is read exactly as a
benchmark database is.

Stage 1 reads one table at a time:   "<table words> : <column words> [<type>] (<samples>) | ... joins <tables>"
Stage 2 reads the kept tables:       "* | <table words> : <column words> [<type>] (<values>) ; ... | ..."

Words are names split at underscores and camel case, lower-cased. Values are a column's distinct non-empty values in
row order, cut to 20 characters; stage 2 lists all of a column's values when it has at most 6, else 2 samples. Stage 2
lists first the values the question itself says (Schema.anchors): a text value of at least 3 letters that a run of
the question's words spells, case aside ("players from college UCLA" lists UCLA beside the college). Matching is
on the data, not on any word list, so it reads every language the data is written in.
"""
import re

from engine.sql_ast import SQLType

STAGE1_SAMPLES = 3
TOP_TABLES = 6                                     # the tables serving keeps for stage 2
LIST_ALL = 6
SAMPLES = 2
VALUE_CHARS = 20
TYPE_WORDS = {SQLType.INTEGER: "number", SQLType.REAL: "number", SQLType.DATE: "date", SQLType.BOOLEAN: "boolean"}
ANCHORS = 3                                        # the question's values listed per column, at most
ANCHOR_WORDS = 6                                   # the longest run of question words a value is matched against
EDGE = ".,?!;:\"'()"


def normal(text):
    return " ".join(str(text).lower().split()).strip(EDGE)


def words(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name)).replace("_", " ")
    return " ".join(spaced.split()).lower()


class Schema:
    """The tables, columns, inferred types, first distinct values, and foreign-key neighbours of a schema graph."""

    def __init__(self, graph):
        self.graph = graph
        self.tables = list(graph.tables)
        self.columns = {t: [c.ref.name for c in graph.by_table[t]] for t in self.tables}
        self.types = {(c.ref.table, c.ref.name): TYPE_WORDS.get(c.ref.type, "text") for c in graph.columns}
        self.values = {}
        for column in graph.columns:
            seen = []
            for value in column.values:
                if value is None or str(value).strip() == "":
                    continue
                text = str(value).replace("|", "/").replace(";", ",").replace("\n", " ")[:VALUE_CHARS]
                if text not in seen:
                    seen.append(text)
                    if len(seen) > LIST_ALL:
                        break
            self.values[(column.ref.table, column.ref.name)] = seen
        # Every distinct text value of every text column, by its normal form, for the question's anchors.
        self.text_values = {}
        for column in graph.columns:
            if self.types[(column.ref.table, column.ref.name)] != "text":
                continue
            index = {}
            for value in column.values:
                if not isinstance(value, str):
                    continue
                key = normal(value)
                if len(key) >= 3 and not key.replace(".", "").isdigit():
                    index.setdefault(key, value.strip())
            self.text_values[(column.ref.table, column.ref.name)] = index
        self.related = {t: set() for t in self.tables}
        for key in graph.foreign_keys:
            a, b = key.from_column.table, key.to_column.table
            if a != b:
                self.related[a].add(b)
                self.related[b].add(a)


    def anchors(self, question):
        """{(table, column): [values]}: the text values a run of the question's words spells, in the order the
        question says them."""
        tokens = question.split()
        found = {}
        for length in range(min(ANCHOR_WORDS, len(tokens)), 0, -1):
            for start in range(len(tokens) - length + 1):
                key = normal(" ".join(tokens[start:start + length]))
                if len(key) < 3:
                    continue
                for column, index in self.text_values.items():
                    if key in index:
                        found.setdefault(column, []).append((start, index[key]))
        return {column: [value for _, value in sorted(set(hits))][:ANCHORS] for column, hits in found.items()}


def table_text(schema, table, max_columns=48):
    """Stage 1: (text, [(column, start, end)]) for one table."""
    text = words(table) + " :"
    spans = []
    for name in schema.columns[table][:max_columns]:
        text += " "
        start = len(text)
        text += words(name)
        spans.append((name, start, len(text)))
        values = schema.values[(table, name)][:STAGE1_SAMPLES]
        text += f" [{schema.types[(table, name)]}]" + (f" ({', '.join(values)})" if values else "") + " |"
    if schema.related[table]:
        text += " joins " + ", ".join(words(t) for t in sorted(schema.related[table]))
    return text, spans


def reading_text(schema, tables, columns_of, samples=True, anchors=None):
    """Stage 2: (text, star span, {table: span}, {"t.c": (span, [(value, start, end)])}). The question's own values
    (``anchors``) come first; without ``samples`` only the columns with few distinct values list the rest."""
    anchors = anchors or {}
    text = "*"
    star = (0, 1)
    table_spans, column_spans = {}, {}
    for table in tables:
        text += " | "
        start = len(text)
        text += words(table)
        table_spans[table] = (start, len(text))
        text += " :"
        for i, column in enumerate(columns_of[table]):
            text += " " if i == 0 else " ; "
            start = len(text)
            text += words(column)
            span = (start, len(text))
            values = schema.values[(table, column)]
            listed = list(anchors.get((table, column), []))
            listed += [v for v in (values if len(values) <= LIST_ALL else (values[:SAMPLES] if samples else []))
                       if v not in listed]
            text += f" [{schema.types[(table, column)]}]"
            value_spans = []
            if listed:
                text += " ("
                for j, value in enumerate(listed):
                    if j:
                        text += ", "
                    value_spans.append((value, len(text), len(text) + len(value)))
                    text += value
                text += ")"
            column_spans[f"{table}.{column}"] = (span, value_spans)
    return text, star, table_spans, column_spans


def fit(tokenizer, budget, question, schema, tables, columns_of, droppable, anchored=True):
    """(kept tables, reading_text(...)) within the token budget: the samples go before a table does, then the
    droppable tables in order, each without samples. A reading of a dropped table can never be right, and a table
    without samples still lists its columns and their few values: on calibration the readers' pools hold 10 more
    right answers this way (783 of 1,028), most of them over wide databases whose samples filled the budget
    (2026-10-25). ``anchored`` lists the question's own values first (a model reads with them or without)."""
    kept, serialized = list(tables), None
    anchors = schema.anchors(question) if anchored else {}
    for dropped in range(len(droppable) + 1):
        kept = [t for t in tables if t not in droppable[:dropped]]
        for samples in (True, False):
            serialized = reading_text(schema, kept, columns_of, samples, anchors)
            if len(tokenizer(question, serialized[0])["input_ids"]) <= budget:
                return kept, serialized
    return kept, serialized
