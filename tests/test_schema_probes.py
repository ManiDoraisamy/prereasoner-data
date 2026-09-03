"""OUT-OF-DISTRIBUTION generalization gate for the Schema.org class interpreter.

Every probe here is a table the corpus never contains VERBATIM: renamed tables, dropped auxiliary columns,
consumer shapes with short proper names. The interpreter must decode by SEMANTIC shape, not by memorized
surface (a corpus that teaches one presentation per relation passes heldout metrics at P=1.0 and still fails
all of these — that is exactly the failure this gate exists to catch).

Positives must decode; consumer tables must ABSTAIN (an upload of customers/orders is none of the servable
source classes). Loads the trained head + Qwen (no Postgres): registered in the model tier of run_all.

python -m tests.test_schema_probes
"""
from __future__ import annotations
import sys

# The tests' canonical customers shape — SHORT first names (Ada/Bo/Sam are real city names and read as
# code-like tokens): the adversarial consumer shape that false-fired DefinedTerm before the demo negatives.
CUSTOMERS = {"name": "customers", "columns": ["name", "city", "remarks"], "rows": [
    ["Ada", "Paris", "package arrived late and damaged, terrible delivery"],
    ["Lin", "Lyon", "great product, very happy with the quality"],
    ["Bo", "Berlin", "shipping was slow and the box was crushed"],
    ["Sam", "Nice", "excellent service, fast and smooth"],
    ["Mai", "Tokyo", "the courier lost my parcel, awful logistics"],
    ["Eve", "Munich", "love it, would buy again"]]}

ORDERS = {"name": "orders", "columns": ["order ID", "customer", "ordered", "currency", "amount"], "rows": [
    [101, "Sherlock Holmes", "Magnifying Glass, Brass", "GBP", 118],
    [109, "Inspector Clouseau", "Gabardine Trench Coat", "EUR", 310],
    [111, "Chuck Bartowski", "Nerd Herd Tie", "USD", 25],
    [119, "Byomkesh Bakshi", "Case Notebook", "INR", 40]]}

# ECB rows presented the way a REAL upload looks: renamed table, no 'base currency' context column.
FX = {"name": "exchange rates", "columns": ["effective date", "quote currency", "units per eur"], "rows": [
    ["2026-08-14", "USD", 1.1641], ["2026-08-14", "GBP", 0.8598],
    ["2026-08-14", "JPY", 171.23], ["2026-08-13", "USD", 1.1633],
    ["2026-08-13", "CHF", 0.9421], ["2026-08-12", "SEK", 11.203]]}

# ICD-10-CM rows renamed, KEEPING the coding-system column: schema.org MedicalCode is defined by
# codeValue + codingSystem, so this table carries the evidence the class requires.
DIAGNOSES = {"name": "diagnosis codes", "columns": ["coding system", "code", "description"], "rows": [
    ["ICD-10-CM", "A00", "Cholera"],
    ["ICD-10-CM", "A00.0", "Cholera due to Vibrio cholerae 01, biovar cholerae"],
    ["ICD-10-CM", "A01", "Typhoid and paratyphoid fevers"],
    ["ICD-10-CM", "B20", "Human immunodeficiency virus disease"],
    ["ICD-10-CM", "E11", "Type 2 diabetes mellitus"], ["ICD-10-CM", "J45", "Asthma"]]}

# THE CONTRASTIVE PARTNER, and the sharper half of the test: the same codes with the coding system
# REMOVED. A bare code column does not establish an ICD-10-CM MedicalCode — it could be any code set —
# so the honest outcome is abstention.
#
# This pair is here because the earlier version of this suite asserted that the code-only table decodes
# to MedicalCode, and it PASSED — on a head trained against a corpus that labeled codingSystem/inCodeSet
# on 363 instances where "ICD-10-CM" appeared nowhere in the text. The probe was passing *because of* that
# defect. With the evidence invariant enforced, codeValue still fires on the code column (0.993) while
# codingSystem correctly does not (0.001), and the class abstains. Requiring BOTH directions is what makes
# this a test of evidence rather than of memorised surface.
DIAGNOSES_NO_SYSTEM = {"name": "diagnosis codes", "columns": ["code", "description"],
                       "rows": [row[1:] for row in DIAGNOSES["rows"]]}

# CLASS-level expectations are asserted only for classes the promoted artifact actually marks servable — a
# probe that demands a decode from a calibration_failed class tests the gate, not the model, and can never
# pass. Which classes are servable is read from the artifact at runtime rather than hard-coded.
MUST_DECODE = [
    (FX, "ExchangeRateSpecification"),
    (DIAGNOSES, "MedicalCode"),
]
MUST_ABSTAIN = [CUSTOMERS, ORDERS]

# PROPERTY-level expectations, which is where the generalization claim actually lives and which hold
# regardless of whether a class cleared its serving gate. Each entry: (table, must-fire, must-not-fire).
# The MedicalCode pair is the sharp one — the same codes with and without their coding system.
PROPERTY_PROBES = [
    (DIAGNOSES, ("codeValue",), ()),
    (DIAGNOSES_NO_SYSTEM, ("codeValue",), ("codingSystem", "inCodeSet")),
    (FX, ("currentExchangeRate", "priceCurrency", "price"), ()),
    (CUSTOMERS, (), ("termCode", "codeValue", "codingSystem")),
]

# DISCRIMINATION probes: (property, table that HAS its evidence, table that does NOT, minimum separation).
# This is the sharper test of generalization, and it is deliberately separate from the firing assertions.
# Whether a score clears a calibrated threshold conflates two things — does the model READ the evidence, and
# is the threshold placed to admit this particular out-of-distribution score. Only the first is a property
# of the model. Measured here: codingSystem scores 0.884 on the table that carries "coding system:
# ICD-10-CM" and 0.004 on the same codes without it — a 0.88 separation, i.e. the column is read correctly.
# Class calibration is tested separately from this property-level discrimination: a property can remain
# below its independent assertion threshold while its continuous probability contributes to a class score.
DISCRIMINATION_PROBES = [
    ("codingSystem", DIAGNOSES, DIAGNOSES_NO_SYSTEM, 0.50),
    ("inCodeSet", DIAGNOSES, DIAGNOSES_NO_SYSTEM, 0.50),
]


_DET_SCRIPT = (
    "import os, sys\n"
    "sys.path.insert(0, os.environ['PR_ROOT'])\n"
    "import engine.config, json\n"
    "from engine.schema_model import SchemaInterpreter\n"
    "from tests.test_schema_probes import FX\n"
    "r = SchemaInterpreter().interpret_table(FX)\n"
    "print('DET::' + json.dumps([(c['name'], c['score']) for c in r['classes']], sort_keys=True))\n"
)


def _determinism(fails):
    """A served explanation must be REPRODUCIBLE, not sampled: two fresh interpreters, same table, identical
    classes and scores. Same bar the typed-AST planner is held to in tests/test_routing.py."""
    import os
    import subprocess
    import sys
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env = {**os.environ, "PR_ROOT": root, "PYTHONHASHSEED": "0",
           "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}   # same pinning as test_routing.py
    outs = []
    for _ in range(2):
        proc = subprocess.run([sys.executable, "-c", _DET_SCRIPT], capture_output=True, text=True,
                              env=env, timeout=900)
        outs.append(next((line[5:] for line in proc.stdout.splitlines()
                          if line.startswith("DET::")), f"NONE ({proc.stderr[-200:]})"))
    if not outs[0].startswith("[") or outs[0] != outs[1]:
        fails.append(f"class decode is not cross-process deterministic:\n  run1={outs[0]}\n  run2={outs[1]}")
    else:
        print(f"  determinism    -> identical across processes: {outs[0]}")


def main():
    try:
        from engine.schema_model import SchemaInterpreter, summarize_table
        interp = SchemaInterpreter()
    except (FileNotFoundError, ImportError) as e:
        # The ONLY legitimate skip: the head is external (gitignored but manifest-pinned), so source-only CI
        # has no artifact until weights are provisioned. Every OTHER exception is deliberate — SchemaInterpreter raises
        # on an ontology or corpus mismatch and ClassDecoder raises on an uncalibrated artifact — and a bare
        # `except: return 0` turned each of those loud integrity failures into a green generalization gate.
        print(f"SKIP: schema head not present ({type(e).__name__}: {e})"); return 0
    fails = []
    servable = {row["name"] for row in interp.decoder.classes.values() if row.get("servable")}
    print(f"  servable classes in the promoted artifact: {sorted(servable)}\n")

    # --- PROPERTY layer: where the generalization claim lives, independent of class serving gates ---
    for table, must_fire, must_not_fire in PROPERTY_PROBES:
        rep = interp.interpret_table(table)
        fired = {p["name"] for p in rep["properties"] if p["fired"]}
        print(f"  {table['name']:16s} fired: {sorted(fired)}")
        for name in must_fire:
            if name not in fired:
                score = next((p["score"] for p in rep["properties"] if p["name"] == name), None)
                fails.append(f"{table['name']}: {name} must fire (score {score})")
        for name in must_not_fire:
            if name in fired:
                fails.append(f"{table['name']}: {name} must NOT fire — its evidence is absent from the text")

    # --- DISCRIMINATION: does the model READ the evidence, independent of threshold placement? ---
    for name, has_it, lacks_it, floor in DISCRIMINATION_PROBES:
        uri = f"https://schema.org/{name}"
        with_ev = interp.profile_text(summarize_table(has_it)).get(uri, 0.0)
        without = interp.profile_text(summarize_table(lacks_it)).get(uri, 0.0)
        threshold = interp.thresholds.get(uri, 0.5)
        margin = "fires" if with_ev >= threshold else f"under threshold {threshold:.3f} by {threshold - with_ev:.3f}"
        print(f"  {name:16s} {with_ev:.4f} with evidence vs {without:.4f} without "
              f"(separation {with_ev - without:.4f}; {margin})")
        if with_ev - without < floor:
            fails.append(f"{name}: separation {with_ev - without:.4f} < {floor} — the model is not reading "
                         f"the column, it is guessing from context")

    # --- CLASS layer: asserted only for classes the artifact actually marks servable ---
    print()
    for table, expected in MUST_DECODE:
        rep = interp.interpret_table(table)
        got = [c["name"] for c in rep["classes"]]
        print(f"  {table['name']:16s} -> {got or 'ABSTAIN'}")
        if expected not in servable:
            print(f"      (skipped: {expected} is not servable in this artifact — property layer covers it)")
            continue
        if expected not in got:
            fired = [p["name"] for p in rep["properties"] if p["fired"]]
            fails.append(f"{table['name']}: expected {expected}, got {got or 'ABSTAIN'} (fired props: {fired})")
    for table in MUST_ABSTAIN:
        rep = interp.interpret_table(table)
        got = [c["name"] for c in rep["classes"]]
        print(f"  {table['name']:16s} -> {got or 'ABSTAIN'}")
        if got:
            fails.append(f"{table['name']}: a consumer upload must abstain, decoded {got}")
    _determinism(fails)
    print("\n" + ("PASS — the interpreter generalizes by shape, abstains on consumer tables" if not fails
                  else "FAIL:\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
