"""Hermetic invariants for a clean public source checkout."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _version(requirements: str, package: str) -> tuple[int, ...]:
    match = re.search(rf"(?m)^{re.escape(package)}==([0-9]+(?:\.[0-9]+)+)", requirements)
    assert match, f"{package} must have an exact release pin"
    return tuple(int(part) for part in match.group(1).split("."))


def test_public_artifact_boundary():
    tracked = subprocess.check_output(
        ["git", "ls-files"], cwd=ROOT, text=True, encoding="utf-8"
    ).splitlines()
    folded = [path.replace("\\", "/").lower() for path in tracked]
    assert not any("spider/results/" in path and "per_example" in path for path in folded)
    assert "training/world/build_wikipedia_schema.py" not in folded
    assert not any(path.endswith((".pt", ".tfstate", ".env")) for path in folded)
    gitleaks = _text(".gitleaks.toml")
    assert "web/public/lib/config" in gitleaks
    assert gitleaks.count("paths =") == 1


def test_spider_evaluator_supports_module_invocation():
    result = subprocess.run(
        [sys.executable, "-m", "spider.probe.full_eval", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--config" in result.stdout


def test_public_weight_bundle_is_manifested_and_documented():
    manifest = json.loads(_text("engine/data/weights_manifest.json"))
    assert manifest["repository"] == "prereasoner/prereasoner-weights"
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["revision"])
    assert len(manifest["files"]) == 7

    setup_docs = "\n".join(
        _text(path)
        for path in (
            "README.md",
            "docs/GETTING_STARTED.md",
            "docs/MODEL_CARD.md",
            "engine/data/README.md",
        )
    )
    assert "https://huggingface.co/prereasoner/prereasoner-weights" in setup_docs
    assert "default weight repository is private" not in setup_docs.lower()
    card = _text("docs/HUGGING_FACE_MODEL_CARD.md")
    assert "No Hugging Face account or token is required" in card
    assert "historical training run" in card


def test_fresh_weight_fetch_stages_committed_artifacts():
    from engine.artifact_provenance import sha256_file, validate_weight_bundle
    from engine.fetch_weights import _stage_committed_artifacts

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source"
        staging = root / "staging"
        source.mkdir()
        staging.mkdir()
        (source / "ontology.json").write_text('{"version": 30}', encoding="utf-8")
        (staging / "encoder.pt").write_bytes(b"public-weight")
        manifest = {
            "version": 1,
            "files": {"encoder.pt": sha256_file(staging / "encoder.pt")},
            "committed_artifacts": {
                "ontology.json": {"sha256": sha256_file(source / "ontology.json")}
            },
        }

        _stage_committed_artifacts(source, staging, manifest)
        assert validate_weight_bundle(staging, manifest)


def test_supported_model_stack_is_security_baseline():
    serving = _text("requirements.txt")
    training = _text("training/requirements.txt")
    assert _version(serving, "torch") >= (2, 13, 0)
    assert _version(serving, "transformers") >= (5, 10, 4)
    for package in ("torch", "transformers", "tokenizers", "safetensors", "accelerate", "peft"):
        assert _version(training, package) == _version(serving, package), (
            f"training and serving disagree on {package}"
        )
    sync = _text("db/sync/requirements.txt")
    for package in ("torch", "transformers", "tokenizers"):
        assert _version(sync, package) == _version(serving, package), (
            f"source sync and serving disagree on {package}"
        )
    assert _text(".python-version").strip() == "3.11"
    model_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (ROOT / "engine", ROOT / "training")
        for path in root.rglob("*.py")
    )
    assert "weights_only=False" not in model_sources
    assert "allow_pickle=True" not in model_sources
    assert "revision=MODEL_REVISION" in _text("engine/encoder_overlay.py")
    assert "revision=MODEL_REVISION" in _text("engine/dimension.py")
    artifact_docs = _text("engine/data/README.md")
    all_docs = "\n".join(path.read_text(encoding="utf-8") for path in ROOT.rglob("*.md"))
    assert "weights_only=False" not in all_docs
    assert "weights_only=True" in artifact_docs


def test_privacy_is_a_published_route_not_a_request_dialog():
    privacy = _text("web/public/privacy.html")
    assert "Anthropic" in privacy and "Google Cloud" in privacy
    for page in ("index.html", "reason.html", "knowledge.html", "picker.html", "chatui.html"):
        assert 'href="/privacy"' in _text(f"web/public/{page}")
    client = (_text("web/public/lib/workbook-reference.js")
              + _text("web/public/lib/workbook-conversations.js")
              + _text("web/public/lib/workbook.js") + _text("web/public/chatui.html"))
    assert "external_llm_consent" not in client
    assert not re.search(
        r"confirm\([^)]*(Anthropic|Claude|consent|local-only)", client, re.IGNORECASE
    )


def test_external_model_deployment_fails_closed():
    variables = _text("infra/variables.tf")
    main = _text("infra/main.tf")
    assert 'variable "enable_external_llm"' in variables
    external_var = variables.split('variable "enable_external_llm"', 1)[1].split("}", 1)[0]
    assert re.search(r"default\s*=\s*false", external_var)
    assert "external_llm_enabled = var.enable_external_llm || var.enable_orchestrator" in main
    assert 'value = tostring(local.external_llm_enabled)' in main
    assert 'count     = local.external_llm_enabled ? 1 : 0' in main


def test_wikidata_precreator_is_non_destructive():
    builder = _text("db/sync/build_wikipedia.py")
    assert "DROP TABLE" not in builder
    assert "DROP SCHEMA" not in builder
    assert "--force" not in builder and "--yes" not in builder


def test_local_documentation_links_resolve():
    missing = []
    ignored_parts = {".git", ".terraform", ".venv", "venv", "node_modules", "site-packages"}
    for document in ROOT.rglob("*.md"):
        if any(part.lower() in ignored_parts for part in document.parts):
            continue
        for raw in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
            target = raw.strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0].split(' "', 1)[0]
            if target and not (document.parent / unquote(target)).exists():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")
    assert not missing, "missing local documentation links:\n" + "\n".join(missing)


def test_public_test_imports_do_not_require_model_stack():
    script = r'''
import builtins

real_import = builtins.__import__
blocked = {"torch", "transformers", "peft"}

def import_without_model_stack(name, *args, **kwargs):
    if name.split(".", 1)[0] in blocked:
        raise ModuleNotFoundError(f"blocked optional model dependency: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = import_without_model_stack
import engine.knowledge
from engine.knowledge_query import KnowledgeQuery
from training.schema_org.train_property_head import _cached_embeddings
from training.props.eval_intent import read_op_mirror, thresholds_from_score_rows
assert KnowledgeQuery and _cached_embeddings and read_op_mirror and thresholds_from_score_rows
'''
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)


def test_documentation_has_one_status_entrypoint():
    root = _text("README.md")
    index = _text("docs/README.md")
    architecture = _text("docs/ARCHITECTURE.md")
    roadmap = _text("docs/KNOWLEDGE_ENRICHMENT_ROADMAP.md")
    marketing = _text("docs/MARKETING_WEBSITE_REVIEW.md")

    assert "docs/README.md" in root
    for state in ("Current", "Opt-in", "External", "Planned"):
        assert f"**{state}**" in index
    assert "current runtime architecture" in architecture
    assert "pending migration" in architecture
    assert "mixed implementation ledger and forward roadmap" in roadmap
    assert "read-only" in marketing and "does not authorize changes" in marketing


def test_runpod_training_is_an_owned_bounded_lease():
    from training.tools import runpod_api

    events = []
    create_bodies = []

    def fake_rest(method, path, body=None, timeout=90, key=None):
        events.append((method, path))
        if method == "POST":
            create_bodies.append(body)
            return 201, {"id": "pod-1"}
        if method == "GET":
            return 200, {"desiredStatus": "RUNNING", "publicIp": "127.0.0.1", "portMappings": {"22": 22}}
        return 204, None

    with tempfile.TemporaryDirectory() as directory, \
            patch.object(runpod_api, "STATE", Path(directory) / "active.json"), \
            patch.object(runpod_api, "rest", side_effect=fake_rest), \
            patch.object(runpod_api, "pubkey", return_value="ssh-ed25519 test"), \
            patch.object(runpod_api.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 60)):
        try:
            runpod_api.run_lease(1, ["python", "train.py"])
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("training timeout was swallowed")
        assert events[-1] == ("DELETE", "/pods/pod-1")
        assert not runpod_api.STATE.exists()
        body = create_bodies[0]
        assert "terminateAfter" not in body
        assert body["imageName"] == runpod_api.IMAGE and "@sha256:" in body["imageName"]
        assert "runpodctl pod delete" in body["dockerStartCmd"][-1]
        assert "HF_TOKEN" not in body["env"]

    assert not (ROOT / "training/data/pod_id.txt").exists()
    source = _text("training/tools/runpod_api.py")
    assert "finally:" in source and "--keep" in source and "max_minutes * 60" in source
    assert '"dockerStartCmd"' in source and "runpodctl pod delete" in source
    assert '"terminateAfter"' not in source
    assert "@sha256:" in source


def test_runpod_cleanup_failure_does_not_hide_training_failure():
    from training.tools import runpod_api

    def fake_rest(method, path, body=None, timeout=90, key=None):
        if method == "POST":
            return 201, {"id": "pod-2"}
        if method == "GET":
            return 200, {"desiredStatus": "RUNNING", "publicIp": "127.0.0.1",
                         "portMappings": {"22": 22}}
        return 503, "provider unavailable"

    with tempfile.TemporaryDirectory() as directory, \
            patch.object(runpod_api, "STATE", Path(directory) / "active.json"), \
            patch.object(runpod_api, "rest", side_effect=fake_rest), \
            patch.object(runpod_api, "pubkey", return_value="ssh-ed25519 test"), \
            patch.object(runpod_api.time, "sleep"), \
            patch.object(runpod_api.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 60)):
        try:
            runpod_api.run_lease(1, ["python", "train.py"])
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("cleanup failure masked or swallowed the training timeout")
        assert runpod_api.STATE.exists(), "unconfirmed termination must retain recovery state"


def test_runpod_transfers_are_inside_the_owned_lease():
    from training.tools import runpod_api

    events = []

    def fake_rest(method, path, body=None, timeout=90, key=None):
        if method == "POST":
            return 201, {"id": "pod-3"}
        if method == "GET":
            return 200, {"desiredStatus": "RUNNING", "publicIp": "127.0.0.1",
                         "portMappings": {"22": 22}}
        events.append((method, path))
        return 204, None

    with tempfile.TemporaryDirectory() as directory, \
            patch.object(runpod_api, "STATE", Path(directory) / "active.json"), \
            patch.object(runpod_api, "rest", side_effect=fake_rest), \
            patch.object(runpod_api, "pubkey", return_value="ssh-ed25519 test"), \
            patch.object(runpod_api.subprocess, "run") as run:
        source = Path(directory) / "input.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        runpod_api.run_lease(
            1,
            ["python", "-m", "training.schema_org.train_property_head"],
            uploads=((str(source), "/workspace/input.jsonl"),),
            downloads=(("/workspace/output", str(Path(directory) / "output")),),
        )
        assert run.call_count == 3
        assert "input.jsonl" in run.call_args_list[0].args[0][-2]
        assert "train_property_head" in run.call_args_list[1].args[0][-1]
        assert "/workspace/output" in run.call_args_list[2].args[0][-2]
        assert events[-1] == ("DELETE", "/pods/pod-3")
        assert not runpod_api.STATE.exists()
        try:
            runpod_api._remote_path("/workspace/../root/.ssh")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe remote transfer path was accepted")


def test_runpod_retries_only_idempotent_transfers():
    from training.tools import runpod_api

    transient = subprocess.CalledProcessError(255, ["scp"])
    with patch.object(
        runpod_api.subprocess, "run",
        side_effect=[transient, subprocess.CompletedProcess(["scp"], 0)],
    ) as run, patch.object(runpod_api.time, "sleep"):
        runpod_api._run_transfer(["scp", "source", "target"], lambda: 60)
        assert run.call_count == 2

    source = _text("training/tools/runpod_api.py")
    assert source.count("_run_transfer(") >= 3
    assert "subprocess.run([*ssh" in source


def test_cloud_build_context_is_git_archive_plus_manifested_weights():
    from deploy.gcp.build_context import SOURCE_ALLOWLIST, SOURCE_SYNC_ALLOWLIST

    source = _text("deploy/gcp/build_context.py")
    assert '"archive", "--format=tar"' in source
    assert "validate_weight_bundle" in source
    assert 'for relative in manifest["files"]' in source
    assert {"engine", "db", "regress", "mcp_server", "orchestrator"} <= set(SOURCE_ALLOWLIST)
    assert "requirements.lock.txt" in SOURCE_ALLOWLIST
    assert not {"training", "tests", "spider", "world_eval", "infra"} & set(SOURCE_ALLOWLIST)
    assert {"Dockerfile.sync", "cloudbuild.sync.yaml", "db"} <= set(SOURCE_SYNC_ALLOWLIST)
    assert not {"engine", "training", "tests", "spider", "world_eval"} & set(
        SOURCE_SYNC_ALLOWLIST
    )
    assert {
        "engine/__init__.py",
        "engine/enrichment/__init__.py",
        "engine/enrichment/registry.py",
    } <= set(SOURCE_SYNC_ALLOWLIST)
    sync_dockerfile = _text("Dockerfile.sync")
    assert "COPY engine/enrichment/registry.py" in sync_dockerfile
    assert "COPY engine/ /app/engine/" not in sync_dockerfile
    assert 'choices=("engine", "sync")' in source
    assert '"build_target": target' in source
    workflow = _text(".github/workflows/ci.yml")
    assert "prereasoner-engine:ci -c \"import engine.server" in workflow
    assert "prereasoner-sync:ci -c \"from db.sync.community_bootstrap" in workflow
    for ignore in (".venv*/", "service-account*.json", "*.tfstate"):
        assert ignore in _text(".gcloudignore")


def test_live_database_tests_allocate_production_shaped_schemas():
    from regress.live_schema import live_schema

    with patch.dict("os.environ", {}, clear=True), patch(
        "regress.live_schema.atexit.register"
    ) as register:
        first = live_schema()
        second = live_schema()
    assert re.fullmatch(r"c_[0-9a-f]{32}", first.name)
    assert re.fullmatch(r"c_[0-9a-f]{32}", second.name)
    assert first.name != second.name and first.managed and second.managed
    assert register.call_count == 2

    with patch.dict("os.environ", {"AUTH_TEST_SUB": "explicit_test_schema"}, clear=True):
        explicit = live_schema()
    assert explicit.name == "explicit_test_schema" and not explicit.managed


def test_release_installs_only_hash_locked_dependencies():
    locks = (
        "requirements.lock.txt",
        "requirements-ci.lock.txt",
        "requirements-ci-windows.lock.txt",
        "orchestrator/requirements.lock.txt",
        "db/sync/requirements-core.lock.txt",
        "db/sync/requirements.lock.txt",
        "deploy/gcp/requirements.lock.txt",
        "training/requirements.lock.txt",
    )
    for relative in locks:
        lock = _text(relative)
        assert "--hash=sha256:" in lock, f"release dependency lock has no hashes: {relative}"
    assert "--require-hashes -r /tmp/requirements.lock.txt" in _text("Dockerfile")
    assert "--require-hashes -r orchestrator/requirements.lock.txt" in _text(
        "Dockerfile.orchestrator"
    )
    assert "--require-hashes -r /tmp/requirements-core.lock.txt" in _text("Dockerfile.sync")
    assert "--require-hashes -r requirements-ci.lock.txt" in _text(".github/workflows/ci.yml")
    assert "--require-hashes -r deploy/gcp/requirements.lock.txt" in _text(
        "deploy/gcp/deploy.sh"
    )
    assert "if [[ ! -x" in _text("deploy/gcp/deploy.sh")
    assert _text("deploy/gcp/deploy.sh").count("--require-hashes -r deploy/gcp/requirements.lock.txt") == 1
    from deploy.dependency_locks import check

    check()


def test_world_evaluation_records_release_provenance():
    source = _text("world_eval/run.py")
    for field in (
        "source_commit",
        "worktree_dirty",
        "weights_revision",
        "weights_manifest_sha256",
        "release_id",
        "last_refreshed_at",
    ):
        assert f'"{field}"' in source
    assert "validate_weight_bundle" in source
    assert "knowledgebase.schedule" in source


def test_gpu_training_preserves_the_runner_cuda_torch():
    from training.tools.install_dependencies import _without_torch

    lock = _text("training/requirements.lock.txt")
    gpu_lock = _without_torch(lock)
    assert "torch==" in lock
    assert "torch==" not in gpu_lock
    runner = _text("training/tools/run_schema_training.sh")
    assert "python -m training.tools.install_dependencies gpu" in runner
    assert "pip install" not in runner


def test_schema_training_selection_never_reads_test_evidence():
    from collections import Counter

    from training.schema_org.promote import _committed_blob_sha256
    from training.schema_org.signatures import (
        _class_data_ready,
        _real_selection_sources,
    )
    from training.schema_org.train_property_head import (
        _trainer_identity,
        _training_properties,
    )

    support = {
        "train": Counter({"p": 25}),
        "validation": Counter({"p": 10}),
        "test": Counter(),
    }
    groups = {"p": {"validation": set(range(10)), "test": set()}}
    assert _training_properties(("p",), support, groups) == ("p",)
    support["test"]["p"] = 10_000
    groups["p"]["test"] = set(range(10_000))
    assert _training_properties(("p",), support, groups) == ("p",)
    assert _class_data_ready(25, 10, [1, 2], ["publisher"])
    assert not _class_data_ready(25, 9, [1, 2], ["publisher"])
    sources = {"publisher": Counter({"test": 100}), "product_templates": Counter({"train": 100})}
    assert _real_selection_sources(sources) == []
    sources["publisher"]["validation"] = 1
    assert _real_selection_sources(sources) == ["publisher"]
    source = _text("training/schema_org/train_property_head.py")
    assert '"selection_data": ("train", "validation")' in source
    assert '"evaluation_data": ("test",)' in source
    assert '"trainer": _trainer_identity()' in source
    assert '"runtime": _runtime_identity(torch, device, args.runner_image)' in source
    assert '"scikit_learn": importlib.metadata.version("scikit-learn")' in source
    assert '"--runner-image"' in source
    assert "schema_embeddings.npz" in _text("training/tools/run_schema_training.sh")
    trainer = _trainer_identity()
    assert all(
        _committed_blob_sha256(trainer["repository_commit"], relative) == expected
        for relative, expected in trainer["source_files"].items()
    ), "trainer provenance must hash committed Git blobs, not platform-normalized checkout bytes"
    promotion = _text("training/schema_org/promote.py")
    assert "trainer source does not match its recorded commit" in promotion
    assert "does not identify its immutable runner image" in promotion
    assert "does not satisfy untouched heldout gates" in promotion


def test_schema_signatures_keep_named_companion_evidence_explicit():
    from collections import Counter

    from training.schema_org.signatures import _signature_candidates

    candidates = _signature_candidates(
        Counter({"currentExchangeRate": 80, "priceCurrency": 75}),
        100,
        Counter({"currentExchangeRate": 90, "priceCurrency": 100}),
        10_000,
        {"currentExchangeRate"},
    )
    by_property = {item["property"]: item for item in candidates}
    assert set(by_property) == {"currentExchangeRate", "priceCurrency"}
    assert by_property["currentExchangeRate"]["ontology_compatible"] is True
    assert by_property["priceCurrency"]["ontology_compatible"] is False


def test_schema_promotion_preserves_the_served_class_surface():
    from training.schema_org.promote import _servable_class_uris

    current = {"classes": [
        {"uri": "https://schema.org/Movie", "servable": True},
        {"uri": "https://schema.org/Place", "servable": False},
    ]}
    candidate = {"classes": [
        {"uri": "https://schema.org/Movie", "servable": False},
        {"uri": "https://schema.org/Place", "servable": True},
    ]}
    removed = _servable_class_uris(current) - _servable_class_uris(candidate)
    assert removed == {"https://schema.org/Movie"}, (
        "a candidate may add classes, but removing a currently served class must fail release"
    )


def test_class_model_is_a_deterministic_signed_named_dimension_superposition():
    import numpy as np

    from training.schema_org.train_property_head import _fit_continuous_class_model

    signature = [
        {"property": "diagnostic", "weight": 4.0},
        {"property": "anti_signal", "weight": 2.0},
        {"property": "generic", "weight": 1.0},
    ]
    positive = {"diagnostic": 0.95, "anti_signal": 0.05, "generic": 0.8}
    negative = {"diagnostic": 0.05, "anti_signal": 0.95, "generic": 0.8}
    train_profiles = [
        *(dict(positive) for _ in range(50)),
        *(dict(negative) for _ in range(50)),
    ]
    validation_profiles = [
        *(dict(positive) for _ in range(20)),
        *(dict(negative) for _ in range(20)),
    ]
    train_truth = np.array([1] * 50 + [0] * 50, dtype=np.int8)
    validation_truth = np.array([1] * 20 + [0] * 20, dtype=np.int8)
    property_sets = [frozenset(positive) for _ in validation_profiles]
    args = (
        signature, train_profiles, train_truth, validation_profiles,
        validation_truth, property_sets,
    )
    first = _fit_continuous_class_model(*args)
    second = _fit_continuous_class_model(*args)
    fitted, bias, threshold, feasible, metrics, regularization = first
    weights = {item["property"]: item["weight"] for item in fitted}
    assert first == second, "fixed data and seed must produce an identical class artifact"
    assert feasible and metrics["precision"] == 1.0 and metrics["recall"] == 1.0
    assert metrics["evidence_coverage"] == 1.0
    assert weights["diagnostic"] > 0 and weights["anti_signal"] < 0
    assert abs(weights["diagnostic"]) > abs(weights["generic"])
    assert all("ontology_weight" in item for item in fitted)
    assert isinstance(bias, float) and 0.0 < threshold <= 1.0
    assert regularization > 0


def test_class_threshold_uses_the_validation_gap_without_changing_decisions():
    import numpy as np

    from training.schema_org.train_property_head import _metrics, _precision_threshold

    scores = np.array([0.91, 0.81, 0.22, 0.10])
    truth = np.array([1, 1, 0, 0], dtype=np.int8)
    edge, feasible = _precision_threshold(scores, truth, 0.97, margin=False)
    midpoint, midpoint_feasible = _precision_threshold(scores, truth, 0.97)
    assert feasible and midpoint_feasible
    assert edge == 0.81 and midpoint == (0.81 + 0.22) / 2.0
    assert _metrics(scores, truth, edge) == _metrics(scores, truth, midpoint)


def test_class_metrics_separate_evidence_coverage_from_accuracy():
    import numpy as np

    from training.schema_org.train_property_head import _class_metrics

    metrics, scope = _class_metrics(
        np.array([0.9, 0.1, 0.1]), np.array([1, 1, 0], dtype=np.int8),
        [frozenset({"a", "b"}), frozenset({"a"}), frozenset({"a", "b"})],
        [{"property": "a", "weight": 1.0}, {"property": "b", "weight": 1.0}],
        0.8,
    )
    assert scope.tolist() == [True, False, True]
    assert metrics["precision"] == 1.0 and metrics["recall"] == 1.0
    assert metrics["evidence_coverage"] == 0.5


TESTS = [
    test_public_artifact_boundary,
    test_public_weight_bundle_is_manifested_and_documented,
    test_fresh_weight_fetch_stages_committed_artifacts,
    test_supported_model_stack_is_security_baseline,
    test_privacy_is_a_published_route_not_a_request_dialog,
    test_external_model_deployment_fails_closed,
    test_wikidata_precreator_is_non_destructive,
    test_local_documentation_links_resolve,
    test_public_test_imports_do_not_require_model_stack,
    test_documentation_has_one_status_entrypoint,
    test_runpod_training_is_an_owned_bounded_lease,
    test_runpod_cleanup_failure_does_not_hide_training_failure,
    test_runpod_transfers_are_inside_the_owned_lease,
    test_runpod_retries_only_idempotent_transfers,
    test_cloud_build_context_is_git_archive_plus_manifested_weights,
    test_live_database_tests_allocate_production_shaped_schemas,
    test_release_installs_only_hash_locked_dependencies,
    test_world_evaluation_records_release_provenance,
    test_gpu_training_preserves_the_runner_cuda_torch,
    test_schema_training_selection_never_reads_test_evidence,
    test_schema_signatures_keep_named_companion_evidence_explicit,
    test_schema_promotion_preserves_the_served_class_surface,
    test_class_model_is_a_deterministic_signed_named_dimension_superposition,
    test_class_threshold_uses_the_validation_gap_without_changing_decisions,
    test_class_metrics_separate_evidence_coverage_from_accuracy,
]


def main():
    failed = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nrelease: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
