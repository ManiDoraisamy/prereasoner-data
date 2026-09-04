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

    print("\n" + ("PASS — every shipped demo dataset answers its shipped prompt" if not fails
                  else "FAIL:\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
