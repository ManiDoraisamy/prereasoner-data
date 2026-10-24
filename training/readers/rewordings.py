"""Rewordings of training questions into the families the readers misread, each keeping its original's gold readings
(as Spider-Syn's rewordings do): no word list in serving, only more ways of asking in the training data.

Families, chosen from the gold readings (never from the question's words):
  measure    the readings aggregate a quantity (SUM, AVG, MAX or MIN of a column) or rank or filter groups by its total
             or average; the rewording names that quantity by the everyday word a person would use for it instead of
             the column's own words ("the total line_total" -> "the total spend").
  exclusion  the readings exclude the rows another query finds (EXCEPT, NOT IN); the rewording says it as negated
             existence ("that no ... has", "that never ...").
  opposite   the readings ask for one extreme (MIN or MAX of a column, or the first rows of a sort); the rewording asks
             for the opposite extreme, and its readings are the original's with MIN and MAX and the sort direction
             swapped (``opposite_readings``). The training data asks for the highest four times as often as the lowest
             (1,745 descending top-1 sorts against 387 ascending), and the readers read "minimum notice_days" and
             "lowest amount" as the highest (2026-10-22). Gemini also names the phrase it replaced and its
             replacement; a rewording is kept only if the original holds the first and the rewording the second.
  sheet      the readings count the rows of one table (COUNT(*) or COUNT of a column, at most filtered); the rewording
             asks it as a person looking at the spreadsheet would: the table as a sheet, its rows below the header,
             a column's non-empty cells. "Count all non-empty Order ID rows below the header in the Customers sheet"
             was read as "order id is 'non-empty'" by every reader, which counted 0 of 4 orders (2026-10-23).

Gemini (engine/llm.py, the product's one client; temperature 0, seed 0) writes each rewording. One is kept only if
every value its original question states appears in it verbatim, it differs from the original, and it has at most
MAX_WORDS words. Questions on the split's calibration databases are never reworded; the Spider test set is not read.

Outputs: <out dir>/rewordings.jsonl (source "reworded": the question, its family and its original) and
<out dir>/readings_reworded.jsonl (the original's readings under the rewording's source and index), for
training.readers.examples --reworded.

usage: python -m training.readers.rewordings <split dir> <readings.jsonl ...> <out dir>
           [--bird <BIRD train dir>] [--workers 8] [--limit N]
"""
import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from engine.readers import numerals
from training.readers.sources import Sources, add_arguments

MAX_WORDS = 45
SYSTEM = ("You reword questions asked of a database, to train a model that reads questions. Keep the meaning exactly: "
          "the same rows, the same numbers, every value, name, date and condition word for word. Change only what "
          "the instruction asks.")
INSTRUCTIONS = {
    "measure": ("The question asks about the quantity stored in {columns}. Reword it so it names that quantity by the "
                "everyday word a person would use for it (for example in a report or a chat), and never by the "
                "column's own words. If no everyday word fits that quantity better than its column name, set skip."),
    "exclusion": ("The question asks for what is excluded by {columns}. Reword it so it says the exclusion as a person "
                  "would, as something that does not exist or never happened (that no ... has, that never ..., "
                  "without any ...). If it cannot be said that way and keep its meaning, set skip."),
    "opposite": ("The question asks for an extreme of {columns} (the largest or the smallest, the most or the fewest, "
                 "the latest or the earliest, and so on). Reword it so it asks for the opposite extreme and nothing "
                 "else changes. Give the phrase you replaced (replaced) and what replaced it (replacement). If the "
                 "extreme has no single opposite, set skip."),
    "sheet": ("The table {columns} is a sheet of a spreadsheet: a header row naming its columns, one record per row "
              "below it. Reword the question as a person looking at that sheet would ask it, naming the sheet, its "
              "rows below the header, or the cells of a column as the header names it (non-empty or filled cells "
              "when it counts a column's values), and keep every condition and value. If it cannot be said that "
              "way, set skip."),
}
SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}, "skip": {"type": "boolean"},
                                           "replaced": {"type": "string"}, "replacement": {"type": "string"}},
          "required": ["question", "skip"]}
FAMILIES = tuple(INSTRUCTIONS)


def family_columns(readings):
    """{family: the columns that make the question one of it}, from the gold readings alone."""
    measure, exclusion = [], []
    if readings["structure"] == "EXCEPT":
        exclusion.extend(column for column, _, _ in readings["blocks"][1]["where"])
        exclusion.extend(column for _, _, column, _ in readings["blocks"][1]["select"])
    for block in readings["blocks"]:
        measure.extend(column for _, aggregate, column, _ in block["select"]
                       if aggregate in ("SUM", "AVG", "MAX", "MIN") and column != "*")
        measure.extend(column for aggregate, column in block["order"] if aggregate in ("SUM", "AVG"))
        measure.extend(column for aggregate, column, _, _ in block["having"] if aggregate in ("SUM", "AVG"))
        exclusion.extend(column for column, operator, _ in block["where"] if operator == "NOT IN")
    found = {}
    if readings["structure"] == "single":
        block = readings["blocks"][0]
        counted = [column for _, aggregate, column, _ in block["select"] if aggregate == "COUNT"]
        if (len(block["tables"]) == 1 and len(block["select"]) == 1 and counted and not block["group"]
                and not block["having"] and not block["order"]):
            found["sheet"] = block["tables"]
        extremes = {aggregate for _, aggregate, _, _ in block["select"]} & {"MIN", "MAX"}
        if len(extremes) == 1:
            found["opposite"] = sorted({c for _, a, c, _ in block["select"] if a in extremes})
        elif not extremes and block["order"] and block["dir"] and block["limit"]:
            found["opposite"] = sorted({column for _, column in block["order"]})
    if measure:
        found["measure"] = sorted(set(measure))
    if exclusion:
        found["exclusion"] = sorted(set(exclusion))
    return found


def opposite_readings(readings):
    """``readings`` asking for the opposite extreme: MIN and MAX swapped, a limited sort's direction reversed."""
    swap = {"MIN": "MAX", "MAX": "MIN"}
    flipped = json.loads(json.dumps(readings))
    for block in flipped["blocks"]:
        block["select"] = [[s, swap.get(a, a), c, d] for s, a, c, d in block["select"]]
        block["having"] = [[swap.get(a, a), c, o, v] for a, c, o, v in block["having"]]
        if block["order"] and block["dir"] and block["limit"]:
            block["dir"] = "ASC" if block["dir"] == "DESC" else "DESC"
    return flipped


def swapped(original, reworded, reply):
    """Whether Gemini's named swap holds: the original says the replaced phrase, the rewording its replacement."""
    replaced, replacement = (reply.get("replaced") or "").strip().lower(), (reply.get("replacement") or "").strip().lower()
    return bool(replaced and replacement and replaced != replacement and replaced in original.lower()
                and replacement in reworded.lower() and replaced not in reworded.lower())


def stated_values(readings, question):
    """The literal values of the readings that the question states (text verbatim, numbers as written)."""
    values = []
    for block in readings["blocks"]:
        literals = [value for _, _, value in block["where"]] + [value for _, _, _, value in block["having"]]
        for value in literals:
            if isinstance(value, str):
                text = value.strip("%")
                if text and text.lower() in question.lower():
                    values.append(text)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                values.extend(question[start:end] for number, start, end in numerals.mentions(question)
                              if float(number) == float(value))
    return values


def keeps(original, reworded, values):
    """Whether ``reworded`` may take ``original``'s readings."""
    if not reworded or reworded.strip().lower() == original.strip().lower():
        return False
    if len(reworded.split()) > MAX_WORDS:
        return False
    return all(value.lower() in reworded.lower() for value in values)


def _prompt(question, schema_text, family, columns):
    return (f"Database tables and columns:\n{schema_text}\n\nQuestion: {question}\n\n"
            + INSTRUCTIONS[family].format(columns=", ".join(columns)))


def _schema_text(table_entry):
    tables = table_entry["table_names_original"]
    columns = {}
    for table, column in table_entry["column_names_original"]:
        if table >= 0:
            columns.setdefault(tables[table], []).append(column)
    return "\n".join(f"- {table}: {', '.join(columns.get(table, []))}" for table in tables)


def reword(job):
    from engine import llm

    family, columns, question, schema_text = job["family"], job["columns"], job["question"], job["schema"]
    error = None
    for _ in range(2):                             # one retry: Vertex drops a call now and then
        try:
            reply = json.loads(llm.generate_text(system=SYSTEM, prompt=_prompt(question, schema_text, family, columns),
                                                 max_output_tokens=400, json_schema=SCHEMA, timeout_seconds=60.0,
                                                 thinking="LOW"))
        except Exception as exc:                   # noqa: BLE001 - a failed call drops one rewording
            error = type(exc).__name__
            continue
        reworded = None if reply.get("skip") else reply.get("question", "").strip()
        if reworded and family == "opposite" and not swapped(question, reworded, reply):
            return {**job, "reworded": None}
        return {**job, "reworded": reworded}
    return {**job, "error": error}


def main():
    ap = argparse.ArgumentParser(description="Reworded training questions that keep their gold readings.")
    ap.add_argument("split_dir", help="the folder of the split's manifest.json (calibration_dbs)")
    ap.add_argument("readings", nargs="+", help="gold readings files of training questions")
    ap.add_argument("out_dir")
    ap.add_argument("--bird", help="BIRD's train directory, for bird_train questions")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, help="reword at most this many questions (a trial)")
    ap.add_argument("--families", default=",".join(FAMILIES), help="comma-separated families to reword")
    add_arguments(ap)
    args = ap.parse_args()
    calibration = set(json.load(open(os.path.join(args.split_dir, "manifest.json")))["calibration_dbs"])
    sources = Sources.from_args(args)
    if args.bird:
        sources.add_bird(args.bird)
    jobs = []
    for path in args.readings:
        for line in open(path, encoding="utf-8"):
            record = json.loads(line)
            if "readings" not in record or record["db_id"] in calibration:
                continue
            question = sources.questions[record["source"]][record["index"]]["question"]
            for family, columns in family_columns(record["readings"]).items():
                if family not in args.families.split(","):
                    continue
                jobs.append({"family": family, "columns": columns, "question": question,
                             "schema": _schema_text(sources.tables[record["db_id"]]),
                             "origin": [record["source"], record["index"]], "db_id": record["db_id"],
                             "status": record.get("status"), "readings": record["readings"]})
    jobs = jobs[:args.limit] if args.limit else jobs
    os.makedirs(args.out_dir, exist_ok=True)
    kept = skipped = failed = refused = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool, \
            open(os.path.join(args.out_dir, "rewordings.jsonl"), "w", encoding="utf-8", newline="\n") as words, \
            open(os.path.join(args.out_dir, "readings_reworded.jsonl"), "w", encoding="utf-8", newline="\n") as labels, \
            open(os.path.join(args.out_dir, "dropped.jsonl"), "w", encoding="utf-8", newline="\n") as dropped:
        for done in pool.map(reword, jobs):
            if "error" in done:
                failed += 1
            elif done["reworded"] is None:
                skipped += 1
            elif not keeps(done["question"], done["reworded"], stated_values(done["readings"], done["question"])):
                refused += 1
                dropped.write(json.dumps({k: done[k] for k in ("family", "question", "reworded", "origin")}) + "\n")
                continue
            if "error" in done or done["reworded"] is None:
                continue
            index = kept
            kept += 1
            words.write(json.dumps({"source": "reworded", "index": index, "db_id": done["db_id"],
                                    "question": re.sub(r"\s+", " ", done["reworded"]), "family": done["family"],
                                    "original": done["question"], "origin": done["origin"]}) + "\n")
            readings = opposite_readings(done["readings"]) if done["family"] == "opposite" else done["readings"]
            labels.write(json.dumps({"source": "reworded", "index": index, "db_id": done["db_id"],
                                     "status": done["status"], "readings": readings}) + "\n")
    print(f"{len(jobs)} rewordings asked: kept {kept}, skipped by the model {skipped}, refused for a lost value or "
          f"length {refused}, failed calls {failed} -> {args.out_dir}")


if __name__ == "__main__":
    main()
