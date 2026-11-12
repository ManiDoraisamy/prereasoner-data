"""The factory's own rules, each on small hand-made cases: the program text binds to the language's forms and refuses what it cannot say; the screen fires on what it must; agreement between two routes; the noise; the links; the sandbox refuses; the name set is whole and holds no fork of the closed lists. Hermetic: no Gemini, no corpus. Runs with: python -m tests.test_factory."""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile

from engine.program.catalog import Catalog
from engine.program.execute import run
from engine.program.language import Agg, Calc, Rank, Show, Size
from engine.program.names import ANSWER_TYPES, FORMS
from engine.program.store import RequestTables
from engine.sheet import compile_workbook, workbook_from_json
from training.factory.agree import agrees, as_day, same_scalar
from training.factory.assemble import apply_quotas, is_subject_only, link_records, named, rule_reaches
from training.factory.factory import parse_author
from training.factory.grammar import Unbound, bind
from training.factory.noise import SHARE, messy
from training.factory.overlap import overlap_of
from training.factory.prompts import plan_slots
from training.factory.screen import screen_text, screen_workbook
from training.factory.vocabulary import PACK

HERE = os.path.dirname(os.path.abspath(__file__))
SHEET = {"name": "t.xlsx", "tabs": [{"name": "Deals", "rows": [["Counterparty", "Region", "Volume", "Price", "Trade Date"], ["ENA", "East", 100.0, 3.5, "2001-02-01"], ["ENA", "West", 50.0, 4.0, "2001-03-01"], ["Duke", "East", 25.0, 3.0, "2001-02-15"], ["Reliant", "West", 75.0, 2.5, "2001-03-15"], ["Total", "", 250.0, "", ""]]}]}


def _catalog() -> Catalog:
    return Catalog.from_model(compile_workbook(workbook_from_json(SHEET)))


def _rows(text: str, catalog: Catalog):
    return run(bind(text, catalog), RequestTables(catalog)).rows


def test_grammar_binds_every_form() -> None:
    catalog = _catalog()
    assert isinstance(bind('agg sum 2 @t0 where 1="East"', catalog), Agg)
    assert isinstance(bind("agg count @t0", catalog), Agg)
    assert isinstance(bind("list 0 @t0 where 2>=75", catalog), Show)
    assert isinstance(bind("rank line high 2 show 0 @t0", catalog), Rank)
    assert isinstance(bind("rank group high sum 2 by 0 @t0 size", catalog), Rank)
    assert isinstance(bind('calc (agg sum 2 @t0 where 1="East") - (agg sum 2 @t0 where 1="West")', catalog), Calc)
    assert isinstance(bind("agg max abs(2) @t0", catalog).measure, Size)


def test_grammar_answers_through_engine_program() -> None:
    catalog = _catalog()
    assert _rows('agg sum 2 @t0 where 1="East"', catalog)[0][0] == 125.0
    assert _rows("agg sum 2 @t0", catalog)[0][0] == 250.0, "the printed total is not added to its lines"
    assert _rows("rank group high sum 2 by 0 @t0", catalog)[0][0] == "ENA"
    assert _rows('calc (agg sum 2 @t0 where 1="East") - (agg sum 2 @t0 where 1="West")', catalog)[0][0] == 0.0
    assert _rows("agg sum 2*3 @t0", catalog)[0][0] == 100 * 3.5 + 50 * 4 + 25 * 3 + 75 * 2.5


def test_grammar_refuses() -> None:
    catalog = _catalog()
    for text, why in (("agg sum 9 @t0", "unknown_column"), ("agg sum a0 @t0", "unknown_column"), ("agg sum 2 @t7", "unknown_table"), ("agg sum 2", "no_table"), ("rank line high 2 show 2 @t0", "unsound"), ('agg count @t0 where 0<>"ENA"', "bad_condition"), ("sum 2 @t0", "unknown_form")):
        try:
            bind(text, catalog)
        except Unbound as exc:
            assert str(exc) == why, (text, str(exc))
        else:
            raise AssertionError(f"bound: {text}")
    try:
        bind("agg sum 2 @t0", catalog, allowed={"t1"})
    except Unbound as exc:
        assert str(exc) == "unknown_table"
    else:
        raise AssertionError("a table off the tab bound")


def test_screen() -> None:
    assert "bank_details" in screen_text("Wire instructions")
    assert "bank_details" in screen_text("Account No. 1234")
    assert "identity_number" in screen_text("123-45-6789")
    assert "password" in screen_text("passcode")
    assert "corpus_word" in screen_text("Password")
    assert "personal_matter" in screen_text("EGM 2000 Year-end Pre-ranking")
    assert "card_number" in screen_text("4111 1111 1111 1111")
    assert not screen_text("Delivery point Henry Hub 3.50 MMBtu")
    assert not screen_text("1234567890123456"), "not a card by its check digit"
    assert screen_workbook(SHEET) == []
    many = {"tabs": [{"name": "x", "rows": [[f"a{i}@b.com"] for i in range(3)]}]}
    assert screen_workbook(many) == ["emails"]
    phones = {"tabs": [{"name": "x", "rows": [[f"713-853-{i:04d}"] for i in range(10)]}]}
    assert screen_workbook(phones) == ["contact_list"]


def test_agree() -> None:
    assert agrees("agg", False, [[100.0]], 100.0000001)
    assert not agrees("agg", False, [[100.0]], 101)
    assert agrees("agg", False, [[0.25]], 25.0), "a share either way"
    assert agrees("show", False, [["2001-02-01"]], "2/1/2001")
    assert agrees("show", False, [["East  Coast"]], "east coast")
    assert agrees("rank", False, [["ENA", 150.0]], ["ENA", 150.0])
    assert agrees("rank", False, [["ENA", 150.0]], "ENA")
    assert agrees("rank", False, [["ENA", 150.0]], 150.0), "highest charge may ask for the charge"
    assert not agrees("rank", False, [["ENA", 150.0]], 149.0)
    assert not agrees("rank", False, [["ENA", 150.0]], ["Duke", 150.0]), "a name beside the figure must be the name"
    assert agrees("rank", False, [["Receipt Nominations Madden West", 9.0]], "Madden West", peer=True)
    assert not agrees("rank", False, [["Receipt Nominations Madden West", 9.0]], "Topock", peer=True)
    assert agrees("show", True, [["945"], ["1000"]], ["0945", "1000"]), "a list of numbers compares as numbers"
    assert agrees("rank", False, [["ENA", 150.0], ["Duke", 150.0]], "Duke"), "either of a tie"
    assert agrees("show", True, [["a"], ["b"]], ["B", "a"])
    assert not agrees("show", True, [["a"], ["b"]], ["a"])
    assert not agrees("agg", False, [], 1)
    assert as_day("12/6/2000\xa0") == "2000-12-06"
    assert not same_scalar("12", "ENA")


def test_parse_author() -> None:
    parsed = parse_author('ROLE gas scheduler\nCOL t0.2 biz:volume 8 val biz:commodity 7\nCOL t0.1 region_trading 9\nQ1|east vol|agg sum 2 @t0 where 1="East"|east>V1;vol>2\nR1|who approved|not held\nnoise\n')
    assert parsed["role"] == "gas scheduler" and len(parsed["columns"]) == 2 and parsed["columns"][0]["val"] == "biz:commodity"
    assert parsed["questions"][0]["program"] == 'agg sum 2 @t0 where 1="East"'
    assert parsed["refusals"][0]["question"] == "who approved"
    assert parsed["junk"] == 1


def test_noise_is_stable_and_near_its_share() -> None:
    changed = 0
    for at in range(2000):
        typed, change = messy(f"q{at}", "total volume delivered to the pipeline")
        assert (typed, change) == messy(f"q{at}", "total volume delivered to the pipeline")
        if change:
            changed += 1
            assert typed != "total volume delivered to the pipeline" and len(typed.split()) == 6
    assert abs(changed / 2000 - SHARE) < 0.03


def test_links_and_rule() -> None:
    assert rule_reaches("cpty", "Counterparty") is False, "an abbreviation with no prefix is the reader's to learn"
    assert rule_reaches("counter", "Counterparty")
    assert rule_reaches("vol", "Volume")
    assert rule_reaches("hh", "Henry Hub")
    q = {"program": 'agg sum 2 @t0 where 1="East"', "question": "east burn total", "links": "east>V1;burn>2;total>C;missing>3;burn>9", "coordinates": ["measure:t0.2", "filter:t0.1", "value:t0.1=east"], "overlap": []}
    columns = {"t0": {0: [0, "Counterparty", "text", "key"], 1: [1, "Region", "text", "label"], 2: [2, "Volume", "number", "measure"]}}
    labels = {("t0", "2"): {"coordinates": ["biz:volume"], "value_coordinates": []}, ("t0", "1"): {"coordinates": ["biz:region_trading"], "value_coordinates": ["biz:region_trading"]}}
    links = link_records(q, {}, columns, labels)
    assert [(link["kind"], link.get("position")) for link in links] == [("value", 1), ("column", 2), ("context", None)], "a word not in the question or a column the table lacks is dropped"
    assert links[0]["target"] == "East" and links[0]["own_words"] is True and links[0]["role"] == "filter"
    assert links[1]["coordinates"] == ["biz:volume"] and links[1]["role"] == "measure"
    unlinked = {"program": 'agg sum 2 @t0 where 1="East"', "question": "total volume east", "links": "", "coordinates": ["measure:t0.2", "filter:t0.1"], "overlap": ["volume"], "used": [{"table": "t0", "position": 2, "letter": "C", "name": "Volume"}, {"table": "t0", "position": 1, "letter": "B", "name": "Region"}]}
    by_rule = link_records(unlinked, {}, columns, labels)
    assert {(link["kind"], link["words"], link["source"]) for link in by_rule} == {("column", "volume", "rule"), ("value", "east", "rule")}, "what a question says outright links by rule"
    assert is_subject_only({"program": "agg sum 2 @t0", "overlap": []}, [links[2]])
    assert not is_subject_only(q, links)
    assert not is_subject_only({"program": "agg sum 2 @t0", "overlap": ["volume"]}, [links[2]])


def test_quotas_hold_by_script_and_keep_what_they_set_apart() -> None:
    from collections import Counter
    rows = [{"id": f"a{i}", "family": "f", "label": "gold", "no_header_word": i < 4, "short": True, "words": 4} for i in range(12)] + [{"id": "r", "family": "f", "label": "refusal", "no_header_word": False, "short": True, "words": 4}]
    counts: Counter = Counter()
    kept, over = apply_quotas(rows, counts)
    gold = [r for r in kept if r["label"] == "gold"]
    assert sum(r["no_header_word"] for r in gold) * 2 >= len(gold) and counts["drop_quota_header"] == 4
    assert any(r["label"] == "refusal" for r in kept) and len(gold) + len(over) == 12
    rows = [{"id": f"b{i}", "family": "g", "label": "gold", "no_header_word": True, "short": i < 11, "words": 4 if i < 11 else 9} for i in range(12)]
    kept, over = apply_quotas(rows, Counter())
    assert sum(r["short"] for r in kept) <= 0.75 * len(kept) + 1 and len(kept) + len(over) == 12


def test_overlap() -> None:
    assert overlap_of("total volume east", {"volume", "price"}) == ["volume"], "a shared word is the word"
    assert overlap_of("total volumes east", {"volume", "price"}) == [], "no language's plural is cut: a near form is no shared word"
    assert overlap_of("vol east", {"volume"}) == [], "an abbreviation does not share it"


def test_named_coordinates() -> None:
    schema = {"price", "Organization"}
    assert named("biz:counterparty", schema) == "biz:counterparty"
    assert named("counterparty", schema) == "biz:counterparty"
    assert named("schema:price", schema) == "schema:price"
    assert named("price", schema) == "schema:price"
    assert named("schema:nosuch", schema) is None and named("biz:nosuch", schema) is None and named("none", schema) is None


def test_plan_carries_rare_forms_where_they_fit() -> None:
    features = {"tables": 1, "columns": 12, "lines": 40, "peers": 1, "bands": 1, "printed": 1, "dates": 1, "numbers": 5, "texts": 3, "notes": 0, "families": 0}
    slots = plan_slots(features, "book", "tab")
    assert plan_slots(features, "book", "tab") == slots
    assert 5 <= len(slots) <= 14
    assert not any("note" in text for _, text in slots), "no shape a tab cannot carry"
    plain = {**features, "peers": 0, "bands": 0, "printed": 0, "dates": 0, "numbers": 0}
    assert all("agg sum" not in text and "calc" not in text for _, text in plan_slots(plain, "book", "tab"))


def test_sandbox_runs_and_refuses() -> None:
    with tempfile.TemporaryDirectory() as folder:
        grids = os.path.join(folder, "g.json.gz")
        with gzip.open(grids, "wt", encoding="utf-8") as sink:
            json.dump([{"name": "A", "rows": [["x", 1], ["y\xa0 z"]]}], sink)
        programs = os.path.join(folder, "p.json")
        with open(programs, "w", encoding="utf-8") as sink:
            json.dump({"tab": "A", "programs": {"0": "sum(num(r[1]) or 0 for r in T)", "1": "answer=s(T[1][0])", "2": "import os", "3": "open('x')", "4": "T[0].__class__", "5": "1/0", "6": "len(T[1])"}}, sink)
        done = subprocess.run([sys.executable, "-I", "-X", "utf8", os.path.join(HERE, "..", "training", "factory", "sandbox_run.py"), grids, programs], capture_output=True, text=True, encoding="utf-8", cwd=folder, check=True)
        out = json.loads(done.stdout)
    assert out["0"] == {"answer": 1.0}
    assert out["1"] == {"answer": "y z"}
    assert "refused" in out["2"]["error"] and "refused" in out["3"]["error"] and "refused" in out["4"]["error"]
    assert "ZeroDivisionError" in out["5"]["error"]
    assert out["6"] == {"answer": 2}, "every row as wide as its tab"


def test_name_set_is_whole() -> None:
    from engine.config import DATA_DIR
    with open(os.path.join(DATA_DIR, "named_coordinates.json"), encoding="utf-8") as source:
        document = json.load(source)
    structure = document["tiers"]["structure"]["names"]
    names = {row["name"] for row in structure}
    assert 90 <= len(structure) <= 110
    assert all(f"form:{form}" in names for form in FORMS)
    assert all(f"answer_type:{kind}" in names for kind in ANSWER_TYPES)
    assert all(row["definition"] for row in structure)
    vocabulary = document["tiers"]["meaning"]["vocabulary"]["names"]
    assert {row["name"] for row in vocabulary} == {f"biz:{name}" for _, name, _ in PACK}
    assert all(row["definition"] for row in vocabulary)
    assert document["tiers"]["meaning"]["schema_org"]["properties"] == 1521 and document["tiers"]["meaning"]["schema_org"]["classes"] == 926
    assert any(row["use"].startswith("NOT USED") for row in document["licenses"])


def main() -> int:
    tests = [(name, item) for name, item in globals().items() if name.startswith("test_") and callable(item)]
    failed = 0
    for name, item in tests:
        try:
            item()
        except Exception as exc:  # noqa: BLE001 - every test reports
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed} of {len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
