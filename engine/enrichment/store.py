"""Release-pinned, bounded access to registered enrichment datasets.

This module performs no source synchronization and no network access. It accepts only
validated registry definitions, resolves one active release, and replays a caller-supplied
pin even after that release is retired.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from engine.enrichment.registry import (
    DatasetDefinition, EmbeddedStorage, LookupCardinality, PostgresStorage, SnapshotPin,
)


MAX_LOOKUP_KEYS = 200
MAX_LOADED_ROWS = 5000


class SourceContractError(RuntimeError):
    """The materialized source does not satisfy its registered contract."""


@dataclass(frozen=True)
class LoadedDataset:
    definition: DatasetDefinition
    snapshot: SnapshotPin
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    multi_match_keys: tuple[tuple, ...] = ()


class SnapshotStore:
    def __init__(self, connection_factory: Callable[[], object]):
        if not callable(connection_factory):
            raise ValueError("connection_factory must be callable")
        self._connection_factory = connection_factory

    def active_snapshot(self, definition: DatasetDefinition) -> SnapshotPin:
        if isinstance(definition.storage, EmbeddedStorage):
            return SnapshotPin("embedded", definition.embedded_snapshot_id, 1,
                               definition.definition_id)
        storage = definition.storage
        assert isinstance(storage, PostgresStorage)
        sql = (
            f'SELECT release_id, schema_version FROM "{storage.relation.schema_name}"."release" '
            "WHERE status='active'"
        )
        connection = self._connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
        finally:
            cursor.close()
            connection.close()
        if len(rows) != 1:
            raise SourceContractError(
                f"{definition.name}: expected one active release, found {len(rows)}"
            )
        release_id, schema_version = rows[0]
        return SnapshotPin(storage.relation.schema_name, str(release_id),
                           int(schema_version), definition.definition_id)

    def load_by_keys(self, definition: DatasetDefinition, snapshot: SnapshotPin,
                     keys: Iterable[tuple], *, max_rows: int = MAX_LOADED_ROWS) -> LoadedDataset:
        normalized_keys = self._validate_lookup(definition, snapshot, keys, max_rows)
        if isinstance(definition.storage, EmbeddedStorage):
            return self._load_embedded(definition, snapshot, normalized_keys, max_rows)
        return self._load_postgres(definition, snapshot, normalized_keys, max_rows)

    @staticmethod
    def _validate_lookup(definition: DatasetDefinition, snapshot: SnapshotPin,
                         keys: Iterable[tuple], max_rows: int) -> tuple[tuple, ...]:
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_LOADED_ROWS:
            raise ValueError(f"max_rows must be an integer in [1,{MAX_LOADED_ROWS}]")
        normalized = []
        seen = set()
        for raw_key in keys:
            if isinstance(raw_key, (str, bytes)):
                raise ValueError("lookup keys must be tuples")
            key = tuple(raw_key)
            if len(key) != len(definition.lookup_key):
                raise ValueError(f"{definition.name}: lookup key arity mismatch")
            if any(value is None for value in key):
                raise ValueError(f"{definition.name}: lookup key values cannot be null")
            if key not in seen:
                normalized.append(key)
                seen.add(key)
        if not normalized:
            raise ValueError("at least one lookup key is required")
        if len(normalized) > MAX_LOOKUP_KEYS:
            raise ValueError(f"at most {MAX_LOOKUP_KEYS} lookup keys may be loaded")
        expected_schema = (
            "embedded" if isinstance(definition.storage, EmbeddedStorage)
            else definition.storage.relation.schema_name
        )
        if snapshot.source_schema != expected_schema:
            raise ValueError(f"{definition.name}: snapshot belongs to another source")
        if snapshot.contract_hash != definition.definition_id:
            raise ValueError(f"{definition.name}: snapshot contract hash is stale")
        return tuple(normalized)

    @staticmethod
    def _load_embedded(definition: DatasetDefinition, snapshot: SnapshotPin,
                       keys: tuple[tuple, ...], max_rows: int) -> LoadedDataset:
        storage = definition.storage
        assert isinstance(storage, EmbeddedStorage)
        positions = tuple(definition.columns.index(column) for column in definition.lookup_key)
        wanted = set(keys)
        rows = tuple(row for row in storage.rows
                     if tuple(row[index] for index in positions) in wanted)
        return SnapshotStore._finish(definition, snapshot, rows[:max_rows + 1], max_rows)

    def _load_postgres(self, definition: DatasetDefinition, snapshot: SnapshotPin,
                       keys: tuple[tuple, ...], max_rows: int) -> LoadedDataset:
        storage = definition.storage
        assert isinstance(storage, PostgresStorage)
        columns = ", ".join(f'"{column}"' for column in definition.columns)
        clauses = []
        params: list[object] = [snapshot.release_id]
        for key in keys:
            clauses.append("(" + " AND ".join(
                f'"{column}"=%s' for column in definition.lookup_key
            ) + ")")
            params.extend(key)
        order = ", ".join(f'"{column}"' for column in definition.identity_key)
        sql = (
            f'SELECT {columns} FROM "{storage.relation.schema_name}".'
            f'"{storage.relation.table_name}" WHERE "release_id"=%s AND '
            f'({" OR ".join(clauses)}) ORDER BY {order} LIMIT %s'
        )
        params.append(max_rows + 1)
        connection = self._connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute(sql, tuple(params))
            rows = tuple(tuple(row) for row in cursor.fetchall())
        finally:
            cursor.close()
            connection.close()
        return self._finish(definition, snapshot, rows, max_rows)

    @staticmethod
    def _finish(definition: DatasetDefinition, snapshot: SnapshotPin,
                rows: tuple[tuple, ...], max_rows: int) -> LoadedDataset:
        if len(rows) > max_rows:
            raise SourceContractError(f"{definition.name}: bounded lookup exceeded {max_rows} rows")
        positions = tuple(definition.columns.index(column) for column in definition.lookup_key)
        counts: dict[tuple, int] = {}
        for row in rows:
            key = tuple(row[index] for index in positions)
            counts[key] = counts.get(key, 0) + 1
        multi = tuple(sorted(key for key, count in counts.items() if count > 1))
        if definition.cardinality == LookupCardinality.ONE and multi:
            raise SourceContractError(
                f"{definition.name}: unique lookup returned multiple rows for {multi[0]!r}"
            )
        return LoadedDataset(definition, snapshot, definition.columns, rows, multi)
