"""Shared eval utilities: capped table loading + a consistent in-memory gold executor + timeouts.

Denotation is compared on the SAME capped data the engine sees (both gold and prediction run over identical
rows), so a row cap keeps the comparison valid while bounding cost — essential because e.g. wta_1.rankings
has ~510k rows. Only large DBs (wta_1) are actually capped; the 19 others fit under the cap exactly.
"""
from __future__ import annotations
import re
import sqlite3
import threading
import time

try:
    from .spider_eval import compare, is_scalar
except ImportError:  # direct script execution / spider/probe on sys.path
    from spider_eval import compare, is_scalar


def load_capped(db_path, cap=5000):
    """{lower_name: {name, columns, rows}} with rows capped."""
    con = sqlite3.connect(db_path)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = [r[0] for r in cur.fetchall()]
    out = {}
    for n in names:
        if n == "sqlite_sequence":
            continue
        try:
            cur.execute(f'SELECT * FROM "{n}"' + (f" LIMIT {int(cap)}" if cap else ""))
        except sqlite3.Error:
            continue
        cols = [d[0] for d in cur.description]
        out[n.lower()] = {"name": n, "columns": cols, "rows": [list(r) for r in cur.fetchall()]}
    con.close()
    return out


def _num(v):
    try:
        float(str(v).replace(",", "")); return True
    except (ValueError, TypeError):
        return False


def build_mem_db(tabs):
    """An in-memory SQLite with `tabs` (original names), numeric columns stored numerically so gold's
    numeric comparisons (age > 20) behave — the same coercion the engine's own executors use."""
    con = sqlite3.connect(":memory:")
    con.text_factory = str
    for t in tabs:
        cols = t["columns"]; rows = t["rows"]
        aff = []
        for ci, c in enumerate(cols):
            vals = [r[ci] for r in rows if ci < len(r) and r[ci] is not None and str(r[ci]).strip() != ""]
            if vals and all(_num(v) for v in vals):
                aff.append("REAL" if any("." in str(v) for v in vals) else "INTEGER")
            else:
                aff.append("TEXT")
        qn = '"' + t["name"].replace('"', '""') + '"'
        con.execute(f'CREATE TABLE {qn} (' + ", ".join(f'"{c}" {a}' for c, a in zip(cols, aff)) + ')')

        def coerce(v, a):
            if v is None or str(v).strip() == "":
                return None
            if a in ("INTEGER", "REAL"):
                try:
                    return int(float(str(v).replace(",", ""))) if a == "INTEGER" else float(str(v).replace(",", ""))
                except ValueError:
                    return None
            return str(v)
        ins = f'INSERT INTO {qn} VALUES (' + ",".join("?" * len(cols)) + ')'
        for r in rows:
            con.execute(ins, [coerce(r[ci] if ci < len(r) else None, aff[ci]) for ci in range(len(cols))])
    con.commit()
    return con


def exec_sql_timed(con, sql, timeout=8.0, max_rows=None):
    """Run sql on con, aborting after `timeout` seconds via Connection.interrupt() from a watchdog thread."""
    done = threading.Event()
    def watch():
        if not done.wait(timeout):
            con.interrupt()
    w = threading.Thread(target=watch, daemon=True); w.start()
    try:
        cur = con.execute(sql)
        rows = cur.fetchall() if max_rows is None else cur.fetchmany(max_rows + 1)
        if max_rows is not None and len(rows) > max_rows:
            return None, f"ResultTooLarge: more than {max_rows} rows"
        return [list(r) for r in rows], None
    except Exception as e:                       # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    finally:
        done.set()


def run_with_budget(fn, budget=12.0):
    """Run synchronously and report a soft latency-budget violation.

    Python cannot safely kill a thread executing Torch. The previous timeout
    abandoned that thread, allowing timed-out predictions to keep using shared
    model state while later examples ran. Search and SQL execution are bounded
    internally; this wrapper keeps evaluation sequential and deterministic.
    """
    started = time.perf_counter()
    try:
        value, error = fn(), None
    except Exception as exc:                     # noqa: BLE001
        value, error = None, exc
    elapsed = time.perf_counter() - started
    return value, error, elapsed, bool(budget and elapsed > budget)


def _score(counter, gold_rows, candidates, con, top_k):
    if is_scalar(gold_rows):
        counter["scalar_n"] += 1
    counter["candidate_total"] += len(candidates)
    counter["candidate_max"] = max(counter["candidate_max"], len(candidates))
    if not candidates:
        counter["no_candidate"] += 1
        return
    flags = []
    successful = 0
    for candidate in candidates:
        rows, error = exec_sql_timed(con, candidate.sql)
        comparison = compare(gold_rows, rows) if error is None else {}
        flags.append(comparison)
        successful += int(error is None)
    if successful == 0:
        counter["execution_failure"] += 1
        return
    counter["answered"] += 1
    first = flags[0]
    counter["lenient"] += bool(first.get("lenient"))
    counter["strict"] += bool(first.get("strict"))
    counter["oracle_lenient"] += any(flag.get("lenient") for flag in flags)
    counter["oracle_strict"] += any(flag.get("strict") for flag in flags)
    counter["topk_oracle_lenient"] += any(flag.get("lenient") for flag in flags[:top_k])
    counter["topk_oracle_strict"] += any(flag.get("strict") for flag in flags[:top_k])
    if is_scalar(gold_rows):
        counter["scalar"] += bool(first.get("scalar_exact"))


def _summary(counter, total):
    pct = lambda value, denominator=total: round(100 * value / max(denominator, 1), 1)
    return {
        "n": total, "answered": counter["answered"], "no_candidate": counter["no_candidate"],
        "execution_failure": counter["execution_failure"], "gold_execution_failure": counter["gold_execution_failure"],
        "strict_correct": counter["strict"], "lenient_correct": counter["lenient"], "scalar_correct": counter["scalar"],
        "topk_oracle_strict_correct": counter["topk_oracle_strict"], "pool_oracle_strict_correct": counter["oracle_strict"],
        "pool_oracle_lenient_correct": counter["oracle_lenient"],
        "average_candidates": round(counter["candidate_total"] / max(total, 1), 2),
        "maximum_candidates": counter["candidate_max"], "lenient_pct": pct(counter["lenient"]),
        "strict_pct": pct(counter["strict"]), "scalar_pct": pct(counter["scalar"], counter["scalar_n"]),
        "scalar_n": counter["scalar_n"], "topk_oracle_lenient_pct": pct(counter["topk_oracle_lenient"]),
        "topk_oracle_strict_pct": pct(counter["topk_oracle_strict"]), "pool_oracle_lenient_pct": pct(counter["oracle_lenient"]),
        "pool_oracle_strict_pct": pct(counter["oracle_strict"]),
    }
