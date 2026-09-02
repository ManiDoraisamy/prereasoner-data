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

    def fake_rest(method, path, body=None, timeout=90, key=None):
        events.append((method, path))
        if method == "POST":
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

    assert not (ROOT / "training/data/pod_id.txt").exists()
    source = _text("training/tools/runpod_api.py")
    assert "finally:" in source and "--keep" in source and "max_minutes * 60" in source
    assert '"terminateAfter"' in source


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


def test_cloud_build_context_is_git_archive_plus_manifested_weights():
    from deploy.gcp.build_context import SOURCE_ALLOWLIST

    source = _text("deploy/gcp/build_context.py")
    assert '"archive", "--format=tar"' in source
    assert "validate_weight_bundle" in source
    assert 'for relative in manifest["files"]' in source
    assert {"engine", "db", "regress", "mcp_server", "orchestrator"} <= set(SOURCE_ALLOWLIST)
    assert not {"training", "tests", "spider", "world_eval", "infra"} & set(SOURCE_ALLOWLIST)
    for ignore in (".venv/", "service-account*.json", "*.tfstate"):
        assert ignore in _text(".gcloudignore")


def test_schema_training_selection_never_reads_test_evidence():
    from collections import Counter

    from training.schema_org.signatures import _class_data_ready, _real_selection_sources
    from training.schema_org.train_property_head import _training_properties

    support = {
        "train": Counter({"p": 25}),
        "validation": Counter({"p": 5}),
        "test": Counter(),
    }
    groups = {"p": {"validation": set(range(5)), "test": set()}}
    assert _training_properties(("p",), support, groups) == ("p",)
    support["test"]["p"] = 10_000
    groups["p"]["test"] = set(range(10_000))
    assert _training_properties(("p",), support, groups) == ("p",)
    assert _class_data_ready(25, 5, [1, 2], ["publisher"])
    sources = {"publisher": Counter({"test": 100}), "product_templates": Counter({"train": 100})}
    assert _real_selection_sources(sources) == []
    sources["publisher"]["validation"] = 1
    assert _real_selection_sources(sources) == ["publisher"]
    source = _text("training/schema_org/train_property_head.py")
    assert '"selection_data": ("train", "validation")' in source
    assert '"evaluation_data": ("test",)' in source


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
    test_cloud_build_context_is_git_archive_plus_manifested_weights,
    test_schema_training_selection_never_reads_test_evidence,
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
