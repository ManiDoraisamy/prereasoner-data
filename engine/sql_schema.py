"""Typed schema graph and deterministic foreign-key path search."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property, lru_cache
import heapq
import math
from numbers import Real
import re
from typing import Any, Iterable, Sequence

from engine.numeric import NUMBER_TEXT, NUMBER_WORD, parse_decimal
from engine.relations import layout
from engine.sql_ast import ColumnRef, Join, SQLType


_WORD_RE = re.compile(NUMBER_WORD + r"|[^\W_]+(?:'[^\W_]+)?", re.UNICODE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T].*)?$")
_NAME_WORDS = frozenset({"name", "title", "label"})


@dataclass(frozen=True)
class SchemaColumn:
    ref: ColumnRef
    values: tuple[Any, ...] = ()
    index: int = -1


@dataclass(frozen=True)
class ForeignKey:
    from_column: ColumnRef
    to_column: ColumnRef
    confidence: float = 1.0
    additional_columns: tuple[tuple[ColumnRef, ColumnRef], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "additional_columns", tuple(
            (left, right) for left, right in self.additional_columns
        ))
        if any(
            left.table != self.from_column.table or right.table != self.to_column.table
            for left, right in self.additional_columns
        ):
            raise ValueError("all composite foreign-key columns must connect the same tables")
        if len(set(self.column_pairs)) != len(self.column_pairs):
            raise ValueError("foreign key contains duplicate column pairs")
        if (isinstance(self.confidence, bool) or not isinstance(self.confidence, Real)
                or not math.isfinite(float(self.confidence))
                or not 0.0 <= float(self.confidence) <= 1.0):
            raise ValueError("foreign-key confidence must be in [0,1]")

    @property
    def column_pairs(self) -> tuple[tuple[ColumnRef, ColumnRef], ...]:
        return ((self.from_column, self.to_column),) + self.additional_columns

    @property
    def is_composite(self) -> bool:
        return bool(self.additional_columns)

    @property
    def tables(self) -> frozenset[str]:
        return frozenset((self.from_column.table, self.to_column.table))

    @property
    def signature(self) -> tuple[str, ...]:
        return tuple(
            value
            for left, right in self.column_pairs
            for value in (left.table, left.name, right.table, right.name)
        )


@dataclass(frozen=True)
class JoinTree:
    root: str
    edge_indexes: tuple[int, ...]
    joins: tuple[Join, ...]
    confidence: float


class SchemaGraph:
    """Typed columns, observed values, and searchable foreign-key relationships."""

    def __init__(self, columns: Iterable[SchemaColumn], foreign_keys: Iterable[ForeignKey],
                 value_index: dict[str, tuple[tuple[ColumnRef, Any], ...]] | None = None):
        self.columns = tuple(columns)
        grouped: dict[str, list[SchemaColumn]] = {}
        for column in self.columns:
            grouped.setdefault(column.ref.table, []).append(column)
        self.by_table = {table: tuple(columns) for table, columns in grouped.items()}
        self.tables = tuple(grouped)
        self.column_map = {(column.ref.table, column.ref.name): column for column in self.columns}
        self.foreign_keys = tuple(
            foreign_key for foreign_key in foreign_keys
            if (foreign_key.from_column.table in self.by_table
                and foreign_key.to_column.table in self.by_table
                and all(
                    (left.table, left.name) in self.column_map
                    and (right.table, right.name) in self.column_map
                    for left, right in foreign_key.column_pairs
                ))
        )
        adjacency: dict[str, list[int]] = {table: [] for table in self.tables}
        for index, foreign_key in enumerate(self.foreign_keys):
            adjacency[foreign_key.from_column.table].append(index)
            adjacency[foreign_key.to_column.table].append(index)
        self.adjacency = {
            table: tuple(sorted(indexes, key=self._edge_sort_key))
            for table, indexes in adjacency.items()
        }
        self.value_index = self._build_value_index() if value_index is None else value_index
        self._without: dict[frozenset[str], SchemaGraph] = {}

    @classmethod
    def from_tables(cls, tables: Sequence[dict], fks: Sequence[dict | tuple]) -> "SchemaGraph":
        columns: list[SchemaColumn] = []
        refs: dict[tuple[str, str], ColumnRef] = {}
        index = 0
        for table in tables:
            name = str(table["name"])
            names = [str(column) for column in table["columns"]]
            rows = table.get("rows") or []
            for column_index, column_name in enumerate(names):
                raw_values = tuple(
                    _row_value(row, column_index, column_name) for row in rows
                )
                column_type = _infer_type(column_name, raw_values)
                values = tuple(_coerce_value(value, column_type) for value in raw_values)
                ref = ColumnRef(name, column_name, column_type)
                refs[(name, column_name)] = ref
                columns.append(SchemaColumn(ref, values, index))
                index += 1
        edges = (_foreign_key(foreign_key, refs) for foreign_key in fks)
        return cls(columns, (edge for edge in edges if edge is not None))

    @classmethod
    def from_planner(cls, schema: Sequence[dict], fks: Sequence[dict | tuple]) -> "SchemaGraph":
        """The graph of the planner's schema (`TableQuery.schema`), built once per request
        (`relations.memoized`): its value index reads every cell."""
        from engine.relations import memoized

        def key():
            return (tuple((str(column["table"]), str(column["name"]), int(column.get("idx", index)),
                           _planner_type(column), tuple(column.get("values") or ()))
                          for index, column in enumerate(schema)), repr(fks))

        return memoized("schema_graph", key, lambda: cls._from_planner(schema, fks))

    @classmethod
    def _from_planner(cls, schema: Sequence[dict], fks: Sequence[dict | tuple]) -> "SchemaGraph":
        columns: list[SchemaColumn] = []
        refs: dict[tuple[str, str], ColumnRef] = {}
        for index, column in enumerate(schema):
            ref = ColumnRef(
                str(column["table"]),
                str(column["name"]),
                _planner_type(column),
            )
            refs[(ref.table, ref.name)] = ref
            column_type = _planner_type(column)
            columns.append(SchemaColumn(
                ref,
                tuple(_coerce_value(value, column_type)
                      for value in (column.get("values") or ())),
                int(column.get("idx", index)),
            ))
        edges = (_foreign_key(foreign_key, refs) for foreign_key in fks)
        return cls(columns, (edge for edge in edges if edge is not None))

    @cached_property
    def layout_copies(self) -> tuple[tuple[str, ...], ...]:
        """The tables that are copies of one layout (`relations.layout`), as groups in the tables' order: an
        export of several columns kept per product or month, such as NT, SI and FF subscriptions. A table of
        one or two columns lists one thing (customers' names, orders' names), and a table a foreign key
        references is its own thing (two code lookups different keys reference): neither is a copy."""
        referenced = {foreign_key.to_column.table for foreign_key in self.foreign_keys}
        groups: dict[frozenset[str], list[str]] = {}
        for table in self.tables:
            if table not in referenced and len(self.by_table[table]) >= 3:
                groups.setdefault(layout(column.ref.name for column in self.by_table[table]), []).append(table)
        return tuple(tuple(group) for group in groups.values() if len(group) > 1)

    def reachable(self, tables: Iterable[str]) -> set[str]:
        """The tables the foreign keys connect to ``tables``, those included."""
        neighbours: dict[str, set[str]] = {}
        for fk in self.foreign_keys:
            neighbours.setdefault(fk.from_column.table, set()).add(fk.to_column.table)
            neighbours.setdefault(fk.to_column.table, set()).add(fk.from_column.table)
        reached = set(tables)
        frontier = list(reached)
        while frontier:
            for table in neighbours.get(frontier.pop(), ()):
                if table not in reached:
                    reached.add(table)
                    frontier.append(table)
        return reached

    def without(self, tables: frozenset[str]) -> "SchemaGraph":
        """This graph less ``tables`` and their foreign keys, built once per set of tables."""
        if tables not in self._without:
            index = {}
            for value, options in self.value_index.items():
                kept = tuple(option for option in options if option[0].table not in tables)
                if kept:
                    index[value] = kept
            self._without[tables] = SchemaGraph(
                (column for column in self.columns if column.ref.table not in tables), self.foreign_keys, index)
        return self._without[tables]

    def identifies(self, columns: Iterable[ColumnRef]) -> bool:
        """Whether one of ``columns`` names entities: it holds no value twice in its table, or it refers by a foreign
        key to a column that holds none twice ("purchases"."product_name" names products). A set difference matches
        rows by their values, so rows sharing values that name nothing leave or stay together."""
        def unique(ref):
            schema_column = self.column_map.get((ref.table, ref.name))
            values = [value for value in schema_column.values if value is not None] if schema_column else []
            return bool(values) and len(set(values)) == len(values)

        for column in columns:
            if unique(column) or any(
                    unique(other) for key in self.foreign_keys for left, right in key.column_pairs
                    for mine, other in ((left, right), (right, left))
                    if (mine.table, mine.name) == (column.table, column.name)):
                return True
        return False

    def identifying_column(self, table: str) -> ColumnRef | None:
        """The column telling ``table``'s rows apart, its key first (``is_surrogate_key``), else the first column holding
        no value twice; None when no column does."""
        columns = sorted(self.by_table.get(table, ()), key=lambda column: (
            0 if is_surrogate_key(column.ref.name) else 1, column.index))
        return next((column.ref for column in columns if self.identifies((column.ref,))), None)

    def display_columns(self, table: str) -> tuple[ColumnRef, ...]:
        columns = list(self.by_table.get(table, ()))
        columns.sort(key=lambda column: (
            0 if set(name_words(column.ref.name)) & _NAME_WORDS else 1,
            0 if column.ref.type == SQLType.TEXT else 1,
            1 if is_surrogate_key(column.ref.name) else 0,
            column.index,
        ))
        return tuple(column.ref for column in columns)

    def join_trees(
        self,
        required_tables: Iterable[str],
        preferred_root: str | None = None,
        limit: int = 8,
        max_hops: int = 6,
    ) -> tuple[JoinTree, ...]:
        required = frozenset(table for table in required_tables if table in self.by_table)
        if not required:
            return ()
        root = preferred_root if preferred_root in required else sorted(required)[0]
        if len(required) == 1:
            return (JoinTree(root, (), (), 1.0),)

        queue: list[tuple[int, float, tuple, frozenset[str], tuple[int, ...]]] = []
        heapq.heappush(queue, (0, 0.0, (), frozenset((root,)), ()))
        seen: set[tuple[frozenset[str], tuple[int, ...]]] = set()
        found: list[JoinTree] = []
        expansions = 0
        while queue and len(found) < limit and expansions < 5000:
            _, _, _, nodes, edges = heapq.heappop(queue)
            state_key = (nodes, edges)
            if state_key in seen:
                continue
            seen.add(state_key)
            expansions += 1
            if required <= nodes:
                joins = self._materialize_joins(root, edges)
                confidence = sum(
                    self.foreign_keys[index].confidence for index in edges
                ) / max(len(edges), 1)
                found.append(JoinTree(root, edges, joins, confidence))
                continue
            if len(edges) >= max_hops:
                continue
            candidates = sorted(
                {index for table in nodes for index in self.adjacency.get(table, ())},
                key=self._edge_sort_key,
            )
            for edge_index in candidates:
                foreign_key = self.foreign_keys[edge_index]
                outside = foreign_key.tables - nodes
                if len(outside) != 1:
                    continue
                new_edges = tuple(sorted(edges + (edge_index,)))
                new_nodes = nodes | outside
                signature = tuple(self.foreign_keys[index].signature for index in new_edges)
                confidence_cost = -sum(
                    self.foreign_keys[index].confidence for index in new_edges
                )
                heapq.heappush(
                    queue,
                    (len(new_edges), confidence_cost, signature, new_nodes, new_edges),
                )
        return tuple(found)

    def _materialize_joins(self, root: str, edge_indexes: tuple[int, ...]) -> tuple[Join, ...]:
        remaining = list(edge_indexes)
        joined = {root}
        joins = []
        while remaining:
            picked = None
            for edge_index in sorted(remaining, key=self._edge_sort_key):
                foreign_key = self.foreign_keys[edge_index]
                left_joined = foreign_key.from_column.table in joined
                right_joined = foreign_key.to_column.table in joined
                if left_joined == right_joined:
                    continue
                new_table = (
                    foreign_key.to_column.table
                    if left_joined else foreign_key.from_column.table
                )
                joins.append(Join(
                    new_table,
                    foreign_key.from_column,
                    foreign_key.to_column,
                    additional=foreign_key.additional_columns,
                ))
                joined.add(new_table)
                picked = edge_index
                break
            if picked is None:
                raise ValueError("join edge set is disconnected")
            remaining.remove(picked)
        return tuple(joins)

    def _edge_sort_key(self, edge_index: int) -> tuple:
        edge = self.foreign_keys[edge_index]
        return (-edge.confidence, edge.signature)

    def _build_value_index(self) -> dict[str, tuple[tuple[ColumnRef, Any], ...]]:
        values: dict[str, list[tuple[ColumnRef, Any]]] = {}
        for column in self.columns:
            seen = set()
            for value in distinct_values(column.values):
                normalized = normalize_value(value)
                if not normalized or normalized in seen or NUMBER_TEXT.match(normalized):
                    continue
                seen.add(normalized)
                values.setdefault(normalized, []).append((column.ref, value))
        return {value: tuple(options) for value, options in values.items()}


def distinct_values(values: Sequence[Any]) -> Sequence[Any]:
    """The values without repeats, first occurrence first, 1 and True apart: a 34,500-row workbook read
    each repeated currency, plan and status cell anew, thousands of times a column (2026-10-04)."""
    try:
        return [value for _, value in dict.fromkeys((type(value), value) for value in values)]
    except TypeError:                                          # an unhashable cell
        return values


def _foreign_key(
    raw: dict | tuple, refs: dict[tuple[str, str], ColumnRef]
) -> ForeignKey | None:
    if isinstance(raw, dict):
        from_table, to_table = str(raw["from_table"]), str(raw["to_table"])
        from_columns = raw.get("from_cols", (raw["from_col"],) if "from_col" in raw else ())
        to_columns = raw.get("to_cols", (raw["to_col"],) if "to_col" in raw else ())
        # Only an absent confidence defaults to 1.0: `or 1.0` turned an explicit 0.0 into full confidence.
        stated = raw.get("conf", raw.get("confidence"))
        confidence = 1.0 if stated is None else float(stated)
    else:
        from_table, from_column, to_table, to_column = map(str, raw[:4])
        from_columns, to_columns = (from_column,), (to_column,)
        confidence = float(raw[4]) if len(raw) > 4 else 1.0
    if isinstance(from_columns, str) or isinstance(to_columns, str):
        return None
    from_columns, to_columns = tuple(map(str, from_columns)), tuple(map(str, to_columns))
    if not from_columns or len(from_columns) != len(to_columns):
        return None
    pairs = tuple(
        (_resolve_ref(refs, from_table, from_column),
         _resolve_ref(refs, to_table, to_column))
        for from_column, to_column in zip(from_columns, to_columns)
    )
    if any(left is None or right is None for left, right in pairs):
        return None
    typed_pairs = tuple((left, right) for left, right in pairs if left is not None and right is not None)
    return ForeignKey(typed_pairs[0][0], typed_pairs[0][1], confidence, typed_pairs[1:])


def _resolve_ref(
    refs: dict[tuple[str, str], ColumnRef], table: str, column: str
) -> ColumnRef | None:
    exact = refs.get((table, column))
    if exact is not None:
        return exact

    def normalized_table(value: str) -> str:
        return (re.sub(r"\W+", "_", value).strip("_") or "t").casefold()

    matches = [
        ref for (candidate_table, candidate_column), ref in refs.items()
        if normalized_table(candidate_table) == normalized_table(table)
        and candidate_column.casefold() == column.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def _planner_type(column: dict) -> SQLType:
    if column.get("is_date"):
        return SQLType.DATE
    affinity = str(column.get("affinity", "")).upper()
    return {
        "INTEGER": SQLType.INTEGER,
        "REAL": SQLType.REAL,
        "NUMERIC": SQLType.REAL,
        "BOOLEAN": SQLType.BOOLEAN,
        "DATE": SQLType.DATE,
        "TEXT": SQLType.TEXT,
    }.get(affinity, SQLType.UNKNOWN)


def _infer_type(name: str, values: Sequence[Any]) -> SQLType:
    populated = [value for value in values if value is not None and str(value).strip()]
    if populated and all(_DATE_RE.match(str(value).strip()) for value in populated):
        return SQLType.DATE
    if populated and all(NUMBER_TEXT.match(str(value).strip()) for value in populated):
        return SQLType.REAL if any("." in str(value) for value in populated) else SQLType.INTEGER
    if set(name_words(name)) & {"date", "datetime", "timestamp"}:
        return SQLType.DATE
    return SQLType.TEXT


def _coerce_value(value: Any, value_type: SQLType) -> Any:
    if value is None:
        return None
    if value_type.numeric:
        if isinstance(value, bool):
            return None
        text = str(value).strip()
        if not isinstance(value, Real) and not NUMBER_TEXT.fullmatch(text):
            return None
        try:
            numeric = parse_decimal(value)
        except ValueError:
            return None
        return int(numeric) if value_type == SQLType.INTEGER else numeric
    if value_type == SQLType.BOOLEAN:
        if isinstance(value, bool):
            return value
        if value in {0, 1}:
            return bool(value)
        return None
    return value


def _row_value(row: Any, index: int, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return row[index] if index < len(row) else None


def name_words(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(word.lower() for word in re.findall(r"[^\W_]+", spaced, re.UNICODE))


# A column is a surrogate key (a primary or foreign key, never a measure) when the last word of its name is
# an identifier word, or its whole name is a row number. A code ('country_code') is a natural attribute
# people ask for by name, not a surrogate key. This is the engine's one surrogate-key rule.
SURROGATE_KEY_WORDS = frozenset({"id", "ids", "uid", "uuid", "guid", "identifier", "key", "pk"})


def is_surrogate_key(name: str) -> bool:
    """'order ID', 'customer_id', 'OrderID' and 'index' are keys; 'orders', 'idea', 'paid' and 'price index'
    are not."""
    words = name_words(re.sub(r' \[column [A-Z]+\](?: \d+)?$', '', name))
    return bool(words) and (words[-1] in SURROGATE_KEY_WORDS or words == ("index",))


def normalize_value(value: Any) -> str:
    """A cell's comparable words, the form ``value_index`` keys and the search's text filters read."""
    if value is None:
        return ""
    return " ".join(canon(token) for token in _WORD_RE.findall(str(value).lower()))


@lru_cache(maxsize=1 << 16)                                    # millions of calls a request, few words
def canon(word: str) -> str:
    """A word's comparable form, the one the search, its expansions, the ranker and the value index read
    questions, names and values with: lower case, and a plural as its singular ("courses" -> "course",
    "matches" -> "match", "classes" -> "class", "cities" -> "city"). Spider DEV, 2026-10-02: "courses" read
    "cours", "matches" "matche" and "finishes" "finishe", so a question saying "course", "match" or
    "finish" named no Courses, matches or best_finish."""
    word = word.lower().strip()
    if "," in word and NUMBER_TEXT.match(word):
        return word.replace(",", "")                      # "1,000" is the number 1000 a query compares
    if word == "handed":
        return "hand"
    if word == "ids":
        return "id"
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses") or (len(word) > 4 and word.endswith(("ches", "shes", "xes"))):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
