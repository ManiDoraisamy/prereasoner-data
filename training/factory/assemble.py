"""The last stage: what the two routes agree on is gold, what they disagree on is "ambiguous, should ask" with both readings, what the author marked unanswerable and the second route also asked about is a refusal. Then the noise by script, the quotas per family by script, and each question's links with the named coordinates of what they point at.

usage: python -m training.factory.assemble --side train|val [--source enron]
Output: FACTORY/out/<side>.jsonl, FACTORY/out/<side>.labels.jsonl, FACTORY/out/<side>.counts.json. Prints counts, never a cell or a question.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from collections import Counter, defaultdict

from training.factory.agree import agrees
from training.factory.factory import final_tab, load_tabs, read_journal, safe_name, stage_dir, tab_key
from training.factory.noise import messy
from training.factory.paths import OUT, pin
from training.factory.prompts import PROMPT_VERSION
from training.factory.split import mailbox
from training.factory.vocabulary import PACK

THRESHOLD = 6                  # a label's 1-9 at or above this counts as named
PACK_NAMES = frozenset(name for _, name, _ in PACK)
SHORT_WORDS = 6
_REF_TOKEN = re.compile(r"[a-z0-9]+")
ROLE_OF = {"measure": "measure", "key": "key", "shown": "key", "peers": "key", "filter": "filter", "value": "filter", "contains": "filter", "line": "filter", "section": "filter", "period": "filter", "number": "filter", "blank": "filter", "table": "context"}


def schema_names() -> set[str]:
    from engine.schema_org import schema_name  # noqa: F401 - the contract's reader owns the file's place
    from engine.config import DATA_DIR
    with open(os.path.join(DATA_DIR, "schema_org_v30.json"), encoding="utf-8") as source:
        contract = json.load(source)
    return {uri.rsplit("/", 1)[-1] for uri in (*contract["property_order"], *contract["class_order"])}


def named(name: str | None, schema: set[str]) -> str | None:
    """A label's name as a coordinate of the name set, or None if the set does not hold it."""
    if not name or name.lower() == "none":
        return None
    text = name.strip().strip(",;")
    if text.startswith("biz:"):
        text = text[4:]
        return f"biz:{text}" if text in PACK_NAMES else None
    if text.startswith("schema:"):
        text = text[7:]
        return f"schema:{text}" if text in schema else None
    if text in PACK_NAMES:
        return f"biz:{text}"
    if text in schema:
        return f"schema:{text}"
    return None


def words_in(text: str) -> list[str]:
    return _REF_TOKEN.findall(text.casefold())


def rule_reaches(words: str, target: str) -> bool:
    """Tier (c): the sheet's own words reach this link by rule: a whole word, a prefix either way at 3+ letters, or an initialism."""
    asked, have = words_in(words), words_in(target)
    if not asked or not have:
        return False
    for a in asked:
        for h in have:
            if a == h or (len(a) >= 3 and h.startswith(a)) or (len(h) >= 3 and a.startswith(h)):
                return True
    initials = "".join(h[0] for h in have)
    return any(len(a) >= 2 and a == initials for a in asked)


def _first_table(program: str) -> str:
    match = re.search(r"@([tfn]\d+)", program)
    return match[1] if match else ""


def value_of(program: str, table: str, position: str) -> str | None:
    """The text a program's condition on this column holds, read from its text."""
    match = re.search(rf"@{table}\b.*?\b{position}\s*[=~^$]\s*\"((?:[^\"]|\"\")*)\"", program) or re.search(rf"\b{position}\s*[=~^$]\s*\"((?:[^\"]|\"\")*)\"", program)
    return match[1].replace('""', '"') if match else None


def band_or_line(program: str, word: str) -> str | None:
    match = re.search(rf"{word}\s*[=~]\s*\"((?:[^\"]|\"\")*)\"", program)
    return match[1].replace('""', '"') if match else None


def link_records(q: dict, tab: dict, columns: dict, labels: dict) -> list[dict]:
    """Parse the author's links against the question and program. A link whose words are not in the question, or whose reference names nothing the program reads, is dropped and counted."""
    program, default = q["program"], _first_table(q["program"])
    slots = defaultdict(set)
    for coordinate in q["coordinates"]:
        slot, _, rest = coordinate.partition(":")
        slots[rest.split("=")[0]].add(slot)
    tokens = words_in(q["question"])
    out = []
    for part in (q.get("links") or "").split(";"):
        if ">" not in part:
            continue
        words, _, ref = part.partition(">")
        words, ref = words.strip(), ref.strip()
        asked = words_in(words)
        if not asked or not all(word in tokens for word in asked):
            continue
        span = [at for at, token in enumerate(tokens) if token in asked]
        table = default
        for one in ref.split(","):
            one = one.strip().lstrip("c") if re.fullmatch(r"c\d+", one.strip()) else one.strip()
            if match := re.match(r"^([tfn]\d+):(.*)$", one):
                table, one = match[1], match[2]
            record = {"words": words, "span": span, "ref": one, "table": table}
            if one == "C":
                record["kind"] = "context"
                record["role"] = "context"
            elif one in ("L", "B"):
                record["kind"] = "line" if one == "L" else "band"
                record["role"] = "filter"
                record["target"] = band_or_line(program, "line" if one == "L" else "band")
            elif match := re.fullmatch(r"([VPN]?)(\d+)", one):
                head, position = match[1], match[2]
                column = columns.get(table, {}).get(int(position))
                if column is None:
                    continue
                record["position"] = int(position)
                record["column"] = column[1]
                record["kind"] = {"": "column", "V": "value", "P": "period", "N": "number"}[head]
                name = f"{table}.{position}"
                if head == "":
                    roles = {ROLE_OF[slot] for slot in slots.get(name, ()) if slot in ROLE_OF}
                    record["role"] = sorted(roles)[0] if roles else "filter"
                else:
                    record["role"] = "filter"
                    if head == "V":
                        record["target"] = value_of(program, table, position)
                label = labels.get((table, str(position)))
                if label:
                    record["coordinates"] = label["coordinates"]
                    if head == "V" and label.get("value_coordinates"):
                        record["coordinates"] = label["value_coordinates"]
            elif match := re.fullmatch(r"A(\d+)", one):
                record["kind"] = "axis"
                record["role"] = "key"
                label = labels.get((table, f"a{match[1]}"))
                if label:
                    record["coordinates"] = label["coordinates"]
            else:
                continue
            target = record.get("target") or record.get("column") or ""
            record["own_words"] = rule_reaches(words, target) if target else False
            out.append(record)
    return out


def is_subject_only(q: dict, links: list[dict]) -> bool:
    """A question naming the sheet's subject only: its program has no condition, it shares no word with a used header, and no word of it links to a value, line, band, period or number."""
    return " where " not in f" {q['program']} " and not q["overlap"] and not {link["kind"] for link in links} & {"value", "line", "band", "period", "number"}


def agreement_kind(q: dict, answer: object) -> str:
    """How two routes agreed. A peer rank agreed on the column's name only, and an audit put those at 5 of 11 with their figure, 9 of 11 on the name; a rank's top figure alone at 23 of 25 (design/phase3_factory.txt). The reader may weigh these apart."""
    if "entity:peer" in q["coordinates"]:
        return "peer_name_only"
    if q["form"] == "rank" and not isinstance(answer, (str, list, dict)):
        return "figure_only"
    return "exact"


def assemble_tab(tab: dict, route: dict, schema: set[str], record: dict, side: str, source: str, teacher: str, commit: str, counts: Counter) -> tuple[list[dict], dict]:
    """(rows, labels) of one tab."""
    labels = {}
    for label in tab["labels"]:
        coordinate = named(label["name"], schema) if label["conf"] >= THRESHOLD else None
        value = named(label["val"], schema) if label.get("val") and (label.get("val_conf") or 0) >= THRESHOLD else None
        counts["label_lines"] += 1
        counts["label_unknown_name"] += 1 if label["name"].lower() != "none" and named(label["name"], schema) is None else 0
        labels[(label["table"], label["at"])] = {"coordinates": [coordinate] if coordinate else [], "value_coordinates": [value] if value else ([coordinate] if coordinate else []), "raw": label["name"], "conf": label["conf"]}
    columns = {table: {row[0]: row for row in rows} for table, rows in record["columns"].items()}
    rows = []
    base = {"source": source, "side": side, "family": tab["family"], "mailbox": mailbox(tab["file"]) if source == "enron" else source, "file": tab["file"], "book": tab["book"], "tab": tab["tab"], "role": tab["role"], "teacher": teacher, "prompt_version": PROMPT_VERSION, "engine_commit": commit}
    for at, q in enumerate(tab["questions"]):
        counts["questions_ran"] += 1
        result = route["results"].get(str(at))
        ask = route["asks"].get(str(at))
        qid = f"{tab['book']}.{safe_name(tab['tab'])[:6]}.{q['slot']}"
        links = link_records(q, tab, columns, labels)
        typed, change = messy(qid, q["question"])
        row = dict(base, id=qid, question=typed, written=q["question"], first_written=q.get("first_written"), noise=change, form=q["form"], program=q["program"], used=q.get("used"), coordinates=q["coordinates"], answer_type=q["answer_type"], answer=q["rows"], links=links,
                   words=len(words_in(typed)), short=len(words_in(typed)) <= SHORT_WORDS, no_header_word=not q["overlap"], subject_only=is_subject_only(q, links), header_words=q["header_words"], route_program=route["programs"].get(str(at)))
        if ask is not None:
            row.update(label="ambiguous", second_reading={"asks": ask})
            counts["ambiguous_route_asked"] += 1
        elif result is None or "error" in result:
            counts["drop_route_failed"] += 1
            continue
        elif agrees(q["form"], bool(q.get("listing")), q["rows"], result.get("answer"), peer="entity:peer" in q["coordinates"]):
            row.update(label="gold", second_reading={"answer": result.get("answer")}, agreement=agreement_kind(q, result.get("answer")))
            counts["gold"] += 1
        else:
            row.update(label="ambiguous", second_reading={"answer": result.get("answer")})
            counts["ambiguous_disagree"] += 1
        rows.append(row)
    for at, refusal in enumerate(tab["refusals"], len(tab["questions"])):
        counts["refusals_written"] += 1
        ask = route["asks"].get(str(at))
        result = route["results"].get(str(at))
        if ask is None:
            counts["drop_refusal_answered" if result and "error" not in result else "drop_refusal_route_failed"] += 1
            continue
        qid = f"{tab['book']}.{safe_name(tab['tab'])[:6]}.r{at}"
        rows.append(dict(base, id=qid, question=refusal["question"], written=refusal["question"], label="refusal", why=refusal["why"], second_reading={"asks": ask}, words=len(words_in(refusal["question"])), short=len(words_in(refusal["question"])) <= SHORT_WORDS))
        counts["refusal"] += 1
    return rows, {"key": tab["key"], "family": tab["family"], "labels": {f"{t}.{a}": v for (t, a), v in labels.items()}, "columns": record["columns"]}


def apply_quotas(rows: list[dict], counts: Counter) -> tuple[list[dict], list[dict]]:
    """Per family, on gold: at least half share no word with a used column's header; at most 75% and at least 60% at most six words, where the family has enough to choose from. Returns (kept, over quota): what is over is set apart and kept on disk, since it is paid for and true. Ambiguous and refusal rows pass; they are not gold."""
    by_family: dict[str, list[dict]] = defaultdict(list)
    out = []
    over: list[dict] = []
    for row in rows:
        if row["label"] == "gold":
            by_family[row["family"]].append(row)
        else:
            out.append(row)
    for family in sorted(by_family):
        members = sorted(by_family[family], key=lambda r: r["id"])
        clean = [r for r in members if r["no_header_word"]]
        dirty = [r for r in members if not r["no_header_word"]]
        allowed = len(clean)
        if len(dirty) > allowed:
            # Keep the shortest of those over the line, so the length quota is not fought twice.
            dirty.sort(key=lambda r: (r["words"], r["id"]))
            counts["drop_quota_header"] += len(dirty) - allowed
            over.extend(dirty[allowed:])
            dirty = dirty[:allowed]
        kept = clean + dirty
        if len(kept) >= 8:
            short = [r for r in kept if r["short"]]
            long_rows = [r for r in kept if not r["short"]]
            if len(short) > 0.75 * len(kept) and long_rows:
                keep_short = int(0.75 / 0.25 * len(long_rows))
                if keep_short < len(short):
                    counts["drop_quota_short"] += len(short) - keep_short
                    short.sort(key=lambda r: r["id"])
                    over.extend(short[keep_short:])
                    kept = long_rows + short[:keep_short]
            elif len(short) < 0.6 * len(kept) and short:
                keep_long = int(0.4 / 0.6 * len(short))
                if keep_long < len(long_rows):
                    counts["drop_quota_long"] += len(long_rows) - keep_long
                    long_rows.sort(key=lambda r: r["id"])
                    over.extend(long_rows[keep_long:])
                    kept = short + long_rows[:keep_long]
        out.extend(kept)
    return out, over


def shares(rows: list[dict]) -> dict:
    gold = [r for r in rows if r["label"] == "gold"]
    n = len(gold) or 1
    forms = Counter(r["form"] for r in gold)
    words = Counter()
    for r in gold:
        for name in r["coordinates"]:
            slot, _, rest = name.partition(":")
            if slot in ("entity", "aggregate", "size", "row_op", "calc_op", "comparison", "blank", "period", "section", "line", "contains"):
                words[slot if slot in ("period", "section", "line", "contains", "blank", "comparison") else f"{slot}:{rest.split('=')[0]}"] += 1
    families = defaultdict(list)
    for r in gold:
        families[r["family"]].append(r)
    big = [m for m in families.values() if len(m) >= 8]
    within = lambda test: round(sum(1 for m in big if test(m)) / max(1, len(big)), 3)
    return {"gold": len(gold), "agreement": dict(Counter(r.get("agreement") for r in gold)), "short_le6": round(sum(r["short"] for r in gold) / n, 3), "no_header_word": round(sum(r["no_header_word"] for r in gold) / n, 3), "noise": round(sum(1 for r in gold if r["noise"]) / n, 3), "subject_only": round(sum(r["subject_only"] for r in gold) / n, 3),
            "forms": dict(forms), "program_parts": dict(words), "gold_families": len(families), "families_ge8": len(big), "share_of_big_families_within": {"short_60_75": within(lambda m: 0.6 <= sum(r["short"] for r in m) / len(m) <= 0.75), "no_header_ge50": within(lambda m: sum(r["no_header_word"] for r in m) / len(m) >= 0.5), "subject_ge15": within(lambda m: sum(r["subject_only"] for r in m) / len(m) >= 0.15), "noise_10_20": within(lambda m: 0.1 <= sum(1 for r in m if r["noise"]) / len(m) <= 0.2)}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--side", required=True)
    parser.add_argument("--source", default="enron")
    args = parser.parse_args()
    commit = pin()
    side, source = args.side, args.source
    os.makedirs(OUT, exist_ok=True)
    schema = schema_names()
    records = {tab_key(r): r for r in load_tabs(side)}
    rewrites = read_journal(f"rewrite_{side}")
    teacher = "gemini-3.8-flash"
    counts: Counter = Counter()
    rows, labels = [], []
    route_dir = stage_dir(side, "route")
    for path in sorted(glob.glob(stage_dir(side, "cand") + "*.json")):
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as handle:
            tab = final_tab(json.load(handle), rewrites)
        if not os.path.exists(route_dir + name):
            counts["tabs_without_route"] += 1
            labels.append(None)
            continue
        with open(route_dir + name, encoding="utf-8") as handle:
            route = json.load(handle)
        tab_rows, tab_labels = assemble_tab(tab, route, schema, records[tab["key"]], side, source, teacher, commit[:7], counts)
        rows.extend(tab_rows)
        labels.append(tab_labels)
        counts["tabs"] += 1
    before = sum(1 for r in rows if r["label"] == "gold")
    kept, over = apply_quotas(rows, counts)
    counts["gold_before_quota"] = before
    with open(f"{OUT}{side}.jsonl", "w", encoding="utf-8", newline="\n") as sink:
        for row in sorted(kept, key=lambda r: r["id"]):
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(f"{OUT}{side}.over_quota.jsonl", "w", encoding="utf-8", newline="\n") as sink:
        for row in sorted(over, key=lambda r: r["id"]):
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(f"{OUT}{side}.labels.jsonl", "w", encoding="utf-8", newline="\n") as sink:
        for row in labels:
            if row is not None:
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {"counts": dict(counts), "labels_kept": dict(Counter(r["label"] for r in kept)), "shares": shares(kept)}
    with open(f"{OUT}{side}.counts.json", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(summary, sink, indent=1, sort_keys=True)
    print(json.dumps(summary, indent=0, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
