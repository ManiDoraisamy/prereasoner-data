"""Select up to 4 doubly-checked questions per workbook and write questions.jsonl, manifest.json, verify.html.
Usage: python -I scripts/build_outputs.py   (prints counts only)"""
import hashlib
import html
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import ROOT  # noqa: E402
from sources import records  # noqa: E402

PER_WORKBOOK = 4
PRICE_IN, PRICE_OUT = 1.00, 6.00   # same assumption as gen.py
MODEL = "gemini-3.8-flash"
DATE = "2026-11-10"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean(v):
    if isinstance(v, float):
        r = float(f"{v:.10g}")
        return int(r) if r == int(r) and abs(r) < 1e15 else r
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


def typed_gold(answer_type, v):
    v = clean(v)
    if isinstance(v, list) and len(v) == 1 and answer_type != "list":
        v = v[0]
    if answer_type == "yes_no":
        return "yes" if (v is True or str(v).strip().lower() in ("yes", "true")) else "no"
    if answer_type in ("date", "name"):
        return str(v)
    return v


YES_NO_SHARE = 0.12
_running = {"yes_no": 0, "total": 0}


def select(cands):
    """Greedy, deterministic: prefer new answer types, then new kinds; yes/no capped at one per workbook and at
    a running share of YES_NO_SHARE over the set (yes/no answers are guessable at 50%)."""
    weight = {"number": 3.0, "name": 2.6, "date": 2.5, "list": 2.4, "yes_no": 1.2}
    allow_yes_no = (_running["yes_no"] + 1) <= YES_NO_SHARE * (_running["total"] + PER_WORKBOOK)
    chosen, types, kinds = [], set(), set()
    pool = list(cands)

    def score(q):
        t = q["answer_type"]
        s = (weight.get(t, 1.0) if t not in types else 0.5 if t == "number" else 0.0)
        s += 1.0 if q.get("kind") not in kinds else 0.0
        if t == "yes_no" and ("yes_no" in types or not allow_yes_no):
            s -= 5.0
        return s

    while pool and len(chosen) < PER_WORKBOOK:
        best = max(pool, key=lambda q: (score(q), -pool.index(q)))
        chosen.append(best)
        types.add(best["answer_type"])
        kinds.add(best.get("kind"))
        pool.remove(best)
    _running["total"] += len(chosen)
    _running["yes_no"] += sum(q["answer_type"] == "yes_no" for q in chosen)
    return sorted(chosen, key=lambda q: int(q["qid"].split("#")[1]))


def main():
    downloads = json.load(open(os.path.join(ROOT, "work", "downloads.json"), encoding="utf-8"))
    rows, stats, src_list = [], {"generated": 0, "pass_program_vs_stated": 0, "pass_independent_check": 0,
                                 "kept": 0, "reject_reasons_first": {}, "reject_reasons_second": {}}, []
    for rec in records():
        sid = rec["id"]
        d = downloads[sid]
        g1 = os.path.join(ROOT, "work", "exec1", sid + ".json")
        g2 = os.path.join(ROOT, "work", "exec2", sid + ".json")
        qs = json.load(open(g2 if os.path.exists(g2) else g1, encoding="utf-8"))["questions"] \
            if os.path.exists(g1) else []
        stats["generated"] += len(qs)
        for q in qs:
            r1 = q.get("reject1") or "kept"
            stats["reject_reasons_first"][r1] = stats["reject_reasons_first"].get(r1, 0) + 1
            if q.get("pass1"):
                stats["pass_program_vs_stated"] += 1
                r2 = q.get("reject2") or ("kept" if q.get("pass2") else "not checked")
                stats["reject_reasons_second"][r2] = stats["reject_reasons_second"].get(r2, 0) + 1
        passed = [q for q in qs if q.get("pass2")]
        stats["pass_independent_check"] += len(passed)
        chosen = select(passed)
        for q in chosen:
            rows.append({
                "workbook_file": "workbooks/" + d["file"], "sheet": q.get("sheet") or None,
                "question": q["question"], "answer_type": q["answer_type"],
                "gold": typed_gold(q["answer_type"], q["result"]),
                "program": q["program"], "source_url": rec["url"], "source_page": rec["landing_page"],
                "zip_member": rec["member"], "publisher": rec["publisher"], "domain": rec["domain"],
                "kind": q.get("kind"), "generator_stated_answer": q["answer"],
                "independent_check_value": clean(q.get("check_result")),
                "independent_check_stated_answer": q.get("check_stated"),
                "independent_check_program": q.get("check_program"), "candidate_id": q["qid"]})
        src_list.append({"id": sid, "file": "workbooks/" + d["file"], "domain": rec["domain"],
                         "publisher": rec["publisher"], "url": rec["url"], "zip_member": rec["member"],
                         "landing_page": rec["landing_page"], "license": rec["license"],
                         "license_url": rec["license_url"], "bytes": d["bytes"], "sha256": d["sha256"],
                         "retrieved_utc": d["retrieved_utc"], "questions_kept": len(chosen)})
    for i, r in enumerate(rows):
        r["id"] = f"FF-{i + 1:04d}"
    order = ["id", "workbook_file", "sheet", "question", "answer_type", "gold", "program", "source_url", "domain"]
    rows = [{**{k: r[k] for k in order}, **{k: v for k, v in r.items() if k not in order}} for r in rows]
    stats["kept"] = len(rows)
    qpath = os.path.join(ROOT, "questions.jsonl")
    with open(qpath, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # usage
    usage = {"calls": 0, "failed_calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    by_step = {}
    upath = os.path.join(ROOT, "work", "usage.jsonl")
    for line in open(upath, encoding="utf-8"):
        u = json.loads(line)
        usage["calls"] += 1
        usage["failed_calls"] += 0 if u.get("ok") else 1
        usage["prompt_tokens"] += u.get("prompt", 0)
        usage["output_tokens"] += u.get("output", 0)
        usage["thinking_tokens"] += u.get("thoughts", 0)
        s = by_step.setdefault(u["step"], {"calls": 0, "prompt_tokens": 0, "output_plus_thinking_tokens": 0})
        s["calls"] += 1
        s["prompt_tokens"] += u.get("prompt", 0)
        s["output_plus_thinking_tokens"] += u.get("output", 0) + u.get("thoughts", 0)
    cost = usage["prompt_tokens"] / 1e6 * PRICE_IN + (usage["output_tokens"] + usage["thinking_tokens"]) / 1e6 * PRICE_OUT
    usage.update(by_step=by_step, model=MODEL, client="engine/llm.py generate_text (temperature 0, seed 0)",
                 assumed_price_usd_per_million={"input": PRICE_IN, "output_and_thinking": PRICE_OUT},
                 estimated_cost_usd=round(cost, 2),
                 note="Estimate from usage_metadata token counts at assumed prices above Flash-class list prices; "
                      "not a billing figure.")

    def count(key):
        out = {}
        for r in rows:
            out[r[key]] = out.get(r[key], 0) + 1
        return dict(sorted(out.items()))

    wb_by_domain = {}
    for s in src_list:
        wb_by_domain[s["domain"]] = wb_by_domain.get(s["domain"], 0) + 1
    manifest = {
        "name": "fresh-frozen-20261110",
        "frozen": "run once at the launch gate; never read for development",
        "date": DATE,
        "description": "Held-out questions over real public US government spreadsheets (public domain or open-portal "
                       "terms). Questions and pandas programs written by Gemini from a text rendering of each sheet; "
                       "gold = the program's result, kept only when it matched Gemini's stated answer and a second, "
                       "independent Gemini pass (no first answer shown) computed the same value with its own program.",
        "method": {"candidates_per_workbook": 6, "kept_per_workbook_max": PER_WORKBOOK,
                   "number_tolerance": "0.5% relative", "text": "case-insensitive, whitespace-normalized",
                   "lists": "compared as sets", "sandbox": "python -I subprocess, temp working directory, "
                   "90 s timeout, network blocked, static import allow-list",
                   "selection": "among doubly-checked candidates, greedily prefer new answer types then new kinds"},
        "questions_file": {"path": "questions.jsonl", "sha256": sha256(qpath), "count": len(rows)},
        "counts": {"workbooks": len(src_list), "workbooks_by_domain": wb_by_domain,
                   "workbooks_with_questions": sum(1 for s in src_list if s["questions_kept"]),
                   "questions_by_domain": count("domain"), "questions_by_answer_type": count("answer_type"),
                   "questions_by_kind": count("kind"), "pipeline": stats},
        "gemini_usage": usage,
        "sources": src_list,
        "excluded_sources": "UK OGL / data.gov.uk, EIA, SEC, Enron, Kaggle, DataBench, WikiTableQuestions",
    }
    with open(os.path.join(ROOT, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    write_html(rows)
    print(json.dumps({"kept": len(rows), "by_domain": count("domain"), "by_type": count("answer_type"),
                      "workbooks_by_domain": wb_by_domain, "pipeline": {k: v for k, v in stats.items()},
                      "est_cost": usage["estimated_cost_usd"], "calls": usage["calls"]}, indent=1))


def esc(v):
    return html.escape(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))


def write_html(rows):
    review_path = os.path.join(ROOT, 'review.json')
    review = json.load(open(review_path, encoding='utf-8')) if os.path.exists(review_path) else None
    audit = {r['id']: r for r in review['results']} if review else {}
    if review:
        assert review['questions_sha256'] == sha256(os.path.join(ROOT, 'questions.jsonl'))
        assert set(audit) == {r['id'] for r in rows}
        assert all(audit[r['id']]['answer'] == r['gold'] for r in rows)
    counts = {v: sum(r['verdict'] == v for r in audit.values()) for v in ('Right', 'Wrong', 'Unsure')}
    def comments_html(text):
        import re
        return ''.join('<a href="' + esc(part) + '" rel="noreferrer">source document</a>'
                       if part.startswith(('https://', 'http://')) else esc(part)
                       for part in re.split(r'(https?://\S+)', text))

    cards = []
    table_rows = []
    for r in rows:
        a = audit.get(r['id'], {})
        verdict = a.get('verdict', 'Unreviewed')
        comments = comments_html(a.get('comments', 'No source audit has been saved.'))
        table_rows.append(f"""<tr data-id="{r['id']}" data-domain="{r['domain']}" data-verdict="{verdict.lower()}">
<th scope="row"><a class="question-link" href="#question-{r['id']}" title="{esc(r['question'])}">{r['id']}</a></th>
<td class="answer">{esc(r['gold'])}</td><td><span class="verdict {verdict.lower()}">{verdict}</span></td><td class="comments">{comments}</td></tr>""")
        cards.append(f"""<article class="q" id="question-{r['id']}" data-id="{r['id']}" data-domain="{r['domain']}" data-verdict="{verdict.lower()}">
<header><span class="qid">{r['id']}</span><span class="tag">{r['domain']}</span><span class="tag">{esc(r['answer_type'])}</span><span class="tag">{esc(r.get('kind') or '')}</span></header>
<p class="question">{esc(r['question'])}</p>
<p class="gold"><b>Gold:</b> <code>{esc(r['gold'])}</code></p>
<p class="audit"><b>Verdict:</b> <span class="verdict {verdict.lower()}">{verdict}</span><br><b>Comments:</b> {comments}</p>
<p class="meta">Workbook: <a href="{esc(r['workbook_file'])}">{esc(r['workbook_file'])}</a>{(' &middot; sheet <code>' + esc(r['sheet']) + '</code>') if r['sheet'] else ''}{(' &middot; zip member <code>' + esc(r['zip_member']) + '</code>') if r.get('zip_member') else ''}<br>
Source: <a href="{esc(r['source_url'])}" rel="noreferrer">download</a> &middot; <a href="{esc(r['source_page'])}" rel="noreferrer">page</a> &middot; {esc(r['publisher'])}</p>
<details><summary>Program</summary><pre>{esc(r['program'])}</pre></details>
<details><summary>Independent check</summary><p>Check value: <code>{esc(r.get('independent_check_value'))}</code> &middot; generator stated: <code>{esc(r.get('generator_stated_answer'))}</code></p><pre>{esc(r.get('independent_check_program') or '')}</pre></details>
<fieldset class="mark"><legend>Your follow-up review</legend>
<label><input type="radio" name="m-{r['id']}" value="right"> Right</label>
<label><input type="radio" name="m-{r['id']}" value="wrong"> Wrong</label>
<label><input type="radio" name="m-{r['id']}" value="unsure"> Unsure</label>
<input type="text" class="note" placeholder="note (optional)" aria-label="note for {r['id']}">
</fieldset>
</article>""")
    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Frozen Set Review</title>
<style>
:root{--bg:#fafaf8;--fg:#1d1d1b;--muted:#66645e;--card:#ffffff;--line:#deddd8;--accent:#2f5d8a;--code:#f1f0ec;--right:#2e7d4f;--wrong:#b3261e;--unsure:#856000}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#171716;--fg:#ecebe6;--muted:#a19f97;--card:#21211f;--line:#3a3936;--accent:#8db6e0;--code:#2b2b28;--right:#6cc493;--wrong:#f08a80;--unsure:#e7c76a}}
:root[data-theme="dark"]{--bg:#171716;--fg:#ecebe6;--muted:#a19f97;--card:#21211f;--line:#3a3936;--accent:#8db6e0;--code:#2b2b28;--right:#6cc493;--wrong:#f08a80;--unsure:#e7c76a}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:.2rem 0}
.bar{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:8px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:center;z-index:1}
.bar button,.bar select,.follow-up button{font:inherit;padding:4px 10px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;cursor:pointer}
.bar input{font:inherit;padding:5px 10px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;min-width:150px;flex:1}
.summary{font-weight:600;margin:12px 0 6px;display:flex;gap:8px 24px;flex-wrap:wrap}
.verdict{font-weight:600}.verdict.right{color:var(--right)}.verdict.wrong{color:var(--wrong)}.verdict.unsure{color:var(--unsure)}
.table-wrap{overflow-x:auto;margin:12px 0 24px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;table-layout:fixed;width:100%;min-width:620px;font-size:14px}
caption{text-align:left;padding:10px 12px;color:var(--muted);font-size:13px}
col.id{width:11%}col.answer{width:22%}col.verdict{width:11%}col.comments{width:56%}
th,td{text-align:left;vertical-align:top;padding:10px 12px;border-top:1px solid var(--line);overflow-wrap:anywhere}
thead{background:var(--code)}tbody th{font-weight:500;white-space:nowrap}
.answer{font-variant-numeric:tabular-nums}.comments{line-height:1.5}
tbody tr[data-verdict="wrong"] th{border-left:4px solid var(--wrong)}tbody tr[data-verdict="unsure"] th{border-left:4px solid var(--unsure)}
#question-details>summary{font-weight:600;cursor:pointer;padding:12px 0}
.follow-up{display:flex;flex-wrap:wrap;align-items:center;gap:12px}
.q{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:12px 0;scroll-margin-top:130px}
.q.right{border-left:5px solid var(--right)}.q.wrong{border-left:5px solid var(--wrong)}
.q:target{outline:2px solid var(--accent);outline-offset:2px}
header{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.qid{font-weight:600}.tag{font-size:.8rem;color:var(--muted);border:1px solid var(--line);border-radius:10px;padding:0 7px}
.question{font-size:1.05rem;margin:.5rem 0}
.meta{color:var(--muted);font-size:.88rem;overflow-wrap:anywhere}
a{color:var(--accent)}
code,pre{background:var(--code);border-radius:4px;font:13px/1.45 ui-monospace,Consolas,monospace}
code{padding:1px 4px;overflow-wrap:anywhere}
pre{padding:8px;overflow-x:auto;white-space:pre}
fieldset{border:1px solid var(--line);border-radius:6px;margin-top:8px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.note{flex:1;min-width:160px;font:inherit;padding:3px 6px;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg)}
</style></head><body><main>
<h1>Frozen set review</h1>
<p class="meta">fresh-frozen-20261110: COUNT questions across WORKBOOK_COUNT source files. Each saved verdict reviews the gold answer against its frozen workbook or CSV. This is an answer-key audit; production-system accuracy requires a separate evaluation.</p>
<div class="summary" id="audit-summary"><span>REVIEW_COUNT of COUNT reviewed</span><span class="verdict right">RIGHT_COUNT Right</span><span class="verdict wrong">WRONG_COUNT Wrong</span><span class="verdict unsure">UNSURE_COUNT Unsure</span></div>
<p class="meta">Right: supported by the source. Wrong: invalid for the stated question. Unsure: a tie, missing data, or ambiguity needs resolution. <a href="review.csv" download>Download CSV</a> &middot; <a href="review.json" download>Download full audit JSON</a></p>
<div class="bar">
<select id="filter" aria-label="Filter results"><option value="all">All verdicts</option><option value="attention">Wrong or Unsure</option><option value="right">Right</option><option value="wrong">Wrong</option><option value="unsure">Unsure</option><option value="health">Health</option><option value="other">Other</option><option value="unmarked">Your unmarked</option></select>
<input id="search" type="search" placeholder="Search ID, question, answer, or comments" aria-label="Search results">
<span id="visible-count" class="meta" aria-live="polite"></span></div>
<div class="table-wrap"><table id="answer-table"><caption>Answers and source checks. Select an ID to open its question, workbook link, and calculation programs.</caption>
<colgroup><col class="id"><col class="answer"><col class="verdict"><col class="comments"></colgroup>
<thead><tr><th scope="col">ID</th><th scope="col">Answer</th><th scope="col">Verdict</th><th scope="col">Comments</th></tr></thead><tbody>TABLE_ROWS</tbody></table></div>
<p id="no-results" hidden>No results match this filter.</p>
<details id="question-details"><summary>Question details and your follow-up marks</summary>
<p class="meta">The saved audit appears above. Your follow-up marks stay in this browser and export separately; they do not change the saved audit. This set is frozen: run once at the launch gate, never read for development.</p>
<div class="follow-up"><span id="progress"></span><button id="export" type="button">Export your marks</button><a href="#answer-table">Back to answer table</a></div>
CARDS
</details>
</main>
<script>
(function(){
  var KEY = "fresh-frozen-20261110-marks";
  var marks = {};
  try { marks = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { marks = {}; }
  function save(){ try { localStorage.setItem(KEY, JSON.stringify(marks)); } catch (e) {} }
  var cards = Array.prototype.slice.call(document.querySelectorAll(".q"));
  var tableRows = Array.prototype.slice.call(document.querySelectorAll("#answer-table tbody tr"));
  var filter = document.getElementById("filter"), search = document.getElementById("search");
  var details = document.getElementById("question-details");
  function applyFilter(){
    var f = filter.value, query = search.value.trim().toLowerCase(), count = 0;
    cards.forEach(function(c, index){
      var m = (marks[c.dataset.id] || {}).v, row = tableRows[index];
      var matchesFilter = f === "all" || c.dataset.verdict === f || c.dataset.domain === f ||
        (f === "attention" && (c.dataset.verdict === "wrong" || c.dataset.verdict === "unsure")) || (f === "unmarked" && !m);
      var haystack = row.textContent + " " + c.querySelector(".question").textContent;
      var show = matchesFilter && (!query || haystack.toLowerCase().includes(query));
      c.hidden = !show; row.hidden = !show;
      if (show) count++;
    });
    document.getElementById("visible-count").textContent = count + " of " + cards.length + " shown";
    document.getElementById("no-results").hidden = count > 0;
  }
  function paint(){
    var n = 0, r = 0, w = 0, u = 0;
    cards.forEach(function(c){
      var m = marks[c.dataset.id] || {};
      c.classList.toggle("right", m.v === "right");
      c.classList.toggle("wrong", m.v === "wrong");
      if (m.v) { n++; if (m.v === "right") r++; if (m.v === "wrong") w++; if (m.v === "unsure") u++; }
    });
    document.getElementById("progress").textContent = "Your follow-up: " + n + " of " + cards.length + " marked (" + r + " right, " + w + " wrong, " + u + " unsure)";
  }
  cards.forEach(function(c){
    var id = c.dataset.id, m = marks[id] || {};
    c.querySelectorAll("input[type=radio]").forEach(function(i){
      if (i.value === m.v) i.checked = true;
      i.addEventListener("change", function(){ marks[id] = marks[id] || {}; marks[id].v = i.value; save(); paint(); applyFilter(); });
    });
    var note = c.querySelector(".note");
    note.value = m.note || "";
    note.addEventListener("input", function(){ marks[id] = marks[id] || {}; marks[id].note = note.value; save(); });
  });
  filter.addEventListener("change", applyFilter);
  search.addEventListener("input", applyFilter);
  function openQuestion(){
    if (!location.hash.startsWith("#question-")) return;
    var card = document.getElementById(location.hash.slice(1));
    if (!card) return;
    if (card.hidden) { filter.value = "all"; search.value = ""; applyFilter(); }
    details.open = true;
    requestAnimationFrame(function(){ card.scrollIntoView({block:"start"}); });
  }
  document.querySelectorAll(".question-link").forEach(function(link){
    link.addEventListener("click", function(){ details.open = true; if (location.hash === link.hash) openQuestion(); });
  });
  window.addEventListener("hashchange", openQuestion);
  document.getElementById("export").addEventListener("click", function(){
    var blob = new Blob([JSON.stringify(marks, null, 1)], {type: "application/json"});
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "fresh-frozen-20261110-marks.json";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function(){ URL.revokeObjectURL(a.href); }, 1000);
  });
  paint();
  applyFilter();
  openQuestion();
})();
</script></body></html>
"""
    substitutions = {'WORKBOOK_COUNT': str(len({r['workbook_file'] for r in rows})),
                     'REVIEW_COUNT': str(len(audit)), 'RIGHT_COUNT': str(counts['Right']),
                     'WRONG_COUNT': str(counts['Wrong']), 'UNSURE_COUNT': str(counts['Unsure']),
                     'COUNT': str(len(rows)), 'TABLE_ROWS': '\n'.join(table_rows), 'CARDS': '\n'.join(cards)}
    for key, value in substitutions.items():
        page = page.replace(key, value)
    with open(os.path.join(ROOT, "verify.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(page)


if __name__ == "__main__":
    main()
