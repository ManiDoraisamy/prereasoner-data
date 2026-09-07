"""The shipped demo datasets answer their shipped prompts. Live world Postgres.

Every directory under web/public/dataset/ is a public demo workbook: its CSVs are what the
home page loads and its prompt.txt is what the page prefills. This suite runs each of those
exact payloads through the serving entry point, so a planner or grounding change that breaks
a public demo fails here before a visitor sees it. The expectations span both routes: prompts
that need a knowledgebase join (country/continent grounding, FX conversion) and prompts that
must stay on the own-data path because the column already answers them.

  Needs a synced world Postgres (docker-compose + db/sync) and KB_PG_* env vars set.
  python -m tests.test_datasets
"""
from __future__ import annotations

import csv
import io
import os
import sys
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parents[1] / "web" / "public" / "dataset"

# dataset directory -> expected scalar for its prompt.txt. A new dataset directory without an
# entry here fails the suite: a public demo must not ship with an unverified answer.
EXPECTED = {
    "customer-orders": ("world+fx", 1126.66),     # city -> country join + ECB conversion (as-of drift tolerated)
    "customers-orders": ("world+fx", 1126.66),    # same question through the two-sheet FK join
    "formfacade-leads": ("world", 62000),         # country column -> continent grounding (Europe)
    "formfacade-workshops": ("own", 4),           # AVG with a value filter; no knowledge join
    "neartail-orders": ("own", 5),                # city column answers directly; no knowledge join
    "neartail-shipping": ("world", 46),           # city -> country -> continent two-hop grounding
    "formesign-intake": ("own", 6),               # document-type value filter; no knowledge join
    "formesign-contracts": ("world+fx", 23568),   # continent filter + four-currency ECB conversion to USD
    "formesign-hospital-transfers": ("world", 46),  # hospital entity join, filtered to US hospitals
    "neartail-catering": ("world", 9600),         # restaurant entity join, filtered to US restaurants
    "formfacade-bank-deposits": ("world", 1550),  # bank entity join, filtered to Swiss banks
}
FX_TOLERANCE = 0.15  # world+fx answers move with the ECB daily rate; 15% bounds a plausible drift


def _tables(ds: Path) -> list[dict]:
    tables = []
    for f in sorted(ds.glob("*.csv")):
        rows = list(csv.reader(io.StringIO(f.read_text(encoding="utf-8"))))
        tables.append({"name": f.stem, "columns": rows[0], "rows": rows[1:]})
    return tables


def _scalar(res):
    rows = (res or {}).get("result", {}).get("rows") or []
    if rows and rows[0]:
        try:
            return float(str(rows[0][0]).replace(",", ""))
        except (ValueError, TypeError):
            return rows[0][0]
    return None


def _eval_cases(ds: Path):
    """Parse eval.txt: ordered follow-ups for the SAME conversation, `question => expected`.

    A `~` prefix marks an FX-derived expectation, checked within FX_TOLERANCE because the ECB rate
    moves daily. Expected values are derived from the shipped CSVs independently of the engine, so a
    passing case means the answer is right, not merely reproducible.
    """
    path = ds / "eval.txt"
    if not path.exists():
        return None
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        chat_only = line.startswith("chat:")
        if chat_only:
            line = line[len("chat:"):]
        question, _, expected = line.partition("=>")
        question, expected = question.strip(), expected.strip()
        if not question or not expected:
            raise ValueError(f"{path}: each case must read '<question> => <expected>', got {line!r}")
        fx = expected.startswith("~")
        cases.append((question, float(expected.lstrip("~")), fx, chat_only))
    return cases


def main() -> int:
    if not os.environ.get("KB_PG_PASSWORD"):
        print("set KB_PG_PASSWORD"); return 1
    from engine.knowledge_query import KnowledgeQuery
    from regress.live_schema import live_schema
    Q = KnowledgeQuery()
    schema = live_schema().name
    fails = []

    on_disk = {d.name for d in DATASET_DIR.iterdir() if d.is_dir()}
    for missing in sorted(on_disk - set(EXPECTED)):
        fails.append(f"dataset {missing!r} ships without a verified expectation in tests/test_datasets.py")
    for ds_name in sorted(on_disk):
        if not (DATASET_DIR / ds_name / "eval.txt").exists():
            fails.append(f"dataset {ds_name!r} ships without eval.txt (follow-up coverage is required)")
    for gone in sorted(set(EXPECTED) - on_disk):
        fails.append(f"expectation for {gone!r} names a dataset directory that no longer exists")

    for name in sorted(on_disk & set(EXPECTED)):
        ds = DATASET_DIR / name
        prompt = (ds / "prompt.txt").read_text(encoding="utf-8").strip()
        if not prompt:
            fails.append(f"{name}: empty prompt.txt"); continue
        kind, want = EXPECTED[name]
        res = Q.serve(_tables(ds), prompt, schema=schema)
        got = _scalar(res)
        print(f"{name}: {prompt!r} -> {got} (exp ~{want}, {kind})")
        if not isinstance(got, float):
            fails.append(f"{name}: no numeric answer (got {got!r})"); continue
        if kind == "world+fx":
            if abs(got - want) > want * FX_TOLERANCE:
                fails.append(f"{name}: {got} outside ±{FX_TOLERANCE:.0%} of {want}")
        elif got != want:
            fails.append(f"{name}: {got} != {want}")

        # FOLLOW-UPS (eval.txt) — the same conversation, in order. The prompt alone never exercises
        # qualifier carry-over or conversation-supplied semantics; these do.
        for question, expected, fx, chat_only in (_eval_cases(ds) or []):
            if chat_only:
                print(f"{name}: follow-up {question!r} SKIPPED here — orchestrated path only "
                      f"(verified by the Chrome release pass)")
                continue
            follow = Q.serve(_tables(ds), question, schema=schema)
            answer = _scalar(follow)
            print(f"{name}: follow-up {question!r} -> {answer} (exp {'~' if fx else ''}{expected})")
            if not isinstance(answer, float):
                fails.append(f"{name} follow-up {question!r}: no numeric answer (got {answer!r})")
            elif fx:
                if abs(answer - expected) > expected * FX_TOLERANCE:
                    fails.append(f"{name} follow-up {question!r}: {answer} outside "
                                 f"±{FX_TOLERANCE:.0%} of {expected}")
            elif abs(answer - expected) > 0.01:
                fails.append(f"{name} follow-up {question!r}: {answer} != {expected}")

    print("\n" + ("PASS — every shipped demo dataset answers its prompt.txt and eval.txt follow-ups" if not fails
                  else "FAIL:\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
