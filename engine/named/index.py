"""The per-tab index: what the encoder read of a tab, once, as whole thousandths on named coordinates, under the tab's hash.

For each target (a table, a column, a stored value, a printed label, a band) the top named coordinates and their scores, and a small open vector kept apart. A table's class is a schema.org class, the superposition of its columns' property shares over the properties the class is a domain of (engine/named/names.py:class_signatures, engine/schema_decode.py's formula), each term kept; a class below CLASS_FLOOR is not stored. An index is a deterministic function of the tab and the model: its bytes are compared in the tests, and a cached one is read back by hash, never recomputed.
"""
from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import torch

from engine.named.backbone import TextBackbone
from engine.named.links import SCALE, to_thousandths
from engine.named.model import NamedReader, ReaderConfig
from engine.named.names import NameSet
from engine.named.units import T_COLUMN, T_TABLE, Graph, Target, collate, table_graphs, targets_of, value_graph
from engine.program.catalog import KIND_FAMILY, Catalog, CatalogTable

_MAGIC = b"NAMEDIDX1"
_VALUE_BATCH = 128
_FLOOR = 2                     # thousandths below which a coordinate is not stored
CLASS_FLOOR = 0.05             # a class this unlikely says nothing about a table


@dataclass(frozen=True)
class ClassSignatures:
    """Classes as weighted sums of named properties, each with a bias."""
    classes: Mapping[str, Mapping]

    @classmethod
    def from_ontology(cls) -> "ClassSignatures":
        """schema.org's classes, each the superposition of the properties it is a domain of."""
        from engine.named.names import class_signatures
        return cls(class_signatures())

    def scores(self, profile: Mapping[str, float]) -> dict[str, float]:
        """Each class's logistic superposition of a profile's property scores, rounded; a class under CLASS_FLOOR left out."""
        from engine.schema_decode import ClassDecoder      # only a bundle with signatures reads this: a training pod has neither it nor the config it brings
        scored = ((name, round(ClassDecoder.score_signature(profile, row["signature"], bias=float(row.get("bias", 0.0)), score_model="logistic_property_probability"), 3)) for name, row in sorted(self.classes.items()))
        return {name: score for name, score in scored if score >= CLASS_FLOOR}


@dataclass
class TabIndex:
    """One tab's targets and their coordinates. Target.table holds a local key until bound to a catalog: the table's place within its tab group."""
    tab_hash: str
    reader_id: str
    targets: list[Target]
    ids: np.ndarray       # [targets, keep] int32: head positions
    values: np.ndarray    # [targets, keep] int16: thousandths
    open: np.ndarray      # [targets, open_dim] int8
    profiles: dict[str, dict[str, float]]
    classes: dict[str, dict[str, float]]

    def to_bytes(self) -> bytes:
        header = {"tab_hash": self.tab_hash, "reader_id": self.reader_id, "targets": [[t.table, t.kind, t.position, t.text] for t in self.targets], "profiles": self.profiles, "classes": self.classes, "shape": [self.ids.shape[0], self.ids.shape[1], self.open.shape[1]]}
        head = json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        body = len(head).to_bytes(8, "little") + head + self.ids.astype("<i4").tobytes() + self.values.astype("<i2").tobytes() + self.open.astype("i1").tobytes()
        return _MAGIC + zlib.compress(body, 6)

    @classmethod
    def from_bytes(cls, data: bytes) -> "TabIndex":
        if not data.startswith(_MAGIC):
            raise ValueError("not a tab index")
        body = zlib.decompress(data[len(_MAGIC):])
        size = int.from_bytes(body[:8], "little")
        header = json.loads(body[8:8 + size].decode("utf-8"))
        rows, keep, dim = header["shape"]
        at = 8 + size
        ids = np.frombuffer(body, "<i4", rows * keep, at).reshape(rows, keep)
        at += ids.nbytes
        values = np.frombuffer(body, "<i2", rows * keep, at).reshape(rows, keep)
        at += values.nbytes
        vectors = np.frombuffer(body, "i1", rows * dim, at).reshape(rows, dim)
        return cls(header["tab_hash"], header["reader_id"], [Target(*row) for row in header["targets"]], ids, values, vectors, header["profiles"], header["classes"])


@dataclass
class TableView:
    """One catalog table's part of a workbook's index. What is the index's and not a question's is read once and kept: each target's coordinates, the targets by kind and by coordinate name, and their order by text."""
    targets: list[Target]
    ids: np.ndarray
    values: np.ndarray
    open: np.ndarray
    profile: dict[str, float]
    classes: dict[str, float]
    _coordinates: list[tuple[str, ...]] | None = field(default=None, repr=False, compare=False)
    _by_name: dict[str, list[int]] | None = field(default=None, repr=False, compare=False)
    _text_rank: np.ndarray | None = field(default=None, repr=False, compare=False)

    def coordinates(self) -> list[tuple[str, ...]]:
        """Each target's program coordinates (Target.coordinates), by target."""
        if self._coordinates is None:
            self._coordinates = [target.coordinates() for target in self.targets]
        return self._coordinates

    def targets_named(self, name: str) -> list[int]:
        """The targets whose coordinates hold this name, in target order."""
        if self._by_name is None:
            by_name: dict[str, list[int]] = {}
            for at, names in enumerate(self.coordinates()):
                for held in names:
                    by_name.setdefault(held, []).append(at)
            self._by_name = by_name
        return self._by_name.get(name, [])

    def text_rank(self, column_rank: Mapping[int, int]) -> np.ndarray:
        """Each target's place in (text, column rank) order, ties by target order: the tie-break after a question's score."""
        if self._text_rank is None:
            order = sorted(range(len(self.targets)), key=lambda at: (self.targets[at].text, column_rank.get(self.targets[at].position, -1)))
            rank = np.empty(len(self.targets), dtype=np.int64)
            rank[order] = np.arange(len(order))
            self._text_rank = rank
        return self._text_rank


class IndexCache:
    """Tab indexes by (tab hash, reader), in memory and, given a folder, on disk."""

    def __init__(self, folder: str | Path | None = None):
        self.folder = Path(folder) if folder else None
        self.held: dict[str, TabIndex] = {}
        self.built = 0
        self.reused = 0

    @staticmethod
    def key(tab_hash: str, reader_id: str) -> str:
        return hashlib.sha256(f"{tab_hash}|{reader_id}".encode()).hexdigest()

    def get(self, tab_hash: str, reader_id: str) -> TabIndex | None:
        key = self.key(tab_hash, reader_id)
        held = self.held.get(key)
        if held is None and self.folder is not None and (self.folder / f"{key}.idx").exists():
            held = self.held[key] = TabIndex.from_bytes((self.folder / f"{key}.idx").read_bytes())
        return held

    def put(self, index: TabIndex) -> None:
        key = self.key(index.tab_hash, index.reader_id)
        self.held[key] = index
        if self.folder is not None:
            self.folder.mkdir(parents=True, exist_ok=True)
            part = self.folder / f"{key}.part"
            part.write_bytes(index.to_bytes())
            part.replace(self.folder / f"{key}.idx")


def content_hash(tables: Sequence[CatalogTable]) -> str:
    """A compiled tab's identity when its payload's tab_hash (engine/sheet/payload.py) is not at hand: its tables' names, roles and cells."""
    digest = hashlib.sha256()
    for table in tables:
        digest.update(json.dumps([table.kind, table.tab, table.table.name, [[column.position, column.name, column.kind, column.role] for column in table.columns], [[line.row, line.role, list(line.section), line.label, sorted((position, str(cell.raw), cell.kind) for position, cell in line.cells.items())] for line in table.lines]], ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


def _top_coordinates(scores: np.ndarray, keep: int) -> tuple[np.ndarray, np.ndarray]:
    """Each row's top coordinates in thousandths, ties by head position, so what is kept never depends on sort internals."""
    thousandths = to_thousandths(scores).astype(np.int32)
    order = np.argsort(-thousandths, axis=1, kind="stable")[:, :keep]
    held = np.take_along_axis(thousandths, order, axis=1)
    held[held < _FLOOR] = 0
    ids = np.zeros((len(thousandths), keep), dtype=np.int32)
    values = np.zeros((len(thousandths), keep), dtype=np.int16)
    ids[:, :order.shape[1]] = order
    values[:, :order.shape[1]] = held
    return ids, values


class SheetEncoder:
    """A reader and its backbone, reading tabs into indexes. float32, no gradients."""

    def __init__(self, backbone: TextBackbone, reader: NamedReader, names: NameSet, config: ReaderConfig, signatures: ClassSignatures | None = None, reader_id: str = ""):
        self.backbone, self.reader, self.names, self.config = backbone, reader, names, config
        self.signatures = signatures or ClassSignatures({})
        self.reader_id = reader_id or names.sha

    def read_graphs(self, graphs: list[Graph], lookup: dict[str, torch.Tensor]) -> torch.Tensor:
        vectors = [torch.stack([lookup[text] for text in graph.texts]) if graph.texts else torch.zeros((1, self.backbone.dim)) for graph in graphs]
        inputs, edges, pad = collate(graphs, vectors)
        return self.reader.hidden(inputs, edges, pad)

    def index_group(self, tables: Sequence[CatalogTable], tab_hash: str) -> TabIndex:
        """Read one tab group's tables. Each table's targets name it by its place in the group."""
        units = self.config.units
        all_targets: list[Target] = []
        ids_parts, value_parts, open_parts = [], [], []
        profiles: dict[str, dict[str, float]] = {}
        classes: dict[str, dict[str, float]] = {}
        with torch.inference_mode():
            for place, table in enumerate(tables):
                local = f"{place}"
                targets = targets_of(table, units)
                graphs = table_graphs(table, units)
                order = table.column_order
                value_targets = [target for target in targets if target.kind not in (T_COLUMN, T_TABLE)]
                value_graphs = [value_graph(table, target, units, order) for target in value_targets]
                texts = [text for graph in (*graphs, *value_graphs) for text in graph.texts]
                distinct, vectors = self.backbone.encode_texts(texts, max_tokens=self.config.text_tokens)
                lookup = {text: vectors[at] for at, text in enumerate(distinct)}
                ids = np.zeros((len(targets), self.config.keep), dtype=np.int32)      # the top names per target as each is read: a table of many values never holds its whole score matrix
                values = np.zeros((len(targets), self.config.keep), dtype=np.int16)
                column_scores: dict[int, np.ndarray] = {}
                opens = np.zeros((len(targets), self.config.open_dim), dtype=np.float32)
                where = {target.key: at for at, target in enumerate(targets)}

                def hold(rows: list[int], probabilities: torch.Tensor) -> None:
                    ids[rows], values[rows] = _top_coordinates(probabilities.numpy(), self.config.keep)

                def base_of(graph: Graph, unit: int) -> torch.Tensor:
                    return lookup[graph.texts[unit]] if graph.texts else torch.zeros(self.backbone.dim)

                table_hidden, table_base = [], []
                for graph in graphs:
                    hidden = self.read_graphs([graph], lookup)[0]
                    table_hidden.append(hidden[graph.table])
                    table_base.append(base_of(graph, graph.table))
                    positions = sorted(graph.columns)
                    if positions:
                        units_at = torch.as_tensor([graph.columns[position] for position in positions])
                        probabilities = self.reader.coordinate_probabilities(hidden[units_at], torch.stack([base_of(graph, graph.columns[position]) for position in positions]))
                        vectors_open = self.reader.open_target(hidden[units_at])
                        rows = [where[(table.id, T_COLUMN, position, table.columns[position].name)] for position in positions]
                        hold(rows, probabilities)
                        opens[rows] = vectors_open.numpy()
                        column_scores.update(zip(rows, probabilities.numpy()))
                top_hidden = torch.stack(table_hidden).mean(0)
                at = where[(table.id, T_TABLE, -1, "")]
                hold([at], self.reader.coordinate_probabilities(top_hidden[None], torch.stack(table_base).mean(0)[None]))
                opens[at] = self.reader.open_target(top_hidden).numpy()
                for start in range(0, len(value_graphs), _VALUE_BATCH):
                    batch = value_graphs[start:start + _VALUE_BATCH]
                    hidden = self.read_graphs(batch, lookup)
                    # the value's unit is second in every value graph: after a name, or after a table's context
                    picked = hidden[:, 1]
                    probabilities = self.reader.coordinate_probabilities(picked, torch.stack([base_of(graph, 1) for graph in batch]))
                    vectors_open = self.reader.open_target(picked)
                    rows = [where[target.key] for target in value_targets[start:start + _VALUE_BATCH]]
                    hold(rows, probabilities)
                    opens[rows] = vectors_open.numpy()
                ids_parts.append(ids)
                value_parts.append(values)
                norm = np.maximum(np.linalg.norm(opens, axis=1, keepdims=True), 1e-9)
                open_parts.append(np.rint(opens / norm * 127.0).astype(np.int8))
                all_targets.extend(Target(local, target.kind, target.position, target.text) for target in targets)
                profile = _profile(column_scores, self.names, self.signatures)
                profiles[local] = profile
                classes[local] = self.signatures.scores(profile)
        return TabIndex(tab_hash, self.reader_id, all_targets, np.concatenate(ids_parts) if ids_parts else np.zeros((0, self.config.keep), np.int32), np.concatenate(value_parts) if value_parts else np.zeros((0, self.config.keep), np.int16), np.concatenate(open_parts) if open_parts else np.zeros((0, self.config.open_dim), np.int8), profiles, classes)


def _profile(column_scores: Mapping[int, np.ndarray], names: NameSet, signatures: ClassSignatures) -> dict[str, float]:
    """A table's property scores: each signature name's highest score over its columns, in thousandths rounded. Only what a signature reads: the rest is not a class's business and an index stays small."""
    wanted = sorted({item["property"] for row in signatures.classes.values() for item in row["signature"]})
    columns = np.stack(list(column_scores.values())) if column_scores else None
    out = {}
    for name in wanted:
        position = names.at(name)
        if position is None or columns is None:
            continue
        value = float(np.rint(columns[:, position].max() * SCALE)) / SCALE
        if value > 0:
            out[name] = value
    return out


def group_tables(catalog: Catalog) -> dict[str, list[CatalogTable]]:
    """A workbook's tables by what they hang from: a tab, or a family across tabs. Stable order within each."""
    groups: dict[str, list[CatalogTable]] = {}
    for table in catalog.tables:
        key = f"family:{table.table.name}" if table.kind == KIND_FAMILY else f"tab:{table.tab}"
        groups.setdefault(key, []).append(table)
    return groups


class WorkbookIndex:
    """A workbook's tab indexes bound to its catalog ids."""

    def __init__(self, views: dict[str, TableView], tabs: list[TabIndex], built: int, reused: int):
        self.views, self.tabs, self.built, self.reused = views, tabs, built, reused


def index_workbook(encoder: SheetEncoder, catalog: Catalog, cache: IndexCache | None = None, tab_hashes: Mapping[str, str] | None = None) -> WorkbookIndex:
    """Each tab group read once per its hash; targets bound to the catalog's table ids. tab_hashes: engine/sheet/payload.py's identity per tab, when the caller read a payload."""
    cache = cache if cache is not None else IndexCache()
    views: dict[str, TableView] = {}
    tabs = []
    built = reused = 0
    for key, tables in group_tables(catalog).items():
        tab = key.split(":", 1)[1]
        tab_hash = (tab_hashes or {}).get(tab) if key.startswith("tab:") else None
        tab_hash = tab_hash or content_hash(tables)
        held = cache.get(tab_hash, encoder.reader_id)
        if held is None:
            held = encoder.index_group(tables, tab_hash)
            cache.put(held)
            built += 1
        else:
            reused += 1
        tabs.append(held)
        at = 0
        for place, table in enumerate(tables):
            local = str(place)
            start = at
            while at < len(held.targets) and held.targets[at].table == local:
                at += 1
            views[table.id] = TableView([Target(table.id, t.kind, t.position, t.text) for t in held.targets[start:at]], held.ids[start:at], held.values[start:at], held.open[start:at], held.profiles.get(local, {}), held.classes.get(local, {}))
    return WorkbookIndex(views, tabs, built, reused)


__all__ = ["ClassSignatures", "IndexCache", "SheetEncoder", "TabIndex", "TableView", "WorkbookIndex", "content_hash", "group_tables", "index_workbook"]
