"""The per-tab index: what the encoder read of a tab, once, as whole thousandths on named coordinates, under the tab's hash.

For each target (a table, a column, a stored value, a printed label, a band) the top named coordinates and their scores, and a small open vector kept apart. A table's class is the additive superposition of its columns' property scores (engine/schema_decode.py's formula), each term kept. An index is a deterministic function of the tab and the model: its bytes are compared in the tests, and a cached one is read back by hash, never recomputed.
"""
from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import torch

from engine.named.backbone import TextBackbone
from engine.named.links import SCALE, to_thousandths
from engine.named.model import NamedReader, ReaderConfig
from engine.named.names import NameSet
from engine.named.units import T_COLUMN, T_TABLE, Graph, Target, collate, column_order, table_graphs, targets_of, value_graph
from engine.program.catalog import KIND_FAMILY, Catalog, CatalogTable

_MAGIC = b"NAMEDIDX1"
_VALUE_BATCH = 128
_FLOOR = 2                     # thousandths below which a coordinate is not stored


@dataclass(frozen=True)
class ClassSignatures:
    """Classes as weighted sums of named properties, each with a bias."""
    classes: Mapping[str, Mapping]

    @classmethod
    def from_groups(cls, groups: Mapping[str, Sequence[str]], weight: float = 1.0, bias: float = -1.0) -> "ClassSignatures":
        """The vocabulary's groups as classes: a table of gas names reads as gas, in a sum one can read."""
        return cls({f"class:{group}": {"bias": bias, "signature": [{"property": name, "weight": weight} for name in members]} for group, members in groups.items()})

    def scores(self, profile: Mapping[str, float]) -> dict[str, float]:
        """Each class's logistic superposition of a profile's property scores, rounded, zero terms skipped."""
        from engine.schema_decode import ClassDecoder      # only a bundle with signatures reads this: a training pod has neither it nor the config it brings
        return {name: round(ClassDecoder.score_signature(profile, row["signature"], bias=float(row.get("bias", 0.0)), score_model="logistic_property_probability"), 3) for name, row in sorted(self.classes.items())}


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
    """One catalog table's part of a workbook's index."""
    targets: list[Target]
    ids: np.ndarray
    values: np.ndarray
    open: np.ndarray
    profile: dict[str, float]
    classes: dict[str, float]


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
    thousandths = to_thousandths(scores)
    ids = np.zeros((len(thousandths), keep), dtype=np.int32)
    values = np.zeros((len(thousandths), keep), dtype=np.int16)
    for row, line in enumerate(thousandths):
        order = np.lexsort((np.arange(len(line)), -line.astype(np.int32)))[:keep]
        held = line[order]
        held[held < _FLOOR] = 0
        ids[row, :len(order)] = order
        values[row, :len(order)] = held
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
                order = column_order(table)
                value_targets = [target for target in targets if target.kind not in (T_COLUMN, T_TABLE)]
                value_graphs = [value_graph(table, target, units, order) for target in value_targets]
                texts = [text for graph in (*graphs, *value_graphs) for text in graph.texts]
                distinct, vectors = self.backbone.encode_texts(texts, max_tokens=self.config.text_tokens)
                lookup = {text: vectors[at] for at, text in enumerate(distinct)}
                coords = np.zeros((len(targets), len(self.names)), dtype=np.float32)
                opens = np.zeros((len(targets), self.config.open_dim), dtype=np.float32)
                where = {target.key: at for at, target in enumerate(targets)}
                table_hidden = []
                for graph in graphs:
                    hidden = self.read_graphs([graph], lookup)[0]
                    table_hidden.append(hidden[graph.table])
                    positions = sorted(graph.columns)
                    if positions:
                        units_at = torch.as_tensor([graph.columns[position] for position in positions])
                        logits = self.reader.coordinate_logits(hidden[units_at])
                        vectors_open = self.reader.open_target(hidden[units_at])
                        for row, position in enumerate(positions):
                            at = where[(table.id, T_COLUMN, position, table.columns[position].name)]
                            coords[at] = torch.sigmoid(logits[row]).numpy()
                            opens[at] = vectors_open[row].numpy()
                top_hidden = torch.stack(table_hidden).mean(0)
                at = where[(table.id, T_TABLE, -1, "")]
                coords[at] = torch.sigmoid(self.reader.coordinate_logits(top_hidden)).numpy()
                opens[at] = self.reader.open_target(top_hidden).numpy()
                for start in range(0, len(value_graphs), _VALUE_BATCH):
                    batch = value_graphs[start:start + _VALUE_BATCH]
                    hidden = self.read_graphs(batch, lookup)
                    # the value's unit is second in every value graph: after a name, or after a table's context
                    picked = hidden[:, 1]
                    logits = self.reader.coordinate_logits(picked)
                    vectors_open = self.reader.open_target(picked)
                    for row, target in enumerate(value_targets[start:start + _VALUE_BATCH]):
                        at = where[target.key]
                        coords[at] = torch.sigmoid(logits[row]).numpy()
                        opens[at] = vectors_open[row].numpy()
                ids, values = _top_coordinates(coords, self.config.keep)
                ids_parts.append(ids)
                value_parts.append(values)
                norm = np.maximum(np.linalg.norm(opens, axis=1, keepdims=True), 1e-9)
                open_parts.append(np.rint(opens / norm * 127.0).astype(np.int8))
                all_targets.extend(Target(local, target.kind, target.position, target.text) for target in targets)
                profile = _profile(coords, targets, self.names, self.signatures)
                profiles[local] = profile
                classes[local] = self.signatures.scores(profile)
        return TabIndex(tab_hash, self.reader_id, all_targets, np.concatenate(ids_parts) if ids_parts else np.zeros((0, self.config.keep), np.int32), np.concatenate(value_parts) if value_parts else np.zeros((0, self.config.keep), np.int16), np.concatenate(open_parts) if open_parts else np.zeros((0, self.config.open_dim), np.int8), profiles, classes)


def _profile(coords: np.ndarray, targets: list[Target], names: NameSet, signatures: ClassSignatures) -> dict[str, float]:
    """A table's property scores: each signature name's highest score over its columns, in thousandths rounded. Only what a signature reads: the rest is not a class's business and an index stays small."""
    wanted = sorted({item["property"] for row in signatures.classes.values() for item in row["signature"]})
    columns = [at for at, target in enumerate(targets) if target.kind == T_COLUMN]
    out = {}
    for name in wanted:
        position = names.at(name)
        if position is None or not columns:
            continue
        value = float(np.rint(coords[columns, position].max() * SCALE)) / SCALE
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
