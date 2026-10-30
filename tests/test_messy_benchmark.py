"""The messy-question benchmark's own contract: seeded workbooks, gold computed from them, a question that does not repeat a
column's name, and the grader's three verdicts. Hermetic: the benchmark itself runs by regress.run_messy."""
import re

from regress.messy_cases import STUDIED, Case, cases, grade
from regress.messy_workbooks import BUILDERS, workbook


def test_workbooks_are_seeded():
    for name, build in BUILDERS.items():
        assert build() == workbook(name), name


def test_every_answer_is_computed_and_non_empty():
    every = cases()
    assert len(every) >= 190, len(every)
    for case in every:
        kind, gold = case.expected
        assert kind in ("scalar", "groups", "first", "rows", "money"), case.question
        if kind == "scalar":
            golds = gold if isinstance(gold, list) else [gold]
            assert any(value for value in golds), case.question
        else:
            assert gold, case.question
    assert len({(case.workbook, case.question) for case in every}) == len(every)


def test_a_question_on_meaning_does_not_say_the_column():
    """A "column" or "value" question that spelled out the header it leans on would test nothing."""
    def words(text):
        return set(re.findall(r"[a-z]+", text.lower()))

    for case in cases():
        if case.family != "column":
            continue
        for table in workbook(case.workbook):
            for column in table["columns"]:
                header = words(column)
                if len(header) > 1:
                    assert not header <= words(case.question), (case.question, column)


def test_split_keeps_studied_workbooks_out_of_frozen():
    every = cases()
    assert {case.split for case in every} == {"dev", "frozen"}
    assert all(case.split == "dev" for case in every if case.workbook in STUDIED)
    frozen = [case for case in every if case.split == "frozen"]
    assert 50 <= len(frozen) <= 110, len(frozen)
    assert Case("orders", "total sales", ("scalar", 1), "plain").split == Case("deals", "total sales", ("scalar", 2), "plain").split


def test_grade():
    def answer(*rows):
        return {"result": {"rows": [list(row) for row in rows]}}

    assert grade(("scalar", 10), answer([10]))[0] == "right"
    assert grade(("scalar", [3, 10]), answer(["$10.00"]))[0] == "right"
    assert grade(("scalar", 10), answer([11]))[0] == "wrong"
    assert grade(("scalar", 2), answer(["a"], ["b"]))[0] == "right", "the rows themselves answer a count"
    assert grade(("scalar", 3), answer(["a"], ["b"]))[0] == "wrong"
    assert grade(("scalar", 10), answer([10], [10]))[0] == "wrong"
    assert grade(("scalar", 10), {"clarify": True, "reason": "which one?"})[0] == "asked"
    assert grade(("scalar", 10), {"error": "boom"})[0] == "asked"
    assert grade(("groups", {"a": 1, "b": 2}), answer(["a", 1], ["b", 2], ["c", 3]))[0] == "right"
    assert grade(("groups", {"a": 1, "b": 2}), answer(["a", 1]))[0] == "wrong"
    assert grade(("first", ["x"]), answer(["x", 9], ["y", 1]))[0] == "right"
    assert grade(("first", ["x"]), answer(["y", 9], ["x", 1]))[0] == "wrong"
    assert grade(("rows", 2), answer(["x"], ["y"]))[0] == "right"
    assert grade(("money", {"usd": 5, "inr": 80}), answer(["usd", 5], ["inr", 80]))[0] == "right"
    assert grade(("money", {"usd": 5, "inr": 80}), answer([85]))[0] == "wrong"
    assert grade(("money", {"usd": 5}), answer([5]))[0] == "right"


TESTS = [test_workbooks_are_seeded, test_every_answer_is_computed_and_non_empty, test_a_question_on_meaning_does_not_say_the_column,
         test_split_keeps_studied_workbooks_out_of_frozen, test_grade]


def main():
    failed = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"messy benchmark: {len(TESTS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
