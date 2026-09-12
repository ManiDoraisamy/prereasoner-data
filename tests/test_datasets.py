"""The shipped datasets answer their shipped prompts. Live world Postgres.

Each directory under web/public/dataset/ is either a customer-facing example listed in
dataset.txt or a release-only evaluation dataset listed in eval.txt. This suite runs each
selected payload through the serving entry point, so a planner or grounding change that breaks
an example or release gate fails before launch. The expectations span both routes: prompts that
need a knowledgebase join (country/continent grounding, FX conversion) and prompts that must
stay on the own-data path because the column already answers them.

  Needs a synced world Postgres (docker-compose + db/sync) and KB_PG_* env vars set.
  python -m tests.test_datasets
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import subprocess
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parents[1] / "web" / "public" / "dataset"
EXAMPLE_MANIFEST = DATASET_DIR / "dataset.txt"
EVAL_MANIFEST = DATASET_DIR / "eval.txt"


def _manifest_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.split(":", 1)[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


# dataset directory -> expected scalar for its prompt.txt. A new dataset directory without an
# entry here fails the suite: a public demo must not ship with an unverified answer.
EXPECTED = {
    "customer-orders": (
        "world+fx",
        1126.66,
    ),  # city -> country join + ECB conversion (as-of drift tolerated)
    "complex-promotions": (
        "own-rows",
        {
            "columns": ["customer_name", "product_name"],
            "rows": [["Cara", "Beta"], ["Cara", "Alpha"], ["Bob", "Gamma"]],
        },
    ),
    "complex-category-gaps": (
        "own-rows",
        {
            "columns": ["customer_name", "category"],
            "rows": [["Ava", "Travel"]],
        },
    ),
    "complex-unsold-products": (
        "own-rows",
        {
            "columns": ["product_name"],
            "rows": [["Delta"], ["Omega"]],
        },
    ),
    "complex-promotions-xlsx": (
        "own-rows",
        {
            "columns": ["customer_name", "product_name"],
            "rows": [["Cara", "Beta"], ["Cara", "Alpha"], ["Bob", "Gamma"]],
        },
    ),
    "neartail-orders-xlsx": ("own", 5),  # the same orders table, shipped as a real workbook
    "orders-tiers": (
        "world+fx",
        1126.66,
    ),  # separate orders + tier joined discount fixture
    "customers-orders": (
        "world+fx",
        1126.66,
    ),  # same question through the two-sheet FK join
    "formfacade-leads": (
        "world",
        62000,
    ),  # country column -> continent grounding (Europe)
    "formfacade-workshops": ("own", 4),  # AVG with a value filter; no knowledge join
    "neartail-orders": ("own", 5),  # city column answers directly; no knowledge join
    "neartail-shipping": (
        "world",
        46,
    ),  # city -> country -> continent two-hop grounding
    "formesign-intake": ("own", 6),  # document-type value filter; no knowledge join
    "formesign-contracts": (
        "world+fx",
        23568,
    ),  # continent filter + four-currency ECB conversion to USD
    "formesign-hospital-transfers": (
        "world",
        46,
    ),  # hospital entity join, filtered to US hospitals
    "neartail-catering": (
        "world",
        9600,
    ),  # restaurant entity join, filtered to US restaurants
    "formfacade-bank-deposits": (
        "world",
        1550,
    ),  # bank entity join, filtered to Swiss banks
    "payment-commissions": (
        "own",
        1082.41,
    ),  # joined instrument rate, subtracted row by row
    "eval-formfacade-bank-marketing": ("own", 4521),
    "eval-neartail-ecommerce": ("own", 502),
    "eval-formesign-termination-xlsx": ("own", 56.30434782608695),
    "eval-neartail-supplier-report-xlsx": ("own", 2128324.96),
    "eval-formesign-assets-xls": ("own", 1509.36),
    "eval-formesign-procurement-xlsx": ("own", 1168),
}
FX_TOLERANCE = (
    0.15  # world+fx answers move with the ECB daily rate; 15% bounds a plausible drift
)

# Conversational models sometimes add a harmless copula to a standalone prompt. These are still
# serving-contract cases: the typed planner must answer them instead of treating the connector as a
# dropped user constraint.
REWRITE_EXPECTATIONS = {
    "formesign-intake": [("How many Intake Consent documents are there?", 6)],
}


def _xlsx_rows(path: Path) -> list[list[str]]:
    """Read the one-sheet XLSX fixtures used by the release gate.

    The browser converts workbook datasets to CSV text through the upload worker before the
    engine ever sees them, so this reader exists ONLY so the live suite can feed the same
    tables through the production entry point without adding a spreadsheet dependency.
    """
    import xml.etree.ElementTree as ET
    import zipfile

    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as z:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.iterfind(".//m:t", ns))
                for item in root.findall("m:si", ns)
            ]
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for row in root.findall(".//m:sheetData/m:row", ns):
        values = []
        for cell in row.findall("m:c", ns):
            node = (cell.find("m:is/m:t", ns) if cell.get("t") == "inlineStr"
                    else cell.find("m:v", ns))
            value = "" if node is None or node.text is None else node.text
            if cell.get("t") == "s" and value:
                value = shared_strings[int(value)]
            values.append(value)
        rows.append(values)
    return rows


def _tables(ds: Path) -> list[dict]:
    tables = []
    for f in sorted(ds.glob("*.csv")):
        rows = list(csv.reader(io.StringIO(f.read_text(encoding="utf-8"))))
        tables.append({"name": f.stem, "columns": rows[0], "rows": rows[1:]})
    workbooks = sorted([*ds.glob("*.xlsx"), *ds.glob("*.xls")])
    if workbooks:
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("workbook_fixture.js")), *map(str, workbooks)],
            capture_output=True, text=True, encoding="utf-8", timeout=60, check=True,
        )
        for workbook in json.loads(result.stdout):
            for sheet in workbook["sheets"]:
                rows = list(csv.reader(io.StringIO(sheet["csv"])))
                name = Path(workbook["file"]).stem if len(workbook["sheets"]) == 1 else sheet["name"]
                tables.append({"name": name, "columns": rows[0], "rows": rows[1:]})
    return tables


def _scalar(res):
    rows = ((res or {}).get("result") or {}).get(
        "rows"
    ) or []  # a clarify carries result: None
    if rows and rows[0]:
        try:
            return float(str(rows[0][0]).replace(",", ""))
        except (ValueError, TypeError):
            return rows[0][0]
    return None


def _eval_cases(ds: Path):
    """Parse eval.txt: ordered follow-ups, `question => expected`.

    A `~` prefix marks an FX-derived expectation, checked within FX_TOLERANCE because the ECB rate
    moves daily. Expected values are derived from the shipped CSVs independently of the engine, so a
    passing case means the answer is right, not merely reproducible.

    The direct engine gate sends non-`chat:` cases as individual calls with the same tables. It
    deliberately does not pretend to carry conversational history. ``chat:`` cases are shorthand
    cases and are verified by the orchestrator/browser release path instead.

    The literal `clarify` is the one non-numeric expectation: the question is well formed but matches
    no rows, and the engine must say so rather than present a blank as the answer. It is expressed
    here because a wrong BLANK is exactly what a numeric-only gate cannot catch.
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
            line = line[len("chat:") :]
        question, _, expected = line.partition("=>")
        question, expected = question.strip(), expected.strip()
        if not question or not expected:
            raise ValueError(
                f"{path}: each case must read '<question> => <expected>', got {line!r}"
            )
        if expected.lower() == "clarify":
            cases.append((question, None, False, chat_only))
            continue
        if expected.startswith("[") or expected.startswith("{"):
            cases.append((question, json.loads(expected), False, chat_only))
            continue
        fx = expected.startswith("~")
        cases.append((question, float(expected.lstrip("~")), fx, chat_only))
    return cases


def main() -> int:
    if not os.environ.get("KB_PG_PASSWORD"):
        print("set KB_PG_PASSWORD")
        return 1
    from engine import request_timing
    from engine.deterministic.context import (
        analysis_execution_context,
        enforce_execution_response,
    )
    from engine.knowledge import KnowledgeReasoner
    from regress.live_schema import live_schema

    Q = KnowledgeReasoner()
    schema = live_schema().name
    fails = []
    records = []
    modes = [
        mode.strip()
        for mode in os.environ.get(
            "EVAL_EXECUTION_MODES", "sql,python,verify,default"
        ).split(",")
        if mode.strip()
    ]
    unknown_modes = set(modes) - {"sql", "python", "verify", "auto", "default"}
    if unknown_modes:
        raise ValueError(f"unknown EVAL_EXECUTION_MODES: {sorted(unknown_modes)}")

    def serve(tables, question, *, schema, decomposition=None):
        responses = []
        for mode in modes:
            requested_mode = None if mode == "default" else mode
            token = request_timing.begin(f"dataset-{name}-{mode}")
            try:
                with analysis_execution_context(
                    None, schema, execution_mode=requested_mode
                ):
                    try:
                        response = enforce_execution_response(
                            Q.serve(
                                tables,
                                question,
                                schema,
                                decomposition=decomposition,
                            ), requested_mode
                        )
                    except Exception as exc:  # noqa: BLE001 — the matrix records backend failures
                        response = {"error": f"{type(exc).__name__}: {exc}"}
                record = {
                    "dataset": name,
                    "question": question,
                    "mode": mode,
                    "answer": _scalar(response),
                    "clarify": bool(response.get("clarify")),
                    "error": response.get("error"),
                    "execution": response.get("execution"),
                    "timings": request_timing.snapshot(),
                    "manifest": response.get("deterministic", {}).get("manifest"),
                }
                records.append(record)
                print(
                    json.dumps(
                        {
                            key: value
                            for key, value in record.items()
                            if key != "manifest"
                        },
                        default=str,
                    ),
                    flush=True,
                )
                if response.get("error"):
                    fails.append(f"{name} [{mode}] {question!r}: {response['error']}")
                elif response.get("result") is not None:
                    expected_mode = "python" if mode in {"auto", "default"} else mode
                    actual_mode = (response.get("execution") or {}).get("actual")
                    if actual_mode != expected_mode:
                        fails.append(
                            f"{name} [{mode}] {question!r}: expected backend "
                            f"{expected_mode!r}, got {actual_mode!r}"
                        )
                responses.append(response)
            finally:
                request_timing.end(token)
        if any(
            response.get("result") != responses[0].get("result")
            or bool(response.get("clarify")) != bool(responses[0].get("clarify"))
            for response in responses[1:]
        ):
            fails.append(f"{name}: separate mode requests disagree for {question!r}")
        return responses[0]

    on_disk = {d.name for d in DATASET_DIR.iterdir() if d.is_dir()}
    example_names = _manifest_names(EXAMPLE_MANIFEST)
    eval_names = _manifest_names(EVAL_MANIFEST)
    if not EXAMPLE_MANIFEST.exists():
        fails.append("missing customer-facing dataset.txt manifest")
    if not EVAL_MANIFEST.exists():
        fails.append("missing evaluation-only eval.txt manifest")
    if example_names & eval_names:
        fails.append(
            f"dataset names appear in both manifests: {sorted(example_names & eval_names)}"
        )
    manifest_names = example_names | eval_names
    for missing in sorted(manifest_names - on_disk):
        fails.append(f"manifest names missing dataset directory: {missing!r}")
    for unlisted in sorted(on_disk - manifest_names):
        fails.append(f"dataset directory is absent from both manifests: {unlisted!r}")
    for missing in sorted(on_disk - set(EXPECTED)):
        fails.append(
            f"dataset {missing!r} ships without a verified expectation in tests/test_datasets.py"
        )
    for ds_name in sorted(on_disk):
        if not (DATASET_DIR / ds_name / "eval.txt").exists():
            fails.append(
                f"dataset {ds_name!r} ships without eval.txt (follow-up coverage is required)"
            )
    for gone in sorted(set(EXPECTED) - on_disk):
        fails.append(
            f"expectation for {gone!r} names a dataset directory that no longer exists"
        )

    for name in sorted(on_disk & set(EXPECTED)):
        if os.environ.get("EVAL_DATASETS") and name not in os.environ[
            "EVAL_DATASETS"
        ].split(","):
            continue
        ds = DATASET_DIR / name
        prompt = (ds / "prompt.txt").read_text(encoding="utf-8").strip()
        if not prompt:
            fails.append(f"{name}: empty prompt.txt")
            continue
        kind, want = EXPECTED[name]
        decomposition_path = ds / "decomposition.json"
        decomposition = (
            json.loads(decomposition_path.read_text(encoding="utf-8"))
            if decomposition_path.exists()
            else None
        )
        res = serve(
            _tables(ds), prompt, schema=schema, decomposition=decomposition
        )
        got = _scalar(res)
        print(f"{name}: {prompt!r} -> {got} (exp ~{want}, {kind})")
        if kind == "own-rows":
            result = (res or {}).get("result") or {}
            columns = list(result.get("columns") or ())
            try:
                indexes = [columns.index(column) for column in want["columns"]]
                actual_rows = [
                    [row[index] for index in indexes]
                    for row in (result.get("rows") or ())
                ]
            except (ValueError, IndexError):
                actual_rows = None
            if actual_rows != want["rows"]:
                fails.append(
                    f"{name}: expected rows {want['rows']!r}, got {actual_rows!r}"
                )
        elif not isinstance(got, float):
            fails.append(f"{name}: no numeric answer (got {got!r})")
        elif kind == "world+fx":
            if abs(got - want) > want * FX_TOLERANCE:
                fails.append(f"{name}: {got} outside ±{FX_TOLERANCE:.0%} of {want}")
        elif got != want:
            fails.append(f"{name}: {got} != {want}")

        for question, expected in REWRITE_EXPECTATIONS.get(name, []):
            rewritten = serve(_tables(ds), question, schema=schema)
            rewritten_value = _scalar(rewritten)
            print(f"{name}: rewrite {question!r} -> {rewritten_value} (exp {expected})")
            if rewritten.get("clarify") or rewritten_value != expected:
                fails.append(
                    f"{name} rewrite {question!r}: expected {expected}, got {rewritten!r}"
                )

        # FOLLOW-UPS (eval.txt) — direct engine cases in file order. Conversational shorthand is
        # explicitly skipped here and belongs to the orchestrator/browser release path below.
        for question, expected, fx, chat_only in _eval_cases(ds) or []:
            if chat_only:
                print(
                    f"{name}: follow-up {question!r} SKIPPED here — orchestrated path only "
                    f"(verified by the Chrome release pass)"
                )
                continue
            follow = serve(_tables(ds), question, schema=schema)
            answer = _scalar(follow)
            if expected is None:
                clarified = bool((follow or {}).get("clarify"))
                print(
                    f"{name}: follow-up {question!r} -> clarify={clarified} answer={answer!r} (exp clarify)"
                )
                if not clarified:
                    fails.append(
                        f"{name} follow-up {question!r}: matched no rows but was presented "
                        f"as the answer {answer!r} instead of a clarify"
                    )
                continue
            print(
                f"{name}: follow-up {question!r} -> {answer} (exp {'~' if fx else ''}{expected})"
            )
            if not isinstance(answer, float):
                fails.append(
                    f"{name} follow-up {question!r}: no numeric answer (got {answer!r})"
                )
            elif fx:
                if abs(answer - expected) > expected * FX_TOLERANCE:
                    fails.append(
                        f"{name} follow-up {question!r}: {answer} outside "
                        f"±{FX_TOLERANCE:.0%} of {expected}"
                    )
            elif abs(answer - expected) > 0.01:
                fails.append(f"{name} follow-up {question!r}: {answer} != {expected}")

    if os.environ.get("EVAL_REPORT"):
        Path(os.environ["EVAL_REPORT"]).write_text(
            json.dumps({"records": records, "failures": fails}, indent=2, default=str),
            encoding="utf-8",
        )
    print(
        "\n"
        + (
            "PASS — selected dataset prompts and standalone eval cases (chat cases excluded)"
            if not fails
            else "FAIL:\n  " + "\n  ".join(fails)
        )
    )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
