"""Typed schema graph and deterministic foreign-key path search."""
from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
from numbers import Real
import re
from typing import Any, Iterable, Sequence

from engine.sql_ast import ColumnRef, Join, SQLType


_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?")
_NUMBER_RE = re.compile(r"^-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")
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

    @property
    def tables(self) -> frozenset[str]:
        return frozenset((self.from_column.table, self.to_column.table))

    @property
    def signature(self) -> tuple[str, str, str, str]:
        return (
            self.from_column.table,
            self.from_column.name,
            self.to_column.table,
            self.to_column.name,
        )


@dataclass(frozen=True)
class JoinTree:
    root: str
    edge_indexes: tuple[int, ...]
    joins: tuple[Join, ...]
    confidence: float


class SchemaGraph:
    """Typed columns, observed values, and searchable foreign-key relationships."""

    def __init__(self, columns: Iterable[SchemaColumn], foreign_keys: Iterable[ForeignKey]):
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
                and foreign_key.to_column.table in self.by_table)
        )
        adjacency: dict[str, list[int]] = {table: [] for table in self.tables}
        for index, foreign_key in enumerate(self.foreign_keys):
            adjacency[foreign_key.from_column.table].append(index)
            adjacency[foreign_key.to_column.table].append(index)
        self.adjacency = {
            table: tuple(sorted(indexes, key=self._edge_sort_key))
            for table, indexes in adjacency.items()
        }
        self.value_index = self._build_value_index()

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

    def display_columns(self, table: str) -> tuple[ColumnRef, ...]:
        columns = list(self.by_table.get(table, ()))
        columns.sort(key=lambda column: (
            0 if set(_name_words(column.ref.name)) & _NAME_WORDS else 1,
            0 if column.ref.type == SQLType.TEXT else 1,
            1 if _is_id(column.ref.name) else 0,
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
            for value in column.values:
                normalized = _normalize_value(value)
                if not normalized or normalized in seen or _NUMBER_RE.match(normalized):
                    continue
                seen.add(normalized)
                values.setdefault(normalized, []).append((column.ref, value))
        return {value: tuple(options) for value, options in values.items()}


def _foreign_key(
    raw: dict | tuple, refs: dict[tuple[str, str], ColumnRef]
) -> ForeignKey | None:
    if isinstance(raw, dict):
        from_table, from_column = str(raw["from_table"]), str(raw["from_col"])
        to_table, to_column = str(raw["to_table"]), str(raw["to_col"])
        confidence = float(raw.get("conf", raw.get("confidence", 1.0)) or 1.0)
    else:
        from_table, from_column, to_table, to_column = map(str, raw[:4])
        confidence = float(raw[4]) if len(raw) > 4 else 1.0
    left = refs.get((from_table, from_column))
    right = refs.get((to_table, to_column))
    return ForeignKey(left, right, confidence) if left is not None and right is not None else None


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
    if populated and all(_NUMBER_RE.match(str(value).strip()) for value in populated):
        return SQLType.REAL if any("." in str(value) for value in populated) else SQLType.INTEGER
    if set(_name_words(name)) & {"date", "datetime", "timestamp"}:
        return SQLType.DATE
    return SQLType.TEXT


def _coerce_value(value: Any, value_type: SQLType) -> Any:
    if value is None:
        return None
    if value_type.numeric:
        if isinstance(value, bool):
            return None
        if isinstance(value, Real):
            numeric = float(value)
        else:
            text = str(value).strip()
            if not _NUMBER_RE.fullmatch(text):
                return None
            numeric = float(text.replace(",", ""))
        if not math.isfinite(numeric):
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


def _name_words(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(word.lower() for word in re.findall(r"[A-Za-z0-9]+", spaced))


def _is_id(name: str) -> bool:
    words = _name_words(name)
    return bool(words) and words[-1] in {"id", "identifier", "key"}


def _normalize_value(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(_canon(token) for token in _WORD_RE.findall(str(value).lower()))


def _canon(word: str) -> str:
    word = word.lower().strip()
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("ses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
