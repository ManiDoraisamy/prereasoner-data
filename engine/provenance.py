"""Serving provenance attached to result and trace columns.

This is display metadata, not a second planner. It records the tables the planner
actually received and propagates those origins through the operations the engine
actually emitted. The browser renders this contract verbatim and never infers a
source from a column name.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from bisect import bisect_right

from engine.numeric import wire_document


_PASSTHROUGH_OPS = frozenset({"filter", "time_filter", "world_filter", "topn", "sort", "having"})
_DERIVED_OPS = frozenset({"group_agg", "yoy", "running", "divide", "share"})
_WIKIDATA_TABLES = frozenset({"city", "country", "u_s_state", "elements", "continents"})


class InputLineage:
    """Cell/row evidence for one immutable input and explicit section scopes.

    This does not infer layout, rewrite a table, or choose a query. A normalization
    owner supplies source-supported scopes; this object records/replays their cell
    consequences. Missing cells outside a declared scope remain missing. Row ordinals
    preserve duplicate observations. Coordinates are zero-based input coordinates,
    not claimed Excel addresses unless the importer supplies that separate mapping.
    """

    def __init__(self, table, *, source_id, import_record=None):
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("cell lineage requires an explicit source identity")
        self.source_id = source_id
        self.name = str(table["name"])
        self.columns = tuple(str(c) for c in table["columns"])
        self.rows = tuple(tuple(row.get(c) for c in self.columns) if isinstance(row, dict) else tuple(row)
                          for row in table["rows"])
        if not self.columns or any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("cell lineage requires a rectangular input table")
        self.source_layout = self._layout(import_record)
        self._range_starts = tuple(r[0] for r in self.source_layout["sourceRows"]["ranges"]) if self.source_layout else ()
        # Type-tag values: an integer 123 and a string '123' are different source
        # observations even though JSON wire handling protects large integers as text.
        tagged = [[{"type": type(v).__name__, "value": wire_document(v)} for v in row] for row in self.rows]
        self.source_sha256 = hashlib.sha256(json.dumps(
            {"table": self.name, "columns": self.columns, "rows": tagged, "source_layout": self.source_layout},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        self._scopes = {}
        self._row_key_lookups = {}

    def _layout(self, record):
        # One validation owner serves HTTP, persistence, caches and trace replay.
        from engine.request_validation import validate_import_metadata
        return validate_import_metadata(record, row_count=len(self.rows), column_names=self.columns)

    def _source_cell(self, row, column):
        if self.source_layout is None:
            return None
        run = self.source_layout["sourceRows"]["ranges"][bisect_right(self._range_starts, row) - 1]
        number = run[1] + row - run[0]
        letter = self.source_layout["columnBindings"][column]["column"]
        return {"sheet": self.source_layout["sourceSheet"], "row": number, "column": letter,
                "address": f"{letter}{number}"}

    @staticmethod
    def _blank(value):
        return value is None or isinstance(value, str) and not value.strip()

    def _position(self, row, column):
        if (type(row) is not int or type(column) is not int
                or not 0 <= row < len(self.rows) or not 0 <= column < len(self.columns)):
            raise ValueError("source cell outside the captured table")

    def row_id(self, row):
        self._position(row, 0)
        return f"{self.source_id}@{self.source_sha256}/row/{row}"

    def section_labels(self, column, scopes, *, evidence):
        """Declare disjoint [start, stop) scopes anchored at their first input row.

        A new declaration may not overwrite an earlier scope. Labels inside a scope
        must agree with its anchor; conflicting labels reveal a wrong boundary.
        Evidence identifies a source/layout record, never a question or gold answer.
        """
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("section scope requires its source/layout evidence identity")
        if type(column) is not int or not 0 <= column < len(self.columns):
            raise ValueError("section label column outside the captured table")
        if not isinstance(scopes, tuple):
            raise ValueError("section scopes must be an immutable tuple")
        proposed = dict(self._scopes)
        last_stop = -1
        for scope in scopes:
            if (not isinstance(scope, tuple) or len(scope) != 2
                    or any(type(i) is not int for i in scope)):
                raise ValueError("section scope must be an integer start/stop pair")
            start, stop = scope
            if not 0 <= start < stop <= len(self.rows) or start < last_stop:
                raise ValueError("section scopes must be ordered, disjoint and within the source")
            anchor = self.rows[start][column]
            if self._blank(anchor):
                raise ValueError("section scope anchor is blank")
            for row in range(start, stop):
                if (row, column) in proposed:
                    raise ValueError("section scope overwrites an existing declaration")
                value = self.rows[row][column]
                if not self._blank(value) and (type(value) is not type(anchor) or value != anchor):
                    raise ValueError("section scope crosses a conflicting source label")
                proposed[(row, column)] = {"anchor_row": start, "scope": [start, stop], "evidence": evidence}
            last_stop = stop
        self._scopes = proposed
        return self

    def cell(self, row, column):
        self._position(row, column)
        value = self.rows[row][column]
        scope = self._scopes.get((row, column)) if self._blank(value) else None
        inherited = scope is not None
        anchor = scope["anchor_row"] if inherited else row
        inputs = {"row": anchor, "column": column}
        if self.source_layout is not None:
            inputs["source_cell"] = self._source_cell(anchor, column)
        return {"row_id": self.row_id(row), "source_id": self.source_id,
                "source_sha256": self.source_sha256, "table": self.name,
                "row": row, "column": column, "column_name": self.columns[column],
                "original": wire_document(value), "value": wire_document(self.rows[anchor][column]),
                "value_representation": "canonical_parsed_input",
                "operation": "section_label" if inherited else "source_cell",
                "inputs": [inputs], "source_cell": self._source_cell(row, column),
                "scope": deepcopy(scope["scope"]) if inherited else None,
                "evidence": scope["evidence"] if inherited else None}

    def contributor_record(self, rows, columns):
        """Describe an executor-supplied contributor set; never select those rows here."""
        if (not isinstance(rows, tuple) or not isinstance(columns, tuple)
                or any(type(i) is not int or not 0 <= i < len(self.rows) for i in rows)
                or any(type(i) is not int or not 0 <= i < len(self.columns) for i in columns)
                or len(set(columns)) != len(columns)):
            raise ValueError("contributors require valid explicit row/column index tuples")
        cells = [self.cell(row, column) for row in rows for column in columns]
        return {"source_id": self.source_id, "source_sha256": self.source_sha256,
                "table": self.name, "row_ids": [self.row_id(row) for row in rows],
                "cell_lineage": cells, "row_count": len(rows), "distinct_source_rows": len(set(rows)),
                "selection_owner": "caller_executor; lineage_does_not_select"}

    def resolve_row_keys(self, keys, columns, *, rowid_source_sha256=None, key_affinities=None):
        """Resolve executor-provided database keys against this exact input.

        A native SQLite rowid is an input ordinal only when the executor attests
        that this snapshot was inserted in its original order. Natural keys must
        identify exactly one row. Neither path invents an origin for duplicates.
        """
        if not isinstance(keys, (list, tuple)):
            raise ValueError("source row keys must be an explicit sequence")

        key_affinities = {} if key_affinities is None else key_affinities

        def scalar(value, column):
            if column in key_affinities:
                from engine.prepared import _cell
                value = _cell(value, key_affinities[column])
            value = wire_document(value)
            return type(value).__name__, json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)

        rows = []
        for key in keys:
            if (not isinstance(key, dict) or set(key) != {"schema", "table", "key_columns", "key_values"}
                    or key["table"] != self.name or not isinstance(key["key_columns"], list)
                    or not isinstance(key["key_values"], list) or not key["key_columns"]
                    or len(key["key_columns"]) != len(key["key_values"])):
                raise ValueError("source row key does not describe this input table")
            names, values = key["key_columns"], key["key_values"]
            if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
                raise ValueError("source key columns must be unique names")
            if all(name in self.columns for name in names):
                indices = tuple(self.columns.index(name) for name in names)
                cache_key = (indices, tuple(key_affinities.get(self.columns[i]) for i in indices))
                if cache_key not in self._row_key_lookups:
                    lookup = {}
                    for row, values_ in enumerate(self.rows):
                        lookup.setdefault(tuple(scalar(values_[i], self.columns[i]) for i in indices), []).append(row)
                    self._row_key_lookups[cache_key] = lookup
                matches = self._row_key_lookups[cache_key].get(tuple(scalar(value, name) for name, value in zip(names, values)), [])
                if len(matches) != 1:
                    raise ValueError("source key is absent or ambiguous in the immutable input")
                rows.append(matches[0])
            elif (len(names) == 1 and names[0] in ("rowid", "_rowid_", "oid")
                  and key["schema"] == "conversation" and rowid_source_sha256 == self.source_sha256
                  and type(values[0]) is int and 1 <= values[0] <= len(self.rows)):
                rows.append(values[0] - 1)
            else:
                raise ValueError("source row identity requires a verified input-order snapshot")
        return self.contributor_record(tuple(rows), columns)

    def record(self):
        return {"source_id": self.source_id, "source_sha256": self.source_sha256,
                "table": self.name, "columns": list(self.columns),
                "source_layout": deepcopy(self.source_layout),
                "row_count": len(self.rows),
                "section_cells": [{"row": row, "column": col, **deepcopy(value)}
                                  for (row, col), value in sorted(self._scopes.items())]}

    @classmethod
    def replay(cls, table, record, *, import_record=None):
        if set(record) != {"source_id", "source_sha256", "table", "columns", "row_count", "section_cells", "source_layout"}:
            raise ValueError("unknown input-lineage record fields")
        if record["source_layout"] is not None and import_record is None:
            raise ValueError("physical-cell replay requires the original importer metadata")
        out = cls(table, source_id=record["source_id"], import_record=import_record)
        scopes = {}
        for cell in record["section_cells"]:
            if set(cell) != {"row", "column", "anchor_row", "scope", "evidence"}:
                raise ValueError("unknown input-lineage scope fields")
            start, stop = cell["scope"]
            if cell["anchor_row"] != start or not start <= cell["row"] < stop:
                raise ValueError("invalid input-lineage scope anchor")
            scopes[(cell["column"], start, stop)] = cell["evidence"]
        for (column, start, stop), evidence in sorted(scopes.items()):
            out.section_labels(column, ((start, stop),), evidence=evidence)
        if out.record() != record:
            raise ValueError("source snapshot or declared lineage has changed")
        return out


def _record(kind: str, source: str, *, table=None, column=None, release_id=None,
            operation=None, inputs=(), measure=False) -> dict:
    """One column's origin. `measure` marks a quantity the engine computed (an aggregate or
    arithmetic), as opposed to a value carried from a row or a key derived from one (a year, a
    lowercased name): the reply formats a measure's numbers and writes a key as it is."""
    value = {"kind": kind, "source": source}
    for key, item in (("table", table), ("column", column), ("release_id", release_id),
                      ("operation", operation)):
        if item not in (None, ""):
            value[key] = item
    if inputs:
        value["inputs"] = list(dict.fromkeys(str(item) for item in inputs if item))
    if measure:
        value["measure"] = True
    return value


class ProvenanceContext:
    """Decorate one request's HTTP response and streamed trace with column lineage."""

    def __init__(self, tables, *, uploaded_count: int, reference_count: int = 0,
                 enrichment=None, dataset_semantics=()):
        self._by_column: dict[str, list[dict]] = {}
        self._by_location: dict[tuple[str, str], dict] = {}
        self._last: dict[str, dict] = {}
        self._input_tables = {}
        self._input_lineage = {}
        enrichment_sources = {}
        if enrichment is not None:
            for outcome in getattr(enrichment, "outcomes", ()):
                if getattr(outcome, "matched", False):
                    enrichment_sources[str(outcome.dataset_name)] = dict(outcome.provenance)
        asserted_columns = {
            (str(item.get("table", "")).casefold(),
             str(item.get("currency_column", "")).casefold()): item
            for item in (dataset_semantics or ()) if isinstance(item, dict)
            and item.get("table") and item.get("currency_column")
        }

        for index, table in enumerate(tables):
            name = str(table.get("name") or "")
            snapshot = {"name": name, "columns": tuple(table.get("columns") or ()),
                        "rows": tuple(tuple(row.get(c) for c in table["columns"]) if isinstance(row, dict)
                                      else tuple(row) for row in table.get("rows") or ()),
                        "import": deepcopy(table.get("import"))}
            self._input_tables[name] = None if name in self._input_tables else snapshot
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
                asserted = asserted_columns.get((name.casefold(), str(column).casefold()))
                if asserted is not None:
                    item = _record(
                        "asserted", "conversation", table=name, column=str(column),
                        operation="measure currency",
                        inputs=(f'{name}.{asserted.get("column")}',),
                    )
                else:
                    item = _record(meta["kind"], meta["source"], table=name, column=str(column),
                                   release_id=meta.get("release_id"))
                self._by_column.setdefault(str(column).casefold(), []).append(item)
                self._by_location[(name.casefold(), str(column).casefold())] = item

    def input_lineage(self, table):
        """Lazily capture the exact input/position snapshot used by this request.

        Hashing every cell is paid only when a contributor trace is requested.
        This does not infer contributor rows or enrichments from output values.
        """
        if table not in self._input_tables:
            raise ValueError("lineage table was not an input to this request")
        if table not in self._input_lineage:
            source = self._input_tables[table]
            if source is None:
                raise ValueError("ambiguous input table identity in cell lineage")
            self._input_lineage[table] = InputLineage(source, source_id="request/input/" + table,
                                                     import_record=source.get("import"))
        return self._input_lineage[table]

    def execution_lineage(self, trace):
        """Connect runtime row identities to this request's immutable source cells.

        Normalize names with the same table/column owners used by the executor.
        No output-value matching or guessed worksheet offset supplies an origin.
        """
        from engine.tables import normalize_table_name
        from engine.column_names import canonical_columns

        result = deepcopy(trace)
        if not isinstance(result, dict) or result.get("backend") != "python" or not isinstance(result.get("stages"), dict):
            return result
        names = {}
        for name in self._input_tables:
            canonical = normalize_table_name(name)
            names[canonical] = name if canonical not in names else None
        snapshots = result.get("input_order", {})
        resolved, unknown, verified, layouts = 0, 0, {}, {}
        result["source_tables"] = {}
        for key in result.get("source_keys", []):
            key.pop("input_lineage", None)
            key.pop("lineage_state", None)
            key.pop("lineage_reason", None)
            try:
                name = names.get(key["table"])
                if key["schema"] != "conversation" or name is None:
                    raise ValueError("source identity is not an unambiguous request input")
                if name not in layouts:
                    lineage = self.input_lineage(name)
                    canonical = canonical_columns(lineage.columns)
                    layouts[name] = lineage, canonical, dict(zip(canonical, lineage.columns, strict=True))
                lineage, canonical, columns = layouts[name]
                source = {**{k: key[k] for k in ("schema", "table", "key_columns", "key_values")}, "table": name,
                          "key_columns": [columns.get(c, c) for c in key["key_columns"]]}
                if name not in verified:
                    snapshot = snapshots.get(key["table"])
                    order_hash, affinities = None, {}
                    if snapshot is not None:
                        digest = hashlib.sha256(json.dumps(self._input_tables[name]["rows"], default=str,
                                                           ensure_ascii=False).encode("utf-8")).hexdigest()
                        if (snapshot.get("owner") != "engine.prepared.RequestDatabase.load" or
                                snapshot.get("row_count") != len(lineage.rows) or snapshot.get("columns") != canonical or
                                snapshot.get("rows_sha256") != digest):
                            verified[name] = "execution snapshot differs from the request's original row order"
                            raise ValueError(verified[name])
                        order_hash = lineage.source_sha256
                        affinities = {columns.get(c, c): kind for c, kind in snapshot["affinities"].items()}
                    verified[name] = order_hash, affinities
                if isinstance(verified[name], str):
                    raise ValueError(verified[name])
                order_hash, affinities = verified[name]
                # Store row references and one shared address map, not every
                # source cell repeated in every execution stage.
                evidence = lineage.resolve_row_keys([source], (), rowid_source_sha256=order_hash, key_affinities=affinities)
                key["input_lineage"] = evidence
                key["lineage_state"] = "resolved_source_row"
                if lineage.source_id not in result["source_tables"]:
                    result["source_tables"][lineage.source_id] = lineage.record()
                resolved += 1
            except (ValueError, KeyError, TypeError) as error:
                key["lineage_state"] = "unknown"
                key["lineage_reason"] = str(error)
                unknown += 1
        unknown_rows = sum(row.get("source_keys") is None for stage in result["stages"].values() for row in stage.get("rows", []))
        result["input_lineage_coverage"] = {"resolved_source_keys": resolved, "unknown_source_keys": unknown,
                                             "unknown_output_row_occurrences": unknown_rows,
                                             "scope": "source-row identities/address maps, not minimal per-cell influence or learned semantic proof"}
        return result

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

    def _location_record(self, table: str, column: str) -> dict | None:
        item = self._by_location.get((str(table).casefold(), str(column).casefold()))
        return deepcopy(item) if item is not None else self._catalog_record(column)

    @staticmethod
    def _expression_inputs(expression: dict) -> list[str]:
        kind = expression.get("kind") if isinstance(expression, dict) else None
        if kind == "column":
            return [f'{expression.get("table")}.{expression.get("column")}']
        if kind == "aggregate":
            return ProvenanceContext._expression_inputs(expression.get("operand") or {})
        if kind == "binary":
            return (ProvenanceContext._expression_inputs(expression.get("left") or {})
                    + ProvenanceContext._expression_inputs(expression.get("right") or {}))
        return []

    def _expression_record(self, expression: dict, output_column: str) -> dict:
        kind = expression.get("kind") if isinstance(expression, dict) else None
        if kind == "column":
            return (self._location_record(expression.get("table", ""), expression.get("column", ""))
                    or _record("derived", "Prereasoner", column=output_column,
                               operation="projection", inputs=self._expression_inputs(expression)))
        if kind == "aggregate":
            operation = str(expression.get("function") or "aggregate").upper()
        elif kind == "binary":
            operation = str(expression.get("operator") or "calculation")
        else:
            operation = kind or "expression"
        return _record("derived", "Prereasoner", column=output_column, operation=operation,
                       inputs=self._expression_inputs(expression),
                       measure=kind in ("aggregate", "binary"))

    def _computation_records(self, computation, columns) -> list[dict] | None:
        if not isinstance(computation, dict) or not computation.get("verified"):
            return None
        branches = computation.get("branches")
        if not isinstance(branches, list) or not branches:
            return None
        outputs = [branch.get("outputs") for branch in branches if isinstance(branch, dict)]
        if len(outputs) != len(branches) or any(not isinstance(row, list) or len(row) != len(columns)
                                               for row in outputs):
            return None
        records = []
        for index, column in enumerate(columns):
            alternatives = [self._expression_record(row[index].get("expression") or {}, column)
                            for row in outputs if isinstance(row[index], dict)]
            if len(alternatives) != len(outputs):
                return None
            if all(item == alternatives[0] for item in alternatives[1:]):
                records.append(alternatives[0])
            else:
                inputs = [value for item in alternatives for value in item.get("inputs", ())]
                records.append(_record("derived", "Prereasoner", column=column,
                                       operation="set operation", inputs=inputs,
                                       measure=all(item.get("measure") for item in alternatives)))
        return records

    @staticmethod
    def _reference(source: str, column: str, *, table=None, release_id=None) -> dict:
        return _record("reference", source, table=table, column=column, release_id=release_id)

    def _qualified_record(self, column: str, view: dict) -> dict | None:
        """The origin of a stage column the deterministic emitter names `<table>__<column>`.

        The bare-name catalog never matched these, so every column of a `combined` sheet was
        badged as calculated, an uploaded `orders__order ID` beside a city lookup as Wikidata,
        and a `commission_rates__commission_percent` as European Central Bank data
        (payment-commissions, 2026-10-01). The table half names the input it came from.
        """
        table, separator, name = column.partition("__")
        if not separator or not table or not name:
            return None
        located = self._by_location.get((table.casefold(), name.casefold()))
        if located is not None:
            return deepcopy(located)
        if table.casefold() == "exchange_rate":
            return self._reference("European Central Bank", name, table=table,
                                   release_id=view.get("source_release_id"))
        if table.casefold() in _WIKIDATA_TABLES:
            return self._reference("Wikidata", name, table=table,
                                   release_id=view.get("source_release_id"))
        return None

    def _classify_view(self, view: dict, *, resolve: bool = False) -> list[dict]:
        columns = [str(column) for column in (view.get("columns") or [])]
        existing = view.get("column_provenance")
        if self._valid_columns(existing, columns):
            return deepcopy(existing)
        if resolve:
            table = str(view.get("wtable") or "reference")
            source = (view.get("source") or
                      ("Wikidata" if table.casefold() in _WIKIDATA_TABLES else table))
            return [self._reference(source, column, table=table,
                                    release_id=view.get("source_release_id")) for column in columns]

        op = str(view.get("op") or "")
        previous = self._last
        records = []
        for column in columns:
            qualified = self._qualified_record(column, view)
            if qualified is not None:
                records.append(qualified)
                continue
            carried = previous.get(column.casefold())
            catalog = self._catalog_record(column)
            if op == "convert":
                low = column.casefold()
                if low == "converted":
                    item = _record("derived", "Prereasoner", column=column,
                                   operation="multiply", inputs=("amount", "exchange rate"),
                                   measure=True)
                elif carried is not None or catalog is not None:
                    item = carried or catalog
                elif low == "rate_published" or "rate" in low:
                    # The conversion trail's unqualified rate columns. A column the request's
                    # tables already name (a sheet's own commission rate) keeps its origin above.
                    item = self._reference("European Central Bank", column,
                                           table="exchange_rate", release_id=view.get("source_release_id"))
                else:
                    item = _record("derived", "Prereasoner", column=column,
                                   operation="calculation", inputs=tuple(previous), measure=True)
            elif op == "world_join" and carried is None:
                item = catalog or self._reference(
                    str(view.get("source") or "Wikidata"), column,
                    table=view.get("source_table"), release_id=view.get("source_release_id"),
                )
            elif op in _PASSTHROUGH_OPS:
                item = carried or catalog
            elif op in _DERIVED_OPS:
                item = carried or catalog
                if item is None:
                    item = _record("derived", "Prereasoner", column=column,
                                   operation=op, inputs=tuple(previous), measure=True)
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

    def decorate_result(self, result: dict | None, *, computation=None) -> dict | None:
        if not isinstance(result, dict):
            return result
        decorated = deepcopy(result)
        columns = [str(column) for column in (decorated.get("columns") or [])]
        existing = decorated.get("column_provenance")
        if not self._valid_columns(existing, columns):
            records = self._computation_records(computation, columns) or []
            if not records:
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
        decorated["result"] = self.decorate_result(
            decorated.get("result"), computation=decorated.get("computation")
        )
        computation = decorated.get("computation")
        if isinstance(computation, dict) and "source_lineage" in computation:
            computation["source_lineage"] = self.execution_lineage(computation["source_lineage"])
        return decorated
