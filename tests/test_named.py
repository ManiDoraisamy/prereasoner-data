"""The named reader's contract, on the production owners: every decision reads named coordinates, each moves a link by exactly its weight, order cannot matter, the same input gives the same bytes on 1 and on 4 threads, unnamed similarity may propose and never decide, no LLM is on the path, families stay held out. Hermetic: a tiny hashing backbone (training/named/fake.py), the real reader, index, links, decoder, verifier and chooser. NAMED_REAL_BACKBONE=1 adds bge-small from local files. Runs with: python -m tests.test_named."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import torch

from engine.fk_edges import edges, table_edges
from engine.named.bundle import read_bundle, write_bundle
from engine.named.chooser import ANSWER, ASK, FEATURES, PRIOR, Candidate, Chooser, FeatureTerm, answer_or_ask, features_of, score_candidates
from engine.named.index import ClassSignatures, IndexCache, TabIndex, content_hash, group_tables, index_workbook
from engine.named.links import EXACT, RULES, SCALE, RuleHit, contributions_of, link_total, named_matrix, rule_evidence, uniform_weights
from engine.named.model import NamedReader, ReaderConfig
from engine.named.names import CLOSED_HEADS, NameSet, closed_labels, fine_of, load_name_set, meaning_names, property_texts
from engine.named.pipeline import NamedEngine, load_bundle
from engine.named.reader import MODE_NAMED, MODE_NAMED_PLUS_OPEN, PROPOSAL_SCORE, build_reading, closed_reading, needs_asking, read_questions, unsupported_links
from engine.named.units import T_COLUMN, T_VALUE, UnitsConfig, cell_text, sampled_lines, table_graphs, targets_of
from engine.program.catalog import Catalog, CatalogTable
from engine.program.execute import run
from engine.program.language import Agg, Col, Show
from engine.program.names import ANSWER_TYPES, FORMS, SLOT_KEY, SLOT_MEASURE, SLOT_VALUE, SUM, coordinate
from engine.program.scoring import Reading, score_program
from engine.program.store import RequestTables
from engine.program.verifier import Evidence, Verified
from training.named.augment import SURVIVAL_OPS, add_columns, augmented, mutate_catalog, reorder_columns
from training.named.choose import MARGINS, CandidateRecord, calibrate, choose_policy, choose_thresholds, cross_fit, decision_of, fit_chooser, fit_conditional, fit_logistic, fit_slot_weights, fold_of, groups_of
from training.named.data import TabExample, check_held_out, read_examples, split_families, write_shard
from training.named.evaluate import GATE_A, Item, gate_a, graded, record_of, run_mode, run_survival
from training.named.fake import MEANINGS, HashBackbone, fake_catalog, fake_examples, fake_name_set, fake_rows
from training.named.lease import bundle_mb, dataclass_fields, ensure_same_tables, ensure_size_aware, plan_candidate, pod_code, remote_command
from training.named.train import TrainConfig, build_batch, read_descriptions, train

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG = ReaderConfig(backbone="hash", hidden=48, blocks=2, open_dim=16, keep=8, sample_rows=4, window_columns=12, row_mates=2)


def _engine(seed: int = 0, config: ReaderConfig = CONFIG, chooser: Chooser = PRIOR, signatures=None, cache=None) -> NamedEngine:
    torch.manual_seed(seed)
    names = fake_name_set()
    backbone = HashBackbone()
    reader = NamedReader(backbone.dim, names.meaning_start, names.meaning, config).float()
    read_descriptions(backbone, reader, names)
    return NamedEngine(backbone, reader, names, config, uniform_weights(len(names)), chooser=chooser, signatures=signatures, cache=cache)


def _silent_read(engine: NamedEngine, question: str):
    """A question's reading with every named coordinate and open vector at zero: a test then sets the one it means."""
    read = read_questions(engine.backbone, engine.reader, engine.config, [question])[0]
    read.coordinates = np.zeros_like(read.coordinates)
    read.open = np.zeros_like(read.open)
    return read


def _build(engine, read, prepared, mode=MODE_NAMED, weights=None):
    return build_reading(read, prepared.index, prepared.catalog, prepared.decoder, engine.names, weights or engine.weights, mode=mode, signatures=engine.signatures)


def _target_at(view, kind: str, position: int) -> int:
    return next(at for at, target in enumerate(view.targets) if target.kind == kind and target.position == position)


# --------------------------------------------------------------------------------------------------- the formula
def test_link_is_exactly_its_terms() -> None:
    """link = sum of weight x question x target + rule evidence, each returned, and the matrix computes the same number."""
    names = ("a", "b", "c", "d")
    weights = np.asarray([0.5, 2.0, 1.0, 0.25])
    question = np.asarray([1000, 500, 0, 250], dtype=np.int16)
    ids = np.asarray([0, 1, 2, 3])
    values = np.asarray([1000, 250, 1000, 0], dtype=np.int16)
    terms = contributions_of(question, ids, values, weights, names)
    assert [term.coordinate for term in terms] == ["a", "b"]
    assert terms[0].value == 0.5 and terms[1].value == 2.0 * 0.5 * 0.25
    rules = (RuleHit(EXACT, 0.75, 1.0),)
    assert link_total(terms, rules) == 0.5 + 0.25 + 0.75
    matrix = named_matrix(question[None], ids[None], values[None], weights)
    assert abs(matrix[0, 0] - sum(term.value for term in terms)) < 1e-12
    records = [term.record() for term in terms]
    assert records[0]["coordinate"] == "a" and records[0]["contribution"] == 0.5


def test_intervention_moves_a_link_by_exactly_its_weight() -> None:
    """Where the question reads a coordinate at 1, moving the target's coordinate from 0 to 1 moves the program coordinate by exactly that coordinate's weight, through the whole reading, and a program's score by that times its slot's weight. Nothing else moves."""
    engine = _engine()
    catalog = fake_catalog(1)
    prepared = engine.prepare(catalog)
    read = _silent_read(engine, "total gas this year")
    c = engine.names.at(MEANINGS[2])
    word = read.words.index("gas")
    read.coordinates[word, c] = SCALE
    weight = 0.75
    coordinates = np.ones(len(engine.names))
    coordinates[c] = weight
    weights = uniform_weights(len(engine.names))
    object.__setattr__(weights, "coordinates", coordinates)
    view = prepared.index.views["t0"]
    at = _target_at(view, T_COLUMN, 2)
    view.ids[at] = c
    view.values[at] = 0
    view.ids[at, 1:] = np.arange(1, view.ids.shape[1])
    before = _build(engine, read, prepared, weights=weights)
    view.values[at, 0] = SCALE
    after = _build(engine, read, prepared, weights=weights)
    name = coordinate(SLOT_MEASURE, Col("t0", 2).name)
    # the word's role joins a link once the link exists: taken out by its own named term, so the coordinate's own move is what is held
    moved = after.reading.score(name) - before.reading.score(name) - after.book[name].role
    assert abs(moved - weight) < 1e-4, moved
    terms = after.book[name].contributions
    assert [term.coordinate for term in terms] == [MEANINGS[2]] and terms[0].value == weight
    others = {key: value for key, value in after.reading.scores.items() if key.split(":", 1)[1] != "t0.2" and not key.startswith("calc_left")}
    assert others == {key: value for key, value in before.reading.scores.items() if key.split(":", 1)[1] != "t0.2" and not key.startswith("calc_left")}
    program = Agg(SUM, Col("t0", 2), "t0")
    delta = score_program(program, after.reading, catalog).score - score_program(program, before.reading, catalog).score
    assert abs(delta - (after.reading.score(name) - before.reading.score(name))) < 1e-9     # the program's score carries the coordinate's move, nothing more


def test_class_is_a_superposition_of_property_scores() -> None:
    """A table's class moves by exactly a property's weight in logit: the additive computation of engine/schema_decode.py, named terms only."""
    signatures = ClassSignatures({"class:gas": {"bias": -1.0, "signature": [{"property": MEANINGS[2], "weight": 2.0}, {"property": MEANINGS[3], "weight": 0.5}]}})
    logit = lambda p: np.log(p / (1 - p))
    low = signatures.scores({MEANINGS[2]: 0.0, MEANINGS[3]: 0.0})["class:gas"]
    high = signatures.scores({MEANINGS[2]: 0.5, MEANINGS[3]: 0.0})["class:gas"]
    assert abs((logit(high) - logit(low)) - 1.0) < 0.01
    engine = _engine(signatures=signatures)
    prepared = engine.prepare(fake_catalog(0))
    view = prepared.index.views["t0"]
    assert set(view.classes) == {"class:gas"} and set(view.profile) <= {MEANINGS[2], MEANINGS[3]}
    assert view.classes["class:gas"] == signatures.scores(view.profile)["class:gas"]


def test_rules_are_named_and_come_from_the_word_reading() -> None:
    assert rule_evidence("value:t0.1=ena", 1.0, set()) == ((EXACT, 1.0),)
    assert rule_evidence("value:t0.1=ena gas", 0.4, set())[0][0] == "partial"
    assert rule_evidence("contains:t0.1=tran", 0.4, set())[0][0] == "prefix"
    assert rule_evidence("period:2001", 0.9, set())[0][0] == "date"
    assert rule_evidence("number:500", 1.0, set())[0][0] == "stated"
    assert rule_evidence(coordinate(SLOT_MEASURE, "t0.1"), 0.0, set()) == ()
    engine = _engine()
    prepared = engine.prepare(fake_catalog(0))
    table = prepared.catalog.tables[0]
    value = next(text for text in table.folds[0] if text)
    read = _silent_read(engine, f"volume for {value}")
    named = _build(engine, read, prepared)
    evidence = named.book[coordinate(SLOT_VALUE, Col("t0", 0).name, value)]
    assert {hit.rule for hit in evidence.rules} == {EXACT} and evidence.supported and evidence.span
    assert all(rule in RULES for hit in evidence.rules for rule in [hit.rule])


# ----------------------------------------------------------------------------------------------------- order
def test_order_invariance_by_construction() -> None:
    """Columns shuffled, lines reversed: the encoder gets the same units in the same order, bit for bit, and each target its same coordinates wherever it stands."""
    engine = _engine()
    catalog = fake_catalog(2, lines=20)
    table = catalog.tables[0]
    moved_table, mapping = reorder_columns(table.table, 3)
    moved_table.lines.reverse()
    moved = CatalogTable("t0", moved_table, table.kind)
    units = CONFIG.units
    assert [line for line in [table.lines[i].row for i in sampled_lines(table, 4)]] == [moved.lines[i].row for i in sampled_lines(moved, 4)]
    assert table_graphs(table, units)[0].texts == table_graphs(moved, units)[0].texts
    assert [table.columns[p].name for p in table.column_order] == [moved.columns[p].name for p in moved.column_order]
    assert np.array_equal(table_graphs(table, units)[0].edges, table_graphs(moved, units)[0].edges)
    first = engine.encoder.index_group([table], "a")
    second = engine.encoder.index_group([moved], "b")

    def keyed(index: TabIndex, column_of):
        return {(target.kind, column_of(target.position) if target.kind in (T_COLUMN, T_VALUE) else -1, target.text): (index.ids[at].tolist(), index.values[at].tolist(), index.open[at].tolist()) for at, target in enumerate(index.targets)}

    assert keyed(first, lambda position: mapping[position]) == keyed(second, lambda position: position)
    read_a = engine.ask("total volume", engine.prepare(catalog))
    read_b = engine.ask("total volume", engine.prepare(Catalog([moved], catalog.name)))
    # the same lines and cells; a shown line lists its cells in its table's column order, which the shuffle changed
    cells = lambda rows: sorted(sorted(map(str, row)) for row in rows or ())
    assert read_a.kind == read_b.kind and cells(read_a.rows) == cells(read_b.rows)


def test_table_edges_are_the_edges_of_the_owner() -> None:
    """The vectorized table graph equals engine/fk_edges.py's pairwise edges() on every kind of pair."""
    kinds, cols, rows = ["table", "name", "name", "value", "value", "value", "value", "value"], [-1, 0, 1, 0, 1, 0, 1, 0], [-1, -1, -1, 0, 0, 1, 1, 0]
    units = [{"group": "schema", "kind": kind, "table": 0, "col": col, "row": row} for kind, col, row in zip(kinds, cols, rows)]
    assert np.array_equal(edges(units), table_edges(kinds, cols, rows))


def test_cell_units_read_kinds_not_figures() -> None:
    from engine.sheet.grid import read_cell
    assert cell_text(read_cell(1, 1, 1234.5, None, False)) == "number 4 digits fraction"
    assert cell_text(read_cell(1, 1, -7.0, None, False)) == "number 1 digits negative"
    assert cell_text(read_cell(1, 1, "Aquila Inc", None, False)) == "Aquila Inc"


# --------------------------------------------------------------------------------------------- determinism
_DIGEST = """
import hashlib, json, os, sys
import numpy as np, torch
torch.set_num_threads(int(os.environ["OMP_NUM_THREADS"]))
sys.path.insert(0, os.getcwd())
from tests.test_named import _engine, CONFIG
from training.named.fake import fake_catalog
engine = _engine()
prepared = engine.prepare(fake_catalog(3, lines=40))
digest = hashlib.sha256()
for tab in prepared.index.tabs:
    digest.update(tab.to_bytes())
outcome = engine.ask("average price in the east region", prepared)
digest.update(json.dumps(sorted(outcome.named.reading.scores.items())).encode())
digest.update(repr(outcome.decision.candidate.verified.scored.program if outcome.decision.candidate else None).encode())
print(digest.hexdigest())
"""


def test_determinism_across_threads_and_hash_seeds() -> None:
    """The index's bytes, the reading and the choice: one digest on 1 and 4 threads, under two hash seeds."""
    seen = set()
    for threads, seed in ((1, "0"), (4, "12345")):
        env = dict(os.environ, OMP_NUM_THREADS=str(threads), MKL_NUM_THREADS=str(threads), PYTHONHASHSEED=seed)
        output = subprocess.run([sys.executable, "-c", _DIGEST], capture_output=True, text=True, env=env, cwd=ROOT, timeout=300)
        assert output.returncode == 0, output.stderr[-800:]
        seen.add(output.stdout.strip().splitlines()[-1])
    assert len(seen) == 1, seen


def test_real_backbone_determinism_when_asked() -> None:
    """bge-small from local files, 1 and 4 threads: rounded coordinates equal. Only with NAMED_REAL_BACKBONE=1: it is a model load."""
    if os.environ.get("NAMED_REAL_BACKBONE") != "1":
        return
    from engine.named.backbone import load_backbone
    backbone = load_backbone("bge-small", layers=3)
    names = fake_name_set()
    config = ReaderConfig(backbone="bge-small", backbone_layers=3, hidden=96, blocks=2, sample_rows=4)
    torch.manual_seed(0)
    reader = NamedReader(backbone.dim, names.meaning_start, names.meaning, config).float()
    read_descriptions(backbone, reader, names)
    engine = NamedEngine(backbone, reader, names, config, uniform_weights(len(names)))
    catalog = fake_catalog(2, lines=30)
    digests = []
    for threads in (1, 4):
        torch.set_num_threads(threads)
        tab = engine.encoder.index_group(catalog.tables[:1], "a")
        read = read_questions(backbone, reader, config, ["total volume for duke"])[0]
        digests.append(hashlib.sha256(tab.to_bytes() + read.coordinates.tobytes() + json.dumps(read.closed, sort_keys=True).encode()).hexdigest())
    assert digests[0] == digests[1]


def test_scores_are_rounded_before_compare() -> None:
    engine = _engine()
    prepared = engine.prepare(fake_catalog(0))
    named = engine.read("total volume", prepared)
    assert all(round(value, 4) == value for value in named.reading.scores.values())
    assert named.read.coordinates.dtype == np.int16
    assert prepared.index.views["t0"].values.dtype == np.int16
    assert next(engine.reader.parameters()).dtype == torch.float32


# ------------------------------------------------------------------------------------------ the index, the cache
def test_index_round_trips_and_is_compact() -> None:
    engine = _engine()
    prepared = engine.prepare(fake_catalog(1))
    tab = prepared.index.tabs[0]
    data = tab.to_bytes()
    back = TabIndex.from_bytes(data)
    assert back.to_bytes() == data and back.targets == tab.targets and back.tab_hash == tab.tab_hash
    assert len(data) < 200 * len(tab.targets) + 2000


def test_a_tab_is_read_once_per_hash() -> None:
    with tempfile.TemporaryDirectory() as folder:
        cache = IndexCache(folder)
        engine = _engine(cache=cache)
        catalog = fake_catalog(1)
        first = index_workbook(engine.encoder, catalog, cache)
        assert first.built == 1 and first.reused == 0
        second = index_workbook(engine.encoder, catalog, cache)
        assert second.built == 0 and second.reused == 1
        fresh = IndexCache(folder)
        third = index_workbook(engine.encoder, catalog, fresh)
        assert third.built == 0 and third.reused == 1
        changed = index_workbook(engine.encoder, fake_catalog(2), cache)
        assert changed.built == 1
        by_payload = index_workbook(engine.encoder, catalog, cache, {group.split(":", 1)[1]: "from-the-payload" for group in group_tables(catalog)})
        assert by_payload.built == 1 and by_payload.tabs[0].tab_hash == "from-the-payload"
        assert content_hash(catalog.tables) != content_hash(fake_catalog(2).tables)


# ----------------------------------------------------------------------------------------- named or asked
def _proposal_case():
    engine = _engine()
    catalog = fake_catalog(1)
    prepared = engine.prepare(catalog)
    read = _silent_read(engine, "zzqx")
    view = prepared.index.views["t0"]
    at = _target_at(view, T_COLUMN, 2)
    view.open[:] = 0
    view.open[at] = 0
    view.open[at, 0] = 127
    read.open[0, 0] = 127
    return engine, prepared, read, view


def test_open_channel_proposes_and_never_decides() -> None:
    """A word whose only reach is similarity puts its target before the decoder at one fixed score, however similar. That link has no named support."""
    engine, prepared, read, view = _proposal_case()
    named = _build(engine, read, prepared)
    name = coordinate(SLOT_MEASURE, Col("t0", 2).name)
    evidence = named.book[name]
    assert evidence.proposal and not evidence.supported and named.reading.score(name) == PROPOSAL_SCORE
    read.open[0, 0] = 60                                # still above the proposal line, half as similar
    read.open[0, 1] = 100
    second = _build(engine, read, prepared)
    assert second.reading.score(name) == PROPOSAL_SCORE
    assert {key: value for key, value in named.reading.scores.items() if "=" not in key} == {key: value for key, value in second.reading.scores.items() if "=" not in key}


def test_a_program_on_a_proposal_is_needs_asking_and_is_asked() -> None:
    engine, prepared, read, view = _proposal_case()
    named = _build(engine, read, prepared)
    program = Show("t0", (Col("t0", 2),), ())
    assert needs_asking(program, named, prepared.catalog)
    assert unsupported_links(program, named, prepared.catalog)
    scored = score_program(program, named.reading, prepared.catalog)
    verified = Verified(scored, run(program, prepared.tables), (Evidence("ran", True),))
    features = features_of(verified, named, prepared.catalog, rank=0, best_score=scored.score)
    assert features["needs_asking"] == 1.0
    eager = Chooser({"needs_asking": 100.0, "shape_agrees": 100.0}, 100.0)
    decision = answer_or_ask(score_candidates([verified], named, prepared.catalog, eager), eager)
    assert decision.kind == ASK and decision.reasons == ("no_named_support",)


def test_only_the_other_mode_lets_similarity_decide() -> None:
    engine, prepared, read, view = _proposal_case()
    plus = _build(engine, read, prepared, mode=MODE_NAMED_PLUS_OPEN)
    name = coordinate(SLOT_MEASURE, Col("t0", 2).name)
    assert plus.reading.score(name) > PROPOSAL_SCORE
    assert not plus.book[name].proposal
    assert not needs_asking(Show("t0", (Col("t0", 2),), ()), plus, prepared.catalog)
    try:
        _build(engine, read, prepared, mode="anything_else")
    except ValueError:
        return
    raise AssertionError("an unknown mode ran")


def test_named_support_is_named_terms_or_rules() -> None:
    engine = _engine()
    prepared = engine.prepare(fake_catalog(1))
    table = prepared.catalog.tables[0]
    read = _silent_read(engine, f"{table.columns[3].name} please")
    named = _build(engine, read, prepared)
    assert named.book[coordinate(SLOT_MEASURE, Col("t0", 3).name)].supported
    assert not named.book[coordinate(SLOT_KEY, Col("t0", 0).name)].supported


def test_no_llm_on_the_path() -> None:
    """Importing the whole path loads no LLM client and no generative model class."""
    script = "import sys; import engine.named.pipeline, engine.named.chooser, engine.named.reader, engine.named.index, engine.program.verifier; print(json.dumps([name for name in sys.modules if name == 'engine.llm' or name.startswith(('google.genai', 'google.generativeai', 'vertexai', 'anthropic', 'openai', 'orchestrator'))]))"
    output = subprocess.run([sys.executable, "-c", "import json\n" + script], capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert output.returncode == 0, output.stderr[-800:]
    assert json.loads(output.stdout.strip().splitlines()[-1]) == []
    for path in (ROOT / "engine" / "named").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in ("engine.llm", "generate(", "AutoModelForCausalLM"):
            assert word not in text, f"{path.name}: {word}"


# ---------------------------------------------------------------------------------------------- the closed heads
def test_closed_heads_read_gold_coordinates() -> None:
    labels = closed_labels(["form:rank", "answer_type:name", "table:t0", "entity:group", "direction:highest", "aggregate:sum", "measure:t0.2", "key:t0.0", "conditions:t0=1", "filter:t0.1", "value:t0.1=east", "comparison:>=@t0.1"])
    assert labels["form"] == "rank" and labels["entity"] == "group" and labels["direction"] == "highest" and labels["aggregate"] == "sum" and labels["conditions"] == "1" and labels["comparison"] == ">="
    assert labels["size"] == "signed" and labels["row_op"] == "none"
    assert set(labels) == set(CLOSED_HEADS)
    assert CLOSED_HEADS["form"] == FORMS


def test_a_head_reads_as_log_odds_against_silence() -> None:
    engine = _engine()
    read = read_questions(engine.backbone, engine.reader, engine.config, ["how many deals"])[0]
    read.closed["aggregate"] = {name: 0.0 for name in read.closed["aggregate"]}
    read.closed["aggregate"].update({"none": 0.5, "count": 0.5, "sum": 0.0})
    read.closed["form"] = {"show": 0.25, "agg": 0.25, "rank": 0.25, "calc": 0.25}
    scores = closed_reading(read, engine.weights)
    assert abs(scores["aggregate:count"]) < 1e-9
    assert scores["aggregate:sum"] < -5 and abs(scores["form:agg"] - np.log(0.25)) < 1e-9


def test_name_set_loader_and_its_forms() -> None:
    """The structure tier from the name set, then the owner's selected property basis in its order (engine/data/alloc.json, migrated), never the 1,521-property catalogue, and nothing of any trade's vocabulary."""
    document = {"schema_version": 3, "tiers": {"structure": {"names": [{"name": "form:show", "group": "form"}, {"name": "aggregate:sum", "group": "aggregate"}]},
                                                "meaning": {"properties": [{"name": "schema:name", "text": "name: the name of the item", "fine": None}]}}}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "names.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        names = load_name_set(path, meaning=["schema:weight"])
        full = load_name_set(path)
    assert names.names == ("aggregate:sum", "form:show", "schema:weight") and names.meaning_start == 2 and names.meaning == 1 and names.at("form:show") == 1 and names.at("nothing") is None
    assert full.names == ("aggregate:sum", "form:show", "schema:name")
    assert names.sha != NameSet(("form:show",)).sha
    for misplaced in ((("schema:name", "form:show"), 1), (("form:show", "aggregate:sum"), 1)):
        try:
            NameSet(*misplaced)
        except ValueError:
            continue
        raise AssertionError("a meaning tier that is not schema.org's names after the structure tier was taken")
    assert fine_of("schema:director") == "person" and fine_of("class:Corporation") == "company" and fine_of("class:City") == "place" and fine_of("schema:name") is None, "fine answer types by the ontology alone"
    shipped = load_name_set()
    allocation = json.loads((ROOT / "engine" / "data" / "alloc.json").read_text(encoding="utf-8"))
    migrated = {"GeoCoordinates": "geo", "taxonName": "alternateName"}
    basis = tuple(f"schema:{migrated.get(row['name'], row['name'])}" for row in sorted(allocation["dims"], key=lambda row: row["dim_id"]) if row["family"] == "taxonomy")
    assert shipped.names[shipped.meaning_start:] == basis == meaning_names(), "the meaning tier is the selected basis, in alloc.json's order"
    assert {row["family"] for row in allocation["dims"]} == {"taxonomy"} and [row["dim_id"] for row in allocation["dims"]] == list(range(allocation["n_content"])) == list(range(len(basis))), "alloc.json holds only schema.org properties, no structural or intent dimension beside them"
    assert shipped.meaning == len(basis) < 200 and not any(name.startswith(("biz:", "word:")) for name in shipped.names)
    fake = fake_name_set()
    for answer in ANSWER_TYPES:
        assert fake.at(f"answer_type:{answer}") is not None
    try:
        NameSet(("a", "a"))
    except ValueError:
        return
    raise AssertionError("a name twice passed")


# ------------------------------------------------------------------------------------------------- the chooser
def test_chooser_contributions_are_the_logit() -> None:
    chooser = Chooser({"words_explained": 2.0, "type_agrees": 1.0}, -0.5, {"agg": (2.0, 0.5)}, {"agg": 0.8})
    logit, terms = chooser.logit({"words_explained": 0.5, "type_agrees": 1.0, "not_a_feature": 9.0})
    assert logit == -0.5 + 1.0 + 1.0
    assert logit == chooser.bias + sum(term.value for term in terms) and all(isinstance(term, FeatureTerm) and term.feature in FEATURES for term in terms)
    assert chooser.probability(logit, "agg") == round(1 / (1 + np.exp(-(2.0 * logit + 0.5))), 4)
    assert chooser.probability(logit, "show") == round(1 / (1 + np.exp(-logit)), 4)
    back = Chooser.from_record(json.loads(json.dumps(chooser.record())))
    assert back.logit({"type_agrees": 1.0})[0] == chooser.logit({"type_agrees": 1.0})[0] and back.threshold("agg") == 0.8


def test_stable_ties_and_asking_between_close_readings() -> None:
    engine = _engine()
    prepared = engine.prepare(fake_catalog(0))
    named = engine.read("price", prepared)
    catalog = prepared.catalog

    def candidate(position: int, rank: int, probability: float, needs: bool = False) -> Candidate:
        program = Show("t0", (Col("t0", position),), ())
        verified = Verified(score_program(program, named.reading, catalog), run(program, prepared.tables), ())
        return Candidate(verified, {}, probability * 10.0, probability, (), needs, rank)

    open_chooser = Chooser({}, thresholds={form: 0.0 for form in FORMS}, margin=0.5)
    assert answer_or_ask([candidate(2, 0, 0.9), candidate(3, 1, 0.5)], open_chooser).kind == ANSWER
    close = answer_or_ask([candidate(2, 0, 0.9), candidate(3, 1, 0.88)], open_chooser)
    assert close.kind == ASK and close.reasons == ("close_readings",) and close.other is not None
    assert answer_or_ask([candidate(2, 0, 0.3)], Chooser({}, thresholds={"show": 0.5})).reasons == ("low_confidence",)
    assert answer_or_ask([], open_chooser).reasons == ("nothing_ran",)
    outcome = engine.ask("price", prepared)
    assert outcome.kind in (ANSWER, ASK) and "total" in outcome.seconds
    scored = score_candidates(verify_all(engine, prepared, named), named, catalog, PRIOR)
    assert scored == sorted(scored, key=lambda item: (-item.logit, item.rank))


def verify_all(engine, prepared, named):
    from engine.program.verifier import verify
    return verify(prepared.decoder.decode(named.reading, k=10).programs, prepared.tables, named.expected, top=10)


def test_fit_is_deterministic_and_finds_the_signal() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(300, 3))
    y = (x[:, 0] - x[:, 1] > 0).astype(float)
    a = fit_logistic(x, y)
    b = fit_logistic(x, y)
    assert np.array_equal(a[0], b[0]) and a[1] == b[1]
    assert a[0][0] > 1 and a[0][1] < -1 and abs(a[0][2]) < 0.5
    assert calibrate(np.zeros(5), np.zeros(5)) == (1.0, 0.0)
    logit = rng.normal(size=200)
    a2, b2 = calibrate(logit, (logit + rng.normal(size=200) * 0.3 > 0).astype(float))
    assert a2 > 1.0 and abs(b2) < 1.0
    thresholds = choose_thresholds([("agg", 0.9, True)] * 9 + [("agg", 0.9, False)] + [("agg", 0.5, False)] * 5, 1.0)
    assert 0.5 < thresholds["agg"] <= 0.9
    assert choose_thresholds([("agg", 0.9, True)] * 3 + [("agg", 0.4, True)] * 3 + [("agg", 0.4, False)] * 3, 0.5)["agg"] <= 0.4


def _record(question: str, family: str, right: bool, value: float, rank: int = 0) -> CandidateRecord:
    return CandidateRecord(question, family, "agg", {"words_explained": value, "type_agrees": float(right)}, {"measure": value}, right, rank, "x" if right else "y")


def test_biases_move_a_reading_by_kind() -> None:
    from training.named.choose import biased
    reading = Reading({"measure:t0.1": 0.5, "shown:t0.1": 0.5, "value:t0.2=a": -1.0, "contains:t0.2=b": 0.0, "section:t0=c": 2.0, "form:agg": -0.5})
    held = {"measure": 1.0, "key": 1.0, "shown": 1.0, "value": 2.0, "contains": 2.0, "line": 1.0, "section": 1.0}
    moved = biased(reading, held, {"column": 0.0, "value": 0.5, "label": 1.0}).scores
    assert moved["measure:t0.1"] == 1.5 and moved["shown:t0.1"] == 1.5 and moved["value:t0.2=a"] == 0.5 and moved["contains:t0.2=b"] == 1.5 and moved["section:t0=c"] == 2.0 and moved["form:agg"] == -0.5


def test_chooser_fit_keeps_families_apart() -> None:
    fit = [_record(f"q{at // 2}", "a", at % 2 == 0, (at % 2) * 0.1 + 0.2, at % 2) for at in range(40)]
    held = [_record(f"h{at}", "b", at % 2 == 0, 0.5, 0) for at in range(40)]
    chooser = fit_chooser(fit, held)
    assert chooser.weights["type_agrees"] > 0 and "agg" in chooser.calibration and "agg" in chooser.thresholds and chooser.margin in MARGINS
    assert calibrate(np.linspace(-2, 2, 60), (np.linspace(-2, 2, 60) < 0).astype(float)) == (1.0, 0.0)
    near = [[(1.0, 0, CandidateRecord("q", "f", "agg", {}, {}, True, 0, "a")), (0.5, 1, CandidateRecord("q", "f", "agg", {}, {}, False, 1, "b"))], [(1.0, 0, CandidateRecord("r", "f", "agg", {}, {}, False, 0, "c")), (0.99, 1, CandidateRecord("r", "f", "agg", {}, {}, True, 1, "d"))]]
    assert choose_policy(near, Chooser({}), 2, target_wrong=0.0)[1] == 0.5
    groups = groups_of([_record("q", "a", True, 1.0, 0), _record("q", "a", False, 0.0, 1), _record("r", "a", True, 1.0, 0), _record("r", "a", False, 0.2, 1)])
    assert len(groups) == 2
    question = [CandidateRecord(f"q{at}", "a", "agg", {"words_explained": 1.0, "conditions": at % 2}, {}, True, 0, "x") for at in range(30)] + [CandidateRecord(f"q{at}", "a", "agg", {"words_explained": 0.2, "conditions": 1.0}, {}, False, 1, "y") for at in range(30)]
    groups = groups_of(question)
    assert len(groups) == 30
    theta = fit_conditional(groups, len(FEATURES))
    assert theta[FEATURES.index("words_explained")] > 0, "the evidence that sets the right one apart gets the weight"
    assert fit_conditional(groups, len(FEATURES)).tolist() == theta.tolist()
    fold = fold_of("a")
    decided = cross_fit(Chooser(dict(zip(FEATURES, theta.tolist()))), question + [CandidateRecord(f"z{at}", "b", "agg", {"words_explained": 0.5}, {}, True, 0, "x") for at in range(30)])
    assert set(decided) == set(range(5)) and decision_of(question[:1] + question[30:31], decided[fold])[1] is not None
    try:
        fit_chooser(fit, [_record("x", "a", True, 0.1)])
    except ValueError:
        pass
    else:
        raise AssertionError("one family fit and calibrated both")
    assert all(value >= 0 for value in fit_slot_weights(fit).by_slot.values())


# ---------------------------------------------------------------------------------- augmentation, held-out data
def test_survival_ops_keep_gold_answers() -> None:
    catalog = fake_catalog(0, lines=16)
    table = catalog.tables[0]
    program = Show("t0", (Col("t0", 2),), ())
    base = run(program, RequestTables(catalog)).rows
    for name, operation in SURVIVAL_OPS.items():
        mutated = mutate_catalog(catalog, operation)
        assert [t.id for t in mutated.tables] == [t.id for t in catalog.tables]
        new_table = mutated.tables[0]
        if name == "added_columns":
            assert len(new_table.columns) == len(table.columns) + 20
        if name == "unreadable_headers":
            assert all(column.name.isalpha() and column.name.isupper() for column in new_table.columns)     # each named by its letter, a sign of the sheet
        if name != "reorder":
            assert run(program, RequestTables(mutated)).rows == base
    moved, mapping = reorder_columns(table.table, 1)
    assert sorted(mapping) == list(range(len(table.columns)))
    assert moved.columns[mapping[2]].name == table.columns[2].name
    assert moved.lines[0].cells[mapping[2]].raw == table.table.lines[0].cells[2].raw
    added, added_map = add_columns(table.table, 1, 5)
    assert added_map[2] == 2 and len(added.columns) == len(table.columns) + 5


def test_augmentation_tracks_every_position() -> None:
    import random
    table = fake_catalog(0).tables[0].table
    for seed in range(8):
        out, mapping = augmented(table, random.Random(seed), {"rename": 1.0, "mask": 1.0, "shuffle": 1.0, "distract": 1.0})
        assert all(out.lines[0].cells[new].raw == table.lines[0].cells[old].raw for old, new in mapping.items() if old in table.lines[0].cells)
        assert len(out.columns) >= len(table.columns)


def test_families_stay_held_out() -> None:
    examples = fake_examples(6)
    kept, held = split_families(examples, 0.5, 0)
    assert not {e.family for e in kept} & {e.family for e in held} and kept and held
    same = [TabExample("k", examples[0].family, "f", "t", "fake", [], [], {})]
    try:
        check_held_out(examples, same)
    except ValueError:
        pass
    else:
        raise AssertionError("a family on both sides passed")
    frozen = [TabExample("k", "other", "f", "t", "frozen", [], [], {})]
    try:
        check_held_out(examples, frozen)
    except ValueError:
        pass
    else:
        raise AssertionError("a frozen tab passed")
    with tempfile.TemporaryDirectory() as folder:
        write_shard(folder, 0, frozen)
        try:
            read_examples(folder)
        except ValueError:
            pass
        else:
            raise AssertionError("a frozen shard was read")
        write_shard(folder, 0, examples)
        assert len(read_examples(folder, "fake")) == 6
        try:
            read_examples(folder, "train")
        except ValueError:
            return
    raise AssertionError("a side was read as another")


def test_a_columns_property_is_its_meaning_and_its_linked_words_read_it() -> None:
    """A column's schema.org property, as labelled, is what its meaning tier learns, whatever an augmentation did to its header; a word linked to the column learns the same property. No word of the sheet is a coordinate."""
    import random
    examples = fake_examples(4)
    names = fake_name_set()
    batch = build_batch(examples[0], ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16, sample_rows=3), names, random.Random(0), questions=6, shares={"unreadable": 1.0})
    columns = [at for at, target in enumerate(batch.targets) if target.kind == T_COLUMN]
    assert all(target.text.isalpha() and target.text.isupper() for at, target in enumerate(batch.targets) if at in columns)
    assert all(batch.meaning[at] == [MEANINGS[batch.targets[at].position]] for at in columns), "an unreadable header keeps its column's property"
    assert all(names.at(name) < names.meaning_start for held in batch.positive for name in held), "structure names only in the structure tier's labels"
    linked = [held for question in batch.word_positive for held in question if held]
    assert linked and all(name in MEANINGS for held in linked for name in held)


def test_a_labels_own_meaning_trains_the_meaning_tier_and_leaves_the_closed_heads_alone() -> None:
    """label["meaning"] (another labelling pass's basis properties; [] none; None unknown) is what a column's meaning tier and
    its linked words learn, while the closed heads keep reading the label itself: an unknown meaning is masked, not off."""
    import random
    from dataclasses import replace
    example = fake_examples(4)[0]
    names = fake_name_set()
    config = ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16, sample_rows=3)
    keys = sorted(example.labels)
    unknown, other = keys[0], keys[1]
    swapped = {**example.labels, unknown: {**example.labels[unknown], "meaning": None}, other: {**example.labels[other], "meaning": [MEANINGS[0]]}}
    before = build_batch(example, config, names, random.Random(0), questions=6, shares=None)
    after = build_batch(replace(example, labels=swapped), config, names, random.Random(0), questions=6, shares=None)
    column = {f"{target.table}.{target.position}": at for at, target in enumerate(after.targets) if target.kind == T_COLUMN}
    assert after.meaning[column[unknown]] is None and after.meaning[column[other]] == [MEANINGS[0]]
    assert before.meaning[column[unknown]] == [MEANINGS[int(unknown.split(".")[1])]]
    assert after.closed == before.closed, "the closed heads read the label, not its meaning"


def test_the_meaning_tier_starts_from_each_propertys_own_text() -> None:
    """Each basis property is its own named output (a sigmoid, like a structure name), started at its own text: untrained, a unit whose text is a property's own text reads that property first and high, the others low. Labels anchor the outputs from there."""
    names = fake_name_set()
    backbone = HashBackbone()
    reader = NamedReader(backbone.dim, names.meaning_start, names.meaning, ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16)).float()
    read_descriptions(backbone, reader, names)
    text_of = dict(zip(meaning_names(), property_texts()))
    texts = [text_of[name] for name in MEANINGS]
    _, base = backbone.encode_texts(texts)
    order = {text: at for at, text in enumerate(sorted(set(texts), key=lambda text: (len(text), text)))}
    base = torch.stack([base[order[text]] for text in texts])
    with torch.no_grad():
        read = reader.coordinate_probabilities(torch.randn(len(texts), 36), base)[:, names.meaning_start:]
    assert read.argmax(-1).tolist() == list(range(len(MEANINGS)))
    assert all(float(row.max()) > 0.9 for row in read), "a property's own text reads it high"
    both = reader.coordinate_probabilities(torch.randn(1, 36), base[:2].mean(0, keepdim=True))[0, names.meaning_start:]
    assert float(both[0]) + float(both[1]) > 1.0 or float(both[:2].min()) > float(both[2:].max()), "outputs are not a share: a unit between two properties may hold both"


def test_the_meaning_anchor_is_each_propertys_own_log_loss() -> None:
    """Known units anchor every basis output (labelled on, the rest off); a unit whose meaning is unknown anchors nothing."""
    from training.named.train import _meaning_loss
    names = fake_name_set()
    logits = torch.zeros((3, names.meaning))
    assert _meaning_loss(logits, [None, None, None], names) is None
    loss = _meaning_loss(logits, [[MEANINGS[0]], [], None], names)
    expected = float(torch.nn.functional.binary_cross_entropy_with_logits(torch.zeros((2, names.meaning)), torch.tensor([[1.0] + [0.0] * (names.meaning - 1), [0.0] * names.meaning]), reduction="sum")) / 2 / 4.0
    assert abs(float(loss) - expected) < 1e-5


def test_self_anchors_are_schema_orgs_own_text_and_nothing_of_ours() -> None:
    """Step 1: each basis property's own schema.org text reads that property at 1; a related property keeps its soft value, an unrelated one goes to 0; the property rows alone train on them. No structure definition is anchor text."""
    from training.named.train import RELATED_AT, self_anchors, train_self_anchors
    names = fake_name_set()
    backbone = HashBackbone()
    torch.manual_seed(0)
    reader = NamedReader(backbone.dim, names.meaning_start, names.meaning, ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16)).float()
    read_descriptions(backbone, reader, names)
    anchors = self_anchors(backbone, reader, names, "cpu")
    count = names.meaning
    assert anchors.targets.shape == (2 * count, count) and torch.equal(anchors.targets[:count].diagonal(), torch.ones(count))
    assert torch.equal(anchors.related, anchors.related.T) and not anchors.related.diagonal().any()
    off = anchors.targets[:count][~torch.eye(count, dtype=torch.bool)]
    assert bool(((off == 0) | (off >= RELATED_AT)).all()), "an unrelated property's target is 0, a related one keeps its soft value"
    text_of = dict(zip(meaning_names(), property_texts()))
    assert [graph.texts[1] for graph in anchors.graphs[:count]] == [text_of[name] for name in MEANINGS], "the anchor text is the property's own schema.org text"
    before = {name: value.clone() for name, value in reader.state_dict().items()}
    check = train_self_anchors(backbone, reader, anchors, 200, 0.05)
    assert check["own_lowest"] >= 0.9 and check["unrelated_highest"] <= 0.1, check
    moved = {name for name, value in reader.state_dict().items() if not torch.equal(value, before[name])}
    assert moved and all(name.startswith(("meaning_text", "meaning_read")) for name in moved), "only the property rows train on the self-anchors"


def test_unknown_and_related_properties_are_masked_never_trained_as_zero() -> None:
    from training.named.train import _labelled_names, meaning_targets
    names = fake_name_set()
    assert _labelled_names(None) is None and _labelled_names({"raw": "price", "conf": 3, "coordinates": [MEANINGS[0]]}) is None, "no label, or an unsure one: unknown"
    assert _labelled_names({"raw": "legalName", "conf": 8, "coordinates": []}) is None, "a property outside the basis says nothing of the basis"
    assert _labelled_names({"raw": "none", "conf": 8, "coordinates": []}) == [] and _labelled_names({"raw": "name", "conf": 8, "coordinates": [MEANINGS[0]]}) == [MEANINGS[0]]
    related = torch.zeros((names.meaning, names.meaning), dtype=torch.bool)
    related[0, 1] = related[1, 0] = True
    rows, wanted, masks = meaning_targets([[MEANINGS[0]], None, []], names, related, "cpu")
    assert rows == [0, 2]
    assert wanted[0].tolist() == [1.0, 0.0, 0.0, 0.0, 0.0, 0.0] and masks[0].tolist() == [1.0, 0.0, 1.0, 1.0, 1.0, 1.0], "the labelled property on, its related one masked, the rest off"
    assert wanted[1].sum() == 0 and masks[1].sum() == names.meaning, "a column no schema.org property fits: every property off"


def test_a_corpus_table_is_read_as_its_property_profile_without_its_class_name() -> None:
    """Steps 2-3: a corpus instance is its columns; the part of its text that names the source relation, and so the class, is never read. The profile is the highest reading of each property over the columns."""
    from engine.named.units import rendered_graph
    from training.named.train import profile_logits, profile_loss, read_corpus
    names = fake_name_set()
    row = {"text": "table wikidata Movie | title: Alpha; Beta | made in: Peru", "properties": ["https://schema.org/name", "https://schema.org/director", "https://schema.org/location"],
           "classes": ["https://schema.org/Movie"], "split": "train"}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "corpus.jsonl"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        corpus = read_corpus(path, names)
    assert corpus[0].columns == [("title", ["Alpha", "Beta"]), ("made in", ["Peru"])] and corpus[0].classes == ["Movie"]
    assert corpus[0].properties == ["schema:name", "schema:location"], "the basis properties it shows; one outside the name set is not a label"
    backbone = HashBackbone()
    torch.manual_seed(0)
    config = ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16)
    reader = NamedReader(backbone.dim, names.meaning_start, names.meaning, config).float()
    read_descriptions(backbone, reader, names)
    assert not any("Movie" in text for text in rendered_graph(corpus[0].columns).texts)
    with torch.no_grad():
        profile = profile_logits(backbone, reader, config, corpus)
        assert profile.shape == (1, names.meaning) and float(profile_loss(backbone, reader, config, names, corpus, None)) > 0


def test_a_step_starts_from_the_bundle_of_the_step_before() -> None:
    from engine.named.bundle import write_bundle
    from training.named.train import link_weights_of, start_from
    names = fake_name_set()
    backbone = HashBackbone()
    config = ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16)
    torch.manual_seed(1)
    reader = NamedReader(backbone.dim, names.meaning_start, names.meaning, config).float()
    with torch.no_grad():
        reader.raw_coordinate_weights.normal_()
        reader.raw_link_bias.normal_()
    with tempfile.TemporaryDirectory() as folder:
        write_bundle(folder, backbone, reader, names, config, link_weights_of(reader))
        torch.manual_seed(2)
        again = NamedReader(backbone.dim, names.meaning_start, names.meaning, config).float()
        start_from(folder, backbone, again, names, "cpu")
    assert np.allclose(link_weights_of(again).coordinates, link_weights_of(reader).coordinates, atol=1e-4) and link_weights_of(again).slot_biases == link_weights_of(reader).slot_biases
    assert all(torch.equal(value, again.state_dict()[name]) for name, value in reader.state_dict().items() if not name.startswith("raw_"))


def test_anchoring_alone_trains_with_the_corpus_and_keeps_the_last_measure() -> None:
    """Steps 1-3 as one run on the fake: the self-anchors first, then anchoring with the links untrained, a corpus batch beside the sheets."""
    row = {"text": "table x.y | counterparty: Aquila; Duke | region: East", "properties": ["https://schema.org/name", "https://schema.org/location"], "classes": [], "split": "train"}
    with tempfile.TemporaryDirectory() as folder:
        corpus = Path(folder) / "corpus.jsonl"
        corpus.write_text("".join(json.dumps(row) + "\n" for _ in range(4)), encoding="utf-8")
        config = TrainConfig(reader=ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16, sample_rows=3), steps=9, questions=6, seed=0, held_share=0.2, eval_every=4, eval_tabs=2,
                             link_weight=0.0, open_weight=0.0, anchor_steps=20, corpus=str(corpus))
        out = Path(folder) / "out"
        summary = train(fake_examples(6), fake_name_set(), HashBackbone(), config, out, log_print=False)
        log = [json.loads(line) for line in (out / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        check = json.loads((out / "self_anchors.json").read_text(encoding="utf-8"))
    assert summary["steps"] == 9 and summary["best_top1"] == 9.0, "with the links untrained the last measure is kept"
    assert check["steps"] == 20 and any("anchor" in row and "profile" in row for row in log)


def _pick_example(side: str = "clef", family: int = 0) -> TabExample:
    """The fake's first tab with one question that has no gold program, only a teacher's picks: the measure is column 2 of two options, no line is picked, the aggregate is sum."""
    from engine.named.units import Target
    example = fake_examples(family + 1)[family]
    table_id = example.tables[0][0]
    column = lambda position: Target(table_id, T_COLUMN, position, "").coordinates()[0]
    row = {"id": f"pick{family}", "question": "how much gas did Aquila take", "label": "pick", "tables": [table_id],
           "picks": {"measure": {"coordinates": [column(3), column(2)], "pick": 1}, "key": {"coordinates": [Target(table_id, T_COLUMN, 0, "").coordinates()[1]], "pick": None}},
           "closed": {"aggregate": "sum", "form": "agg"}}
    return TabExample(f"pick|{example.key}", example.family, example.file, example.tab, side, example.tables, [row], {})


def test_a_pick_is_a_link_to_the_teachers_choice_with_no_span_and_no_invented_target() -> None:
    """Step 4: a field's pick becomes a link whose first candidate is the teacher's choice and whose span is every word (the best-linking one is used); a field it picked none of is a link to nothing; only the closed heads it named are trained. Nothing is labelled on any dimension."""
    import random
    from training.named.train import UNKNOWN, losses_of, run_batch
    names = fake_name_set()
    example = _pick_example()
    batch = build_batch(example, CONFIG, names, random.Random(0), questions=4, shares=None)
    assert len(batch.links) == 2 and all(link.any_word for link in batch.links)
    measure, key = batch.links
    assert not measure.nothing and batch.targets[measure.candidates[0]].position == 2 and batch.targets[measure.candidates[1]].position == 3, "the pick first, then the other options"
    assert key.nothing and key.role == "key" and measure.span == list(range(len(batch.questions[0].split())))
    assert batch.closed[0]["aggregate"] == "sum" and batch.closed[0]["form"] == "agg" and batch.closed[0]["direction"] is None
    assert batch.word_positive[0] == [None] * len(measure.span) and set(batch.word_role[0]) == {UNKNOWN}, "no word is given a meaning or a role"
    torch.manual_seed(0)
    backbone = HashBackbone()
    reader = NamedReader(backbone.dim, names.meaning_start, names.meaning, CONFIG).float()
    read_descriptions(backbone, reader, names)
    out = run_batch(backbone, reader, CONFIG, batch, "cpu")
    parts = losses_of(reader, names, batch, out, False, "cpu")
    assert float(parts["pick"]) > 0 and float(parts["link"]) == 0 and float(parts["role"]) == 0 and float(parts["meaning"]) == 0
    parts["pick"].backward()
    assert reader.raw_coordinate_weights.grad is not None and float(reader.structure.weight.grad.abs().sum()) > 0, "the gradient reaches the named readings through the link's sum"
    assert reader.open_word.weight.grad is None, "the unnamed channel is not in the sum a pick trains"


def test_only_qualified_properties_enter_a_links_sum() -> None:
    from training.named.train import link_weights_of, qualified_mask
    names = fake_name_set()
    with tempfile.TemporaryDirectory() as folder:
        gate = Path(folder) / "gate.json"
        gate.write_text(json.dumps({"qualified_for_links": [MEANINGS[0]]}), encoding="utf-8")
        mask = qualified_mask(gate, names, "cpu")
    assert mask[:names.meaning_start].sum() == names.meaning_start and mask[names.meaning_start:].tolist() == [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    reader = NamedReader(32, names.meaning_start, names.meaning, ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16))
    weights = link_weights_of(reader, mask).coordinates
    assert weights[names.meaning_start] > 0 and not weights[names.meaning_start + 1:].any(), "a property the gate did not qualify weighs nothing in a bundle's links"


def test_training_on_picks_beside_the_sheets() -> None:
    with tempfile.TemporaryDirectory() as folder:
        picks = Path(folder) / "picks"
        picks.mkdir()
        write_shard(picks, 0, [_pick_example("clef", 0), _pick_example("clef", 1), _pick_example("clef_held", 2)])
        config = TrainConfig(reader=ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16, sample_rows=3), steps=9, questions=6, seed=0, held_share=0.2, eval_every=4, eval_tabs=2, picks=str(picks), open_detached=True)
        out = Path(folder) / "out"
        summary = train(fake_examples(6), fake_name_set(), HashBackbone(), config, out, log_print=False)
        log = [json.loads(line) for line in (out / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert summary["steps"] == 9 and any(row.get("pick", 0) > 0 for row in log if "loss" in row), "a pick batch joins the sheets"
    measured = [row for row in log if "pick_top1" in row]
    assert measured and measured[-1]["picks"] == 2, "the held pick example is measured, never trained on"


def test_batch_has_in_sheet_hard_negatives() -> None:
    import random
    example = fake_examples(1)[0]
    names = fake_name_set()
    batch = build_batch(example, CONFIG, names, random.Random(0), questions=8, shares=None)
    assert batch.links and all(len(link.candidates) >= 2 for link in batch.links)
    table_ids = {batch.targets[at].table for link in batch.links for at in link.candidates}
    assert table_ids == {"t0"}
    for link in batch.links:
        assert len({batch.targets[at].kind for at in link.candidates}) == 1
        assert link.rules.shape == (len(link.candidates), len(RULES))
    assert batch.closed[0]["form"] in FORMS
    augmented_batch = build_batch(example, CONFIG, names, random.Random(1), questions=8, shares={"shuffle": 1.0, "distract": 1.0, "mask": 1.0, "rename": 1.0})
    assert augmented_batch.links


# ------------------------------------------------------------------------------------------------- the pipeline
def test_coordinate_loss_reads_as_a_probability() -> None:
    """A class-balanced loss put 71 of 385 names on for every word, and every target linked alike. Each name's own log loss is lowest at the true rate, so what the head says of a rare name stays rare."""
    from training.named.train import _row_loss
    labels = torch.zeros(1000, 1)
    labels[:20] = 1.0
    at_rate = _row_loss(torch.full((1000, 1), float(np.log(0.02 / 0.98))), labels, torch.ones(1000, 1))
    assert all(at_rate < _row_loss(torch.full((1000, 1), float(np.log(rate / (1 - rate)))), labels, torch.ones(1000, 1)) for rate in (0.005, 0.1, 0.5))


def test_a_bundle_carries_learned_biases_per_kind_of_target() -> None:
    from training.named.train import link_weights_of
    from engine.named.model import LINK_KINDS
    reader = NamedReader(32, 6, 2, ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16))
    with torch.no_grad():
        reader.raw_link_bias[LINK_KINDS.index("value")] = 2.0
    weights = link_weights_of(reader)
    assert weights.bias("value") == weights.bias("contains") and weights.bias("measure") == weights.bias("shown") == weights.bias("key")
    assert weights.bias("value") > weights.bias("measure") and weights.bias("period") == 0.0


def test_training_resumes_from_its_state() -> None:
    """A pod can die with hours of training on it (2026-11-02, step 14,800). What a lease brings back goes on from where it stood."""
    with tempfile.TemporaryDirectory() as folder:
        config = TrainConfig(reader=ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16, sample_rows=3), steps=8, questions=6, seed=0, held_share=0.2, eval_every=4, eval_tabs=2)
        first = os.path.join(folder, "a")
        Path(first).mkdir()
        short = TrainConfig(reader=config.reader, steps=8, questions=6, seed=0, held_share=0.2, eval_every=4, eval_tabs=2, stop_at=4)
        train(fake_examples(6), fake_name_set(), HashBackbone(), short, first, log_print=False)
        state = os.path.join(first, "last_state.pt")
        assert os.path.exists(state)
        second = os.path.join(folder, "b")
        again = TrainConfig(reader=config.reader, steps=8, questions=6, seed=0, held_share=0.2, eval_every=4, eval_tabs=2, resume=state)
        summary = train(fake_examples(6), fake_name_set(), HashBackbone(), again, second, log_print=False)
        assert summary["steps"] == 8 and summary["resumed_at"] == 4 and not os.path.exists(os.path.join(second, "last_state.pt"))


def test_training_runs_under_autocast() -> None:
    """A lease trains under bf16 autocast, and the first one died at its first step mixing bf16 reads into a float32 tensor. The reads come out in float32, the losses count in it."""
    with tempfile.TemporaryDirectory() as folder:
        config = TrainConfig(reader=ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16, sample_rows=3), steps=4, questions=6, seed=0, held_share=0.2, eval_every=4, eval_tabs=2, amp=True)
        summary = train(fake_examples(6), fake_name_set(), HashBackbone(), config, folder, log_print=False)
        assert summary["steps"] == 4


def test_training_lowers_the_loss_and_is_repeatable() -> None:
    """A pipeline check on the fake: loss falls, a bundle comes back, and it reads the same as what was trained. Not an accuracy result."""
    with tempfile.TemporaryDirectory() as folder:
        examples = fake_examples(6)
        names = fake_name_set()

        def once(out: str, seed: int = 0):
            torch.manual_seed(seed)
            config = TrainConfig(reader=ReaderConfig(backbone="hash", hidden=36, blocks=1, open_dim=16, sample_rows=3), steps=60, questions=6, head_lr=3e-3, lr=1e-3, seed=seed, held_share=0.2, eval_every=60, eval_tabs=4, augment={"shuffle": 0.5})
            return train(examples, names, HashBackbone(), config, out, log_print=False), config

        summary, config = once(os.path.join(folder, "a"))
        assert summary["steps"] == 60 and summary["loss_last"] < summary["loss_first"], summary
        assert summary["train_tabs"] and summary["held_tabs"]
        again, _ = once(os.path.join(folder, "b"))
        assert again["loss_last"] == summary["loss_last"]
        held = set(json.loads(Path(folder, "a", "held_families.json").read_text(encoding="utf-8")))
        assert held and held <= {example.family for example in examples}
        engine = load_bundle(Path(folder, "a", "best"), backbone=HashBackbone())
        catalog = fake_catalog(0)
        prepared = engine.prepare(catalog)
        outcome = engine.ask("total volume", prepared)
        assert outcome.kind in (ANSWER, ASK)
        bundle = read_bundle(Path(folder, "a", "best"), backbone=HashBackbone())
        assert bundle.names.sha == names.sha and bundle.reader_id == engine.encoder.reader_id
        assert len(bundle.weights.coordinates) == len(names)
        write_bundle(Path(folder, "c"), bundle.backbone, bundle.reader, bundle.names, bundle.config, bundle.weights)
        assert read_bundle(Path(folder, "c"), backbone=HashBackbone()).reader_id == bundle.reader_id


def test_a_frozen_word_table_stays_out_of_a_bundle() -> None:
    engine = _engine()
    engine.backbone.table.weight.requires_grad_(False)
    with tempfile.TemporaryDirectory() as folder:
        write_bundle(folder, engine.backbone, engine.reader, engine.names, engine.config, engine.weights)
        state = torch.load(Path(folder, "reader.pt"), weights_only=True)
    assert "backbone.table.weight" not in state and "backbone.out.weight" in state


def _items(families=(0, 1)) -> list[Item]:
    items = []
    for family in families:
        catalog = fake_catalog(family)
        for row in fake_rows(family, catalog):
            record = record_of(row["answer"], row["form"])

            def grade(rows, record=record):
                from training.factory.agree import same_scalar
                return bool(rows) and record["expected"] is not None and same_scalar(rows[0][0], record["expected"][0] if isinstance(record["expected"], list) else record["expected"])
            items.append(Item(row["id"], row["question"], f"book{family}", row["family"], row["form"], "gold", lambda family=family: fake_catalog(family), grade, None, row["links"]))
    return items


def test_evaluation_runs_the_whole_engine_end_to_end() -> None:
    """Sheet model, index, reader, decoder, verifier, chooser, answer or ask, both modes and the words floor; the report's fields and the survival tests. A pipeline check: the model is untrained."""
    engine = _engine()
    items = _items()
    for mode in (MODE_NAMED, MODE_NAMED_PLUS_OPEN):
        report = run_mode(items, engine, mode)
        gold = report["by_label"]["gold"]
        assert gold["right"] + gold["asked"] + gold["wrong"] == gold["n"] == len(items)
        assert report["p50_ms"] > 0 and report["p99_ms"] >= report["p50_ms"] and report["links"] > 0 and report["link_top10"] is not None
        assert report["peak_gb"] > 0
        if mode == MODE_NAMED:                         # the next mode reads the same model's cached index
            assert report["index_seconds_p50"] is not None and report["index_kb_p50"] > 0
        assert {"read", "decode", "verify", "choose", "total"} <= set(report["stages_p50_ms"])
    floor = run_mode(items, None, "words")      # the sheet's own words and the question's digits only: a floor, not a reader
    assert sum(floor["by_label"]["gold"][key] for key in ("right", "asked", "wrong")) == floor["by_label"]["gold"]["n"] == len(items)
    survival = run_survival(items, floor, None, "words", 3)
    assert set(survival) == set(SURVIVAL_OPS) and all(row["n"] <= 3 for row in survival.values())
    named = run_survival(items, {"records": [{"id": item.id, "result": "right", "label": "gold"} for item in items]}, engine, MODE_NAMED, 2)
    assert all(row["n"] == 2 for row in named.values())


def test_grading_and_gate_a() -> None:
    gold = Item("a", "q", "b", "f", "agg", "gold", lambda: None, lambda rows: rows == [(1,)], None, [])
    assert graded(gold, ANSWER, [(1,)]) == "right" and graded(gold, ANSWER, [(2,)]) == "wrong" and graded(gold, ASK, ()) == "asked"
    refusal = Item("b", "q", "b", "f", "", "refusal", lambda: None, lambda rows: False, None, [])
    assert graded(refusal, ASK, ()) == "right" and graded(refusal, ANSWER, [(1,)]) == "wrong"
    ambiguous = Item("c", "q", "b", "f", "", "ambiguous", lambda: None, lambda rows: False, lambda rows: rows == [(3,)], [])
    assert graded(ambiguous, ASK, ()) == "right" and graded(ambiguous, ANSWER, [(3,)]) == "right" and graded(ambiguous, ANSWER, [(4,)]) == "wrong"
    assert record_of([["ENA", 5.0]], "rank") == {"expected": "ENA", "alternates": {"figure": 5.0}, "accept_text": [], "tolerance": 0}
    assert record_of([["a"], ["b"]], "show")["expected"] == ["a", "b"]
    assert record_of([[None]], "agg")["expected"] is None
    report = {"by_label": {"gold": {"right": 50, "asked": 40, "wrong": 10, "n": 100}}, "link_top10": 0.95, "p50_ms": 250.0}
    survival = {name: {"n": 100, "share": GATE_A[name]} for name in ("reorder", "added_columns", "renames", "unreadable_headers")}
    assert gate_a(report, survival)["passes"]
    assert not gate_a(report, None)["passes"] and not gate_a(report, None)["all_measured"]
    low = {"by_label": {"gold": {"right": 30, "asked": 60, "wrong": 10, "n": 100}}, "link_top10": 0.95, "p50_ms": 250.0}
    assert gate_a(low, survival)["stop_line"] and not gate_a(report, survival)["stop_line"]


# ----------------------------------------------------------------------------------------------------- the lease
def test_lease_plan_is_size_aware() -> None:
    small = plan_candidate("bge-small", 7000, 40000)
    large = plan_candidate("xlmr-base", 7000, 40000)
    assert small.steps == large.steps == 4 * 7000
    assert small.bundle_mb < 120 and 300 < large.bundle_mb < 400 and bundle_mb("xlmr-base") == large.bundle_mb
    assert large.transfer_minutes_limit > small.transfer_minutes_limit and large.lease_minutes > small.lease_minutes
    assert 0 < small.dollars_low < small.dollars_high and small.dollars_low < large.dollars_low
    assert plan_candidate("bge-small", 10, 10, steps=500).steps == 500
    ensure_same_tables("HEAD")
    pickled = 'class Cell:\n    """one cell"""\n    raw: str\n    kind: str\n'
    assert dataclass_fields(pickled) == dataclass_fields(pickled.replace("one cell", "a cell")), "a docstring is nothing a pickle holds"
    assert dataclass_fields(pickled) != dataclass_fields(pickled.replace("    kind: str\n", "")), "a field is"
    assert "engine/data/schema_org_v30.json" not in pod_code(), "the pod reads the name set's basis, never the catalogue"
    command = remote_command("bge-small", 500, 3)
    assert "training.named.train" in command and "--seed 3" in command and "--steps 500" in command and "exit 4" in command


def test_lease_refuses_until_transfers_are_size_aware() -> None:
    from training.tools import runpod_api
    if all(hasattr(runpod_api, name) for name in ("_transfer_seconds", "_compress_outputs", "_download")):
        ensure_size_aware()
        return
    try:
        ensure_size_aware()
    except SystemExit as stop:
        assert "phase0" in str(stop)
        return
    raise AssertionError("a paid lease would have run on the 15-minute transfers")


def test_pod_code_is_a_closed_set() -> None:
    """What a pod receives imports the trainer with nothing else of the repository, and pulls in no SQL stack it does not have."""
    with tempfile.TemporaryDirectory() as folder:
        import shutil
        for relative in pod_code():
            target = Path(folder, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        output = subprocess.run([sys.executable, "-c", "import sys; import training.named.train as t; print('sqlalchemy' in sys.modules, t.__file__)"], capture_output=True, text=True, cwd=folder, env=env, timeout=300)
        assert output.returncode == 0, output.stderr[-1200:]
        loaded, where = output.stdout.strip().splitlines()[-1].split(" ", 1)
        assert loaded == "False", "the trainer pulled in sqlalchemy"
        assert Path(where).resolve().is_relative_to(Path(folder).resolve())


def test_units_config_defaults_are_the_readers() -> None:
    assert ReaderConfig().units == UnitsConfig()
    assert len(targets_of(fake_catalog(0).tables[0], UnitsConfig(value_cap=2))) < len(targets_of(fake_catalog(0).tables[0], UnitsConfig()))


# ---------------------------------------------------------------------------------------------- the alignment
def _aligned_case():
    from engine.named.align import prior_weights
    engine = _engine()
    engine.aligner = None
    prepared = engine.prepare(fake_catalog(0))
    named = engine.read("total volume mmbtu", prepared)
    weights = prior_weights({"exact": 2.0, "prefix": 1.0, "stated": 1.0})
    return engine, prepared, named, weights


def test_alignment_gives_each_word_one_part_and_shows_its_terms() -> None:
    from engine.named.align import Aligner, parts_of
    engine, prepared, named, weights = _aligned_case()
    aligner = Aligner(weights, engine.names, engine.weights.coordinates)
    builder = aligner.builder(named, prepared.catalog)
    asked = Agg(SUM, Col("t0", 2), "t0")
    other = Agg(SUM, Col("t0", 3), "t0")
    one, two = aligner.align(asked, builder, prepared.catalog, terms=True), aligner.align(other, builder, prepared.catalog, terms=True)
    assert one.total > two.total                     # the header the question names beats a sibling column nobody named
    assert len({word.at for word in one.words}) == len(one.words)
    parts = set(parts_of(asked, prepared.catalog))
    assert all(word.part is None or word.part in parts for word in one.words)
    assert {part for part, _ in one.unasked} == parts - {word.part for word in one.words if word.part}
    for word in one.words:                         # a word's score is its named terms, nothing else
        assert abs(word.score - sum(weight * value for _, weight, value in word.terms)) < 1e-3
    assert one.total == round(one.aligned + one.nothing + one.unasked_total, 4)
    assert aligner.align(asked, aligner.builder(named, prepared.catalog), prepared.catalog).features() == one.features()


def test_training_scores_are_the_serving_alignment() -> None:
    import torch as _torch
    from engine.named.align import compile_weights, parts_of
    from training.named.align import Collected, _Tensors, _scores, _theta_of, CANDIDATE_FEATURES
    from engine.named.align import EvidenceBuilder, align_parts
    engine, prepared, named, weights = _aligned_case()
    builder = EvidenceBuilder(named, prepared.catalog, engine.weights.coordinates, engine.names)
    programs = [Agg(SUM, Col("t0", 2), "t0"), Agg(SUM, Col("t0", 3), "t0"), Show("t0", (Col("t0", 0),), ())]
    names = sorted({name for program in programs for name in parts_of(program, prepared.catalog)})
    rows = [{"parts": tuple(names.index(name) for name in parts_of(program, prepared.catalog)), "features": {}, "right": at == 0, "answer": "x" if at else "y", "rank": at} for at, program in enumerate(programs)]
    question = Collected("q", "f", builder.question_evidence(), [builder.part(name) for name in names], rows)
    tensors = _Tensors(question, _torch, len(engine.names))
    assert tensors.ok and tensors.candidate.shape[1] == len(CANDIDATE_FEATURES)
    score, _, _ = _scores(tensors, _theta_of(weights, engine.names, _torch), _torch)
    compiled = compile_weights(weights, engine.names)
    served = [align_parts(question.question, [question.parts[at] for at in row["parts"]], compiled).total for row in rows]
    assert np.allclose(score.detach().numpy(), served, atol=1e-3)


def test_the_gate_judges_each_fold_at_the_threshold_the_other_folds_set():
    """The gate sets and judges thresholds on one truth: its val folds, each judged at the threshold the other folds set, never
    by its own reading. The anchoring bundle failed every dimension when the first pass's single labels set the thresholds and
    the second pass's judged them (price as a column: 0.73 against 0.99 at one reading)."""
    from types import SimpleNamespace
    from training.named.gate_dimensions import folded, judge
    names = SimpleNamespace(names=("meaning:a",), meaning_start=0)

    def fold(positive: float, negative: float, n: int = 40):
        scores = np.array([[positive]] * n + [[negative]] * n)
        return scores, np.array([[1.0]] * n + [[0.0]] * n), np.ones((2 * n, 1))

    row = judge(folded([fold(0.9, 0.6), fold(0.7, 0.65)]), names)["meaning:a"]
    assert row["thresholds"] == [0.7, 0.9], row["thresholds"]          # fold 0 at fold 1's threshold, fold 1 at fold 0's
    assert row["passes"] and row["precision"] == 1.0 and row["recall"] == 0.5 and row["held_positives"] == 80
    never = judge(folded([fold(0.9, 0.6), fold(0.5, 0.95)]), names)["meaning:a"]
    assert never["thresholds"][1] is not None and never["thresholds"][0] is None and not never["passes"]
    assert never["why"] == "no threshold reaches the precision on the calibration units"


def main() -> int:
    tests = [(name, item) for name, item in globals().items() if name.startswith("test_") and callable(item)]
    failed = 0
    for name, item in tests:
        try:
            item()
        except Exception as exc:  # noqa: BLE001 - every test reports
            failed += 1
            import traceback
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=4)
    print(f"{len(tests) - failed} of {len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
