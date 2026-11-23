"""Send a file of requests to Gemini through engine/llm.py, each journalled with its token counts, so a restart never pays twice. Run from the repository root, whose engine/llm.py is the one client.

usage (from the repository root): python training/factory/llm_run.py <requests.jsonl> <journal name> [--workers 12] [--stage-output N] [--timeout 150]

A request: {"key", "system", "prompt", "max_output"}. A journal line: {"key", "reply"|"error", "prompt_tokens", "output_tokens", "thought_tokens", "finish", "seconds"}. Stops sending at --stop-share (97%) of the hard budget over all journals, or at --stage-output tokens of this run. Prints counts, never a prompt or a reply.
"""
import argparse
import datetime
import importlib.util
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

os.environ["GOOGLE_CLOUD_PROJECT"] = "prereasoner-inference"
sys.path.insert(0, os.getcwd())
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(f"factory_{name}", os.path.join(HERE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


paths = _load("paths")
ledger = _load("ledger")
from engine import llm  # noqa: E402

USAGE = threading.local()


def watch_usage():
    models = llm._client().models
    original = models.generate_content

    def generate_content(**kwargs):
        response = original(**kwargs)
        usage = getattr(response, "usage_metadata", None)
        candidates = getattr(response, "candidates", None) or []
        USAGE.record = {"prompt_tokens": getattr(usage, "prompt_token_count", None), "output_tokens": getattr(usage, "candidates_token_count", None), "thought_tokens": getattr(usage, "thoughts_token_count", None), "total_tokens": getattr(usage, "total_token_count", None),
                        "model_version": getattr(response, "model_version", None), "finish": str(getattr(candidates[0], "finish_reason", None)) if candidates else None}
        return response

    models.generate_content = generate_content


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("requests")
    parser.add_argument("journal")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--stage-output", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--tries", type=int, default=3)
    parser.add_argument("--stop-share", type=float, default=ledger.STOP_SHARE, help="the share of the hard budget at which sending stops; never above 1")
    args = parser.parse_args()
    if not os.path.exists(os.path.join(os.getcwd(), "engine", "llm.py")):
        sys.exit("run from the repository root")
    os.makedirs(paths.JOURNAL, exist_ok=True)
    journal_path = f"{paths.JOURNAL}{args.journal}.jsonl"
    done = set()
    if os.path.exists(journal_path):
        with open(journal_path, encoding="utf-8") as source:
            for line in source:
                record = json.loads(line)
                if record.get("reply") is not None:
                    done.add(record["key"])
    with open(args.requests, encoding="utf-8") as source:
        work = [request for request in map(json.loads, source) if request["key"] not in done]
    total = ledger.spent(paths.JOURNAL)["TOTAL"]
    state = {"output": total["output"] + total["thought"], "prompt": total["prompt"], "stage": 0, "sent": 0, "failed": 0, "stopped": None}
    print("model", llm.model_id(), "to send", len(work), "already", len(done), "spent", state["output"], state["prompt"], flush=True)
    watch_usage()
    lock = threading.Lock()
    share = min(args.stop_share, 1.0)
    limit_output = ledger.HARD_OUTPUT * share
    limit_prompt = ledger.HARD_INPUT * share
    started = time.time()

    def one(request):
        with lock:
            if state["output"] >= limit_output or state["prompt"] >= limit_prompt:
                state["stopped"] = "hard budget"
                return
            if args.stage_output and state["stage"] >= args.stage_output:
                state["stopped"] = "stage budget"
                return
        for attempt in range(args.tries):
            USAGE.record = {}
            began = time.perf_counter()
            record = {"key": request["key"], "at": datetime.datetime.now().isoformat(timespec="seconds"), "attempt": attempt}
            try:
                record["reply"] = llm.generate_text(system=request["system"], prompt=request["prompt"], max_output_tokens=request.get("max_output", 4000), timeout_seconds=args.timeout, thinking="LOW")
            except llm.LLMUnavailable as exc:
                cause = exc.__cause__
                record.update(reply=None, error=str(exc)[:200], cause=type(cause).__name__ if cause else None, status=getattr(cause, "code", None))
            record["seconds"] = round(time.perf_counter() - began, 2)
            record.update(USAGE.record)
            with lock:
                state["output"] += (record.get("output_tokens") or 0) + (record.get("thought_tokens") or 0)
                state["stage"] += (record.get("output_tokens") or 0) + (record.get("thought_tokens") or 0)
                state["prompt"] += record.get("prompt_tokens") or 0
                with open(journal_path, "a", encoding="utf-8", newline="\n") as sink:
                    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                state["sent"] += 1
                if record.get("reply") is None:
                    state["failed"] += 1
                if state["sent"] % 50 == 0:
                    # Another run may be spending too: the journals, not this process, hold the total. Two runs at once passed 97% on 2026-11-02 for want of this.
                    total_now = ledger.spent(paths.JOURNAL)["TOTAL"]
                    state["output"], state["prompt"] = total_now["output"] + total_now["thought"], total_now["prompt"]
                if state["sent"] % 100 == 0:
                    print("sent", state["sent"], "failed", state["failed"], "stage", state["stage"], "output", state["output"], "prompt", state["prompt"], round(time.time() - started), "s", flush=True)
            if record.get("reply") is not None:
                return
            time.sleep(2 ** attempt * 3 + random.random())

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(one, work))
    print("finished: sent", state["sent"], "failed", state["failed"], "stage", state["stage"], "output", state["output"], "prompt", state["prompt"], "stopped", state["stopped"], round(time.time() - started), "s", flush=True)


if __name__ == "__main__":
    main()
