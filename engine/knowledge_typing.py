"""Schema.org typing support for the world-query serving path.

This mixin owns model loading, table-signature caching, per-request typing capture, and
the learned class/family proposal. It deliberately does not authorize a world join; the
caller must still apply deterministic source-key grounding.
"""
from __future__ import annotations

import hashlib
import traceback

from engine.config import kb_model_route_enabled
from engine.entities import TYPE_TO_FRIENDLY
from engine.embeddings import normalize_surface


class KnowledgeTypingMixin:
    """Reusable Schema.org proposal/evidence boundary for ``KnowledgeQuery``."""

    GROUND_FRAC = 0.8
    FREETEXT_MIN_AVGLEN = 12

    def _router(self):
        """Return the generalized Schema.org router, reusing the serving encoder."""
        router = getattr(self, "_column_router", None)
        if router is None:
            from engine.router import Router

            router = self._column_router = Router(
                shared=(self.qwen, self.tok, self.model),
                interpreter=self._schema_interpreter(),
            )
        return router

    def _schema_interpreter(self):
        """Load the class interpreter once; source grounding survives a load failure."""
        interpreter = self.__dict__.get("_schema_interp")
        if interpreter is None:
            try:
                from engine.schema_model import SchemaInterpreter

                interpreter = SchemaInterpreter(shared=(self.qwen, self.tok))
            except Exception as error:  # noqa: BLE001 - deterministic grounding remains available
                print(
                    f"[knowledge_query] schema interpreter unavailable -> class proposals off: {error!r}",
                    flush=True,
                )
                interpreter = False
            self._schema_interp = interpreter
        return interpreter or None

    def _grounds(self, cells, world_type):
        """Return whether enough distinct cells have exact keys for ``world_type``."""
        norms = sorted({normalize_surface(str(cell)) for cell in cells if str(cell).strip()})
        if len(norms) < 2:
            return False
        cursor = self._rconn().cursor()
        cursor.execute(
            'SELECT COUNT(DISTINCT norm) FROM knowledgebase."words" WHERE type=%s AND norm = ANY(%s)',
            (world_type, norms),
        )
        hit_count = cursor.fetchone()[0]
        return hit_count >= max(2, self.GROUND_FRAC * len(norms))

    @staticmethod
    def _table_sig(table):
        """Return a stable cache key for one table schema and its values."""
        values_hash = hashlib.sha256(
            repr([tuple(row) for row in table["rows"]]).encode("utf-8", "replace")
        ).hexdigest()[:12]
        return table["name"], tuple(table["columns"]), values_hash

    def begin_typing(self):
        """Open a per-serve buffer for evidence captured by the model typing path."""
        self._typing_run = []

    def take_typing(self):
        """Close the buffer and return deduplicated typing records."""
        return self.__dict__.pop("_typing_run", None) or []

    def _emit_typing(self, records):
        """Append records to the active buffer, deduplicated by table and column."""
        buffer = self.__dict__.get("_typing_run")
        if buffer is None or not records:
            return
        seen = {(record["table"], record["column"]) for record in buffer}
        for record in records:
            if (record["table"], record["column"]) not in seen:
                buffer.append(record)
                seen.add((record["table"], record["column"]))

    def _schema_model_routes(self, table):
        """Return ``(routes, typing)`` from the learned proposal path.

        The returned routes are only proposals. ``KnowledgeQuery.route`` applies exact
        source membership afterward, so a model failure or abstention cannot grant a join.
        """
        routes, typing = {}, []
        if not kb_model_route_enabled():
            return routes, typing
        try:
            router = self._router()
            for column_index, column in enumerate(table["columns"]):
                cells = [
                    str(row[column_index])
                    for row in table["rows"]
                    if column_index < len(row) and row[column_index] not in (None, "")
                ]
                if len(cells) < 3 or self._avglen(table, column) > self.FREETEXT_MIN_AVGLEN:
                    continue
                proposal = router.route(cells, header=column)
                if not proposal:
                    continue
                grounded = None
                if proposal["geo"]:
                    for world_type in ("city", "country", "state"):
                        friendly = TYPE_TO_FRIENDLY.get(world_type)
                        if friendly in self.words and self._grounds(cells, world_type):
                            routes[(table["name"], column)] = friendly
                            grounded = friendly
                            break
                typing.append(
                    {
                        "table": table["name"],
                        "column": column,
                        "family": proposal["family"],
                        "frac": proposal["frac"],
                        "geo": proposal["geo"],
                        "grounded_to": grounded,
                        "class": proposal.get("class"),
                        "class_name": proposal.get("class_name"),
                        "class_threshold": proposal.get("class_threshold"),
                        "class_score_model": proposal.get("class_score_model"),
                        "class_bias": proposal.get("class_bias"),
                        "ontology_version": proposal.get("ontology_version"),
                        "model_artifact_sha256": proposal.get("model_artifact_sha256"),
                        "evidence": proposal.get("evidence", []),
                    }
                )
        except Exception as error:  # noqa: BLE001 - source-membership fallback is the safety boundary
            print(
                f"[knowledge_query] !! MODEL ROUTING FAILED -> value-membership fallback: {error!r}",
                flush=True,
            )
            traceback.print_exc()
            # Keep the interpreter fallback available when only the learned column
            # proposal failed. Exact source membership remains the join authority.
            routes, typing = {}, []

        interpreter = self._schema_interpreter()
        if interpreter is not None:
            try:
                report = interpreter.interpret_table(table)
                typing.append(
                    {
                        "table": table["name"],
                        "column": "*",
                        "kind": "schema_class",
                        "classes": report["classes"],
                        "properties": [prop for prop in report["properties"] if prop["fired"]],
                        "abstained": report["abstained"],
                        "ontology_version": report["ontology_version"],
                        "model_artifact_sha256": report["model_artifact_sha256"],
                        "input_sha256": report["input_sha256"],
                    }
                )
            except Exception as error:  # noqa: BLE001 - class evidence never authorizes a join
                print(f"[knowledge_query] schema class evidence failed (skipped): {error!r}", flush=True)
        return routes, typing
