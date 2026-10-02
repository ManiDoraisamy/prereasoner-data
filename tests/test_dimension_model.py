"""Hermetic startup and encoder-sharing tests for /api/dimension."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from engine import dimension as dimension_module

P = 0
F = 0


def ok(condition, message):
    global P, F
    if condition:
        P += 1
        print(f"  PASS  {message}")
    else:
        F += 1
        print(f"  FAIL  {message}")


def _shared_encoder():
    return SimpleNamespace(
        model_bundle_sha256="bundle-hash",
        encoder_data_dir="/models/encoder",
        alloc={"n_content": 1},
        nc=1,
        dims=[{"name": "taxonomy", "dim_id": 0, "family": "taxonomy"}],
        sid={"taxonomy": 0},
        thr={"taxonomy": 0.5},
        model=object(),
        nL=2,
        tok=object(),
        qwen=object(),
        hdim=8,
    )


def test_dimension_reuses_world_encoder_and_copies_thresholds():
    shared = _shared_encoder()
    with tempfile.TemporaryDirectory() as directory:
        deploy_dir = Path(directory)
        (deploy_dir / "dim_thresholds.json").write_text(
            json.dumps({"taxonomy": 0.7}), encoding="utf-8"
        )
        dimension = dimension_module.DimensionModel(deploy_dir, shared_encoder=shared)

    ok(dimension.qwen is shared.qwen, "dimension endpoint reuses the world Qwen")
    ok(dimension.tok is shared.tok, "dimension endpoint reuses the world tokenizer")
    ok(dimension.model is shared.model, "dimension endpoint reuses the world readout")
    ok(dimension.dims is shared.dims, "dimension endpoint reuses immutable dimension metadata")
    ok(dimension.thr == {"taxonomy": 0.7}, "dimension calibration overrides are applied")
    ok(dimension.thr is not shared.thr, "dimension overrides cannot mutate world thresholds")
    ok(shared.thr == {"taxonomy": 0.5}, "world calibration remains unchanged")


def test_dimension_standalone_loads_only_the_encoder():
    """The dimension endpoint is a readout over the encoder bundle. It once constructed the own-data
    planner's SQL proposer without using it; it attaches no part of own-data selection."""
    calls = []
    original = dimension_module.load_encoder

    def fake_load_encoder(model, deploy_dir):
        calls.append((model, deploy_dir))
        model.thr = {"taxonomy": 0.5}

    dimension_module.load_encoder = fake_load_encoder
    try:
        with tempfile.TemporaryDirectory() as directory:
            deploy_dir = Path(directory)
            model = dimension_module.DimensionModel(deploy_dir)
    finally:
        dimension_module.load_encoder = original

    ok(len(calls) == 1, "standalone dimension loads the shared encoder bundle once")
    ok(calls[0][0] is model, "standalone loader initializes the dimension model")
    ok(calls[0][1] == deploy_dir, "standalone loader receives the selected bundle directory")
    ok(model.sql_fallback is None, "dimension endpoint attaches no own-data SQL selection fallback")


def test_dimension_waits_on_the_shared_engine_lock_with_a_bound():
    """DIM_LOCK is WORLD_LOCK (the endpoints share the world encoder). /api/reason waits
    QUEUE_TIMEOUT_SECONDS and answers 503; /api/dimension waited without a bound and held its
    request thread for the whole request ahead of it (then a 7B SQL decode). It now answers the
    same retryable 503."""
    import threading
    from engine import server

    sent = []
    handler = object.__new__(server.H)
    handler.headers, handler.client_address = {}, ("127.0.0.1", 0)
    handler._read_json = lambda: {"data": "a,b\n1,2\n", "table": "t", "mode": "analyze"}
    handler._send = lambda code, body, *args, **kwargs: sent.append((code, json.loads(body)))
    originals = (server._verify_principal, server.DIM_RATE, server.DIM_LOCK, server.QUEUE_TIMEOUT_SECONDS,
                 server.DIM_MODEL)
    lock, held, release = threading.Lock(), threading.Event(), threading.Event()

    def busy():
        with lock:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=busy)
    worker.start()
    held.wait(5)
    try:
        server._verify_principal = lambda _token: ("tester", "uid")
        server.DIM_RATE = SimpleNamespace(allow=lambda _key: (True, 0))
        server.DIM_LOCK, server.QUEUE_TIMEOUT_SECONDS = lock, 0.05
        server.DIM_MODEL = SimpleNamespace(analyze=lambda _table: {"columns": []})
        handler._post_dimension()
        release.set()
        worker.join(5)
        handler._post_dimension()
    finally:
        release.set()
        (server._verify_principal, server.DIM_RATE, server.DIM_LOCK, server.QUEUE_TIMEOUT_SECONDS,
         server.DIM_MODEL) = originals
    ok(sent[0] == (503, {"error": "Engine is busy; retry shortly", "retryable": True}),
       f"a dimension request behind another request is a retryable 503 (got {sent[:1]})")
    ok(sent[1:] == [(200, {"columns": []})] and not lock.locked(),
       "once the engine is free the request is served and the lock released")


def main():
    print("[dimension] shared model startup contract")
    test_dimension_reuses_world_encoder_and_copies_thresholds()
    test_dimension_standalone_loads_only_the_encoder()
    test_dimension_waits_on_the_shared_engine_lock_with_a_bound()
    print(f"\ntest_dimension_model: {P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
