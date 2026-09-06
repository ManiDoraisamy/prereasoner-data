"""Serving provenance attached to result and trace columns.

This is display metadata, not a second planner. It records the tables the planner
actually received and propagates those origins through the operations the engine
actually emitted. The browser renders this contract verbatim and never infers a
source from a column name.
"""
from __future__ import annotations

from copy import deepcopy


_PASSTHROUGH_OPS = frozenset({"filter", "time_filter", "world_filter", "topn", "sort", "having"})
_DERIVED_OPS = frozenset({"group_agg", "yoy", "running", "divide", "share"})
_WIKIDATA_TABLES = frozenset({"city", "country", "u_s_state", "Elements", "Continents"})


def _record(kind: str, source: str, *, table=None, column=None, release_id=None,
            operation=None, inputs=()) -> dict:
    value = {"kind": kind, "source": source}
    for key, item in (("table", table), ("column", column), ("release_id", release_id),
                      ("operation", operation)):
        if item not in (None, ""):
            value[key] = item
    if inputs:
        value["inputs"] = list(dict.fromkeys(str(item) for item in inputs if item))
    return value


class ProvenanceContext:
    """Decorate one request's HTTP response and streamed trace with column lineage."""

    def __init__(self, tables, *, uploaded_count: int, reference_count: int = 0,
                 enrichment=None):
        self._by_column: dict[str, list[dict]] = {}
        self._last: dict[str, dict] = {}
        enrichment_sources = {}
        if enrichment is not None:
            for outcome in getattr(enrichment, "outcomes", ()):
                if getattr(outcome, "matched", False):
                    enrichment_sources[str(outcome.dataset_name)] = dict(outcome.provenance)

        for index, table in enumerate(tables):
            name = str(table.get("name") or "")
            if index < uploaded_count:
                meta = {"kind": "input", "source": "upload"}
            elif index < uploaded_count + reference_count:
                meta = {"kind": "reference", "source": "saved reference"}
            else:
                source = enrichment_sources.get(name, {})
                meta = {
                    "kind": "reference",
                    "source": source.get("source") or name,
                    "release_id": source.get("release_id"),
                }
            for column in table.get("columns") or ():
                item = _record(meta["kind"], meta["source"], table=name, column=str(column),
                               release_id=meta.get("release_id"))
                self._by_column.setdefault(str(column).casefold(), []).append(item)

    @staticmethod
    def _valid_columns(value, columns) -> bool:
        return (isinstance(value, list) and len(value) == len(columns)
                and all(isinstance(item, dict) and item.get("kind") for item in value))

    def _catalog_record(self, column: str) -> dict | None:
        candidates = self._by_column.get(str(column).casefold(), [])
        if not candidates:
            return None
        identities = {(item["kind"], item["source"], item.get("release_id")) for item in candidates}
        if len(identities) == 1:
            item = deepcopy(candidates[0])
            tables = sorted({candidate.get("table") for candidate in candidates if candidate.get("table")})
            if len(tables) > 1:
                item.pop("table", None)
                item["tables"] = tables
            return item
        return {
            "kind": "mixed",
            "source": "multiple inputs",
            "sources": [deepcopy(item) for item in candidates],
        }

    @staticmethod
    def _reference(source: str, column: str, *, table=None, release_id=None) -> dict:
        return _record("reference", source, table=table, column=column, release_id=release_id)

    def _classify_view(self, view: dict, *, resolve: bool = False) -> list[dict]:
        columns = [str(column) for column in (view.get("columns") or [])]
        existing = view.get("column_provenance")
        if self._valid_columns(existing, columns):
            return deepcopy(existing)
        if resolve:
            table = str(view.get("wtable") or "reference")
            source = "Wikidata" if table in _WIKIDATA_TABLES else table
            return [self._reference(source, column, table=table,
                                    release_id=view.get("source_release_id")) for column in columns]

        op = str(view.get("op") or "")
        previous = self._last
        records = []
        for column in columns:
            carried = previous.get(column.casefold())
            catalog = self._catalog_record(column)
            if op == "convert":
                low = column.casefold()
                if low == "converted":
                    item = _record("derived", "Prereasoner", column=column,
                                   operation="multiply", inputs=("amount", "exchange rate"))
                elif low == "rate_published" or "rate" in low:
                    item = self._reference("European Central Bank", column,
                                           table="exchange_rate", release_id=view.get("source_release_id"))
                else:
                    item = carried or catalog
            elif op == "world_join" and carried is None:
                item = self._reference("Wikidata", column)
            elif op in _PASSTHROUGH_OPS:
                item = carried or catalog
            elif op in _DERIVED_OPS:
                item = carried or catalog
                if item is None:
                    item = _record("derived", "Prereasoner", column=column,
                                   operation=op, inputs=tuple(previous))
            else:
                item = catalog or carried
            records.append(item or _record("derived", "Prereasoner", column=column,
                                           operation=op or "projection", inputs=tuple(previous)))
        return records

    def decorate_view(self, view: dict, *, resolve: bool = False) -> dict:
        if not isinstance(view, dict):
            return view
        decorated = deepcopy(view)
        columns = [str(column) for column in (decorated.get("columns") or [])]
        records = self._classify_view(decorated, resolve=resolve)
        decorated["column_provenance"] = records
        if not resolve:
            self._last = {column.casefold(): record for column, record in zip(columns, records)}
        return decorated

    def decorate_result(self, result: dict | None) -> dict | None:
        if not isinstance(result, dict):
            return result
        decorated = deepcopy(result)
        columns = [str(column) for column in (decorated.get("columns") or [])]
        existing = decorated.get("column_provenance")
        if not self._valid_columns(existing, columns):
            records = []
            for column in columns:
                records.append(self._last.get(column.casefold()) or self._catalog_record(column)
                               or _record("derived", "Prereasoner", column=column,
                                          operation="projection", inputs=tuple(self._last)))
            decorated["column_provenance"] = records
        return decorated

    def wrap_emitter(self, emit):
        """Decorate live RTDB values before they leave the process."""
        def wrapped(node, value, merge=False):
            if isinstance(node, str) and isinstance(value, dict):
                if node.startswith("resolve/") and value.get("columns"):
                    value = self.decorate_view(value, resolve=True)
                elif node.startswith("views/"):
                    value = self.decorate_view(value)
                elif node == "result":
                    value = self.decorate_result(value)
            return emit(node, value, merge)
        return wrapped

    def decorate_response(self, response):
        if not isinstance(response, dict):
            return response
        decorated = deepcopy(response)
        self._last = {}
        world = decorated.get("provenance")
        if isinstance(world, dict):
            release_id = world.get("release_id")
            if release_id:
                for view in decorated.get("views") or ():
                    if isinstance(view, dict) and view.get("op") == "convert":
                        view.setdefault("source_release_id", release_id)
        decorated["views"] = [self.decorate_view(view) for view in (decorated.get("views") or [])]
        decorated["result"] = self.decorate_result(decorated.get("result"))
        return decorated
