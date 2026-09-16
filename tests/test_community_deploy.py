"""Hermetic contracts for the public guided GCP deployment."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from db.sync import community_bootstrap as bootstrap_module
from db.sync import schedule as schedule_module

ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_bootstrap_plan_is_minimal_deterministic_and_non_shell():
    plan = bootstrap_module.command_plan()
    assert plan == bootstrap_module.command_plan(), "the build plan must be deterministic"
    # Every step is an argv tuple invoking a module directly — never a shell string, so no
    # deployment input can be word-split or expanded into the bootstrap.
    for command in plan:
        assert isinstance(command, tuple) and all(isinstance(part, str) for part in command)
        assert command[:2] == (sys.executable, "-m"), command
    assert (sys.executable, "-m", "db.sync.sync_wikidata", "--reset", "--high-only") in plan, \
        "the community seed must stay the bounded --high-only import, not the multi-hour full sync"
    assert (sys.executable, "-m", "db.sync.build_qid_world") in plan, \
        "QID serving projections must be built before requests, never lazily"
    assert (sys.executable, "-m", "db.sync.sources.iana.sync") in plan


def test_qid_world_projection_is_an_offline_atomic_transform():
    from db.sync.build_qid_world import rebuild

    class Cursor:
        def __init__(self):
            self.statements = []
            self.rowcount = 0
            self.one = (0,)

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if 'INSERT INTO knowledgebase."country"' in text and "SELECT qid" in text:
                self.rowcount = 196
            elif 'INSERT INTO knowledgebase."city"' in text and "SELECT qid" in text:
                self.rowcount = 1234
            elif text.startswith('SELECT count(*) FROM knowledgebase."'):
                self.one = (1234 if '"city"' in text else 196,)

        def fetchone(self):
            return self.one

        def close(self):
            return None

    class Connection:
        def __init__(self):
            self.cursor_value = Cursor()
            self.commits = 0
            self.rollbacks = 0

        def cursor(self):
            return self.cursor_value

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    connection = Connection()
    assert rebuild(connection) == {"city": 1234, "country": 196}
    sql = "\n".join(statement for statement, _ in connection.cursor_value.statements)
    assert "FROM public.settlement" in sql and "FROM public.country" in sql
    assert 'TRUNCATE knowledgebase."city", knowledgebase."country"' in sql
    assert connection.commits == 1 and connection.rollbacks == 0
    source = _text("db/sync/build_qid_world.py")
    assert "urllib" not in source and "requests" not in source


def test_state_projection_builder_does_not_pull_the_model_runtime():
    """The state projection builder must stay importable in the minimal sync image."""
    source = _text("db/sync/build_u_s_state.py")
    assert "from engine.embeddings" not in source
    assert "import torch" not in source
    assert "from engine" not in source
    from db.sync.build_u_s_state import normalize_surface
    from db.sync._normalize import normalize_surface as shared_normalize_surface

    assert normalize_surface is shared_normalize_surface
    assert normalize_surface("The U.S.") == "us"
    assert normalize_surface("New York") == "newyork"


def test_state_projection_rebuild_is_atomic_and_reports_unresolved_rows():
    from db.sync.build_u_s_state import rebuild

    class Cursor:
        def __init__(self):
            self.rowcount = 1
            self.rows = []
            self.statements = []

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if "type='state'" in text:
                self.rows = [("newyork", "Q1384")]
            elif "type='country'" in text:
                self.rows = [("unitedstates", "Q30")]
            elif 'FROM knowledgebase."country"' in text:
                self.rows = [("Q30", "Q49")]
            elif 'FROM knowledgebase."States"' in text:
                self.rows = [("New York", "United States"), ("Unknown", "United States")]
            elif 'SELECT count(*) FROM knowledgebase."u_s_state"' in text:
                self.rows = [(1,)]
            else:
                self.rows = []

        def fetchall(self):
            return list(self.rows)

        def fetchone(self):
            return self.rows[0]

        def close(self):
            pass

    class Connection:
        def __init__(self):
            self.cursor_value = Cursor()
            self.commits = 0
            self.rollbacks = 0

        def cursor(self):
            return self.cursor_value

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            pass

    connection = Connection()
    assert rebuild(connection) == {"inserted": 1, "skipped": 1, "total": 1}
    sql = "\n".join(statement for statement, _ in connection.cursor_value.statements)
    assert 'TRUNCATE knowledgebase."u_s_state"' in sql
    assert connection.commits == 1 and connection.rollbacks == 0


def test_bootstrap_builds_every_table_the_maintenance_catalog_promises():
    """A deployment must not advertise upkeep for a table it never built.

    `knowledgebase.schedule` is seeded from `db/sync/schedule.py:CATALOG`, and each offline-maintained
    entry names the `db/sync/<module>.py` that produces it. Those builders must all run in the
    bootstrap, or the catalog claims a table the community database does not have.
    """
    modules = {step[2] for step in bootstrap_module.command_plan()}
    missing = {}
    for entry in schedule_module.CATALOG:
        for builder in re.findall(r"db/sync/([a-z0-9_]+)\.py", entry.note):
            if f"db.sync.{builder}" not in modules:
                missing[entry.table_name] = builder
    assert not missing, f"catalog tables whose builder the bootstrap never runs: {missing}"


def test_bootstrap_replays_and_abstains_after_ready_version():
    class Cursor:
        def __init__(self, statements):
            self.statements = statements

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, statement, params=None):
            self.statements.append((str(statement), params))

    class Connection:
        def __init__(self):
            self.statements = []
            self.commits = 0

        def cursor(self):
            return Cursor(self.statements)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.statements.append(("ROLLBACK", None))

    originals = (
        bootstrap_module._initialize_database,
        bootstrap_module._ready,
        bootstrap_module._mark,
        bootstrap_module._grant_serving_access,
    )
    events = []
    ready = False
    try:
        bootstrap_module._initialize_database = lambda connection: events.append("initialize")
        bootstrap_module._ready = lambda connection: ready
        bootstrap_module._mark = lambda connection, status, error=None: events.append(
            (status, error)
        )
        bootstrap_module._grant_serving_access = (
            lambda connection, role, datasets: events.append((role, datasets))
        )
        connection = Connection()
        ran = []
        assert bootstrap_module.bootstrap(
            connection, "serving", runner=lambda command: ran.append(tuple(command))
        )
        assert tuple(ran) == bootstrap_module.command_plan()
        assert ("running", None) in events and ("ready", None) in events
        assert ("serving", frozenset({"iana_country"})) in events

        ready = True
        events.clear()
        ran.clear()
        assert not bootstrap_module.bootstrap(
            connection, "serving", runner=lambda command: ran.append(tuple(command))
        )
        assert ran == []
        assert events == ["initialize"]
        assert any("pg_advisory_lock" in statement for statement, _ in connection.statements)
        assert any("pg_advisory_unlock" in statement for statement, _ in connection.statements)
    finally:
        (
            bootstrap_module._initialize_database,
            bootstrap_module._ready,
            bootstrap_module._mark,
            bootstrap_module._grant_serving_access,
        ) = originals


def test_bootstrap_records_failure_and_rejects_privileged_serving_role():
    original_initialize = bootstrap_module._initialize_database
    original_ready = bootstrap_module._ready
    original_mark = bootstrap_module._mark
    try:
        bootstrap_module._initialize_database = lambda connection: None
        bootstrap_module._ready = lambda connection: False
        marks = []
        bootstrap_module._mark = lambda connection, status, error=None: marks.append((status, error))

        class Connection:
            class Cursor:
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def execute(self, *_): return None

            def cursor(self): return self.Cursor()
            def commit(self): return None
            def rollback(self): return None

        connection = Connection()
        try:
            bootstrap_module.bootstrap(
                connection,
                "serving",
                runner=lambda command: (_ for _ in ()).throw(RuntimeError("source unavailable")),
            )
        except RuntimeError as exc:
            assert str(exc) == "source unavailable"
        else:
            raise AssertionError("failed bootstrap was accepted")
        assert marks == [("running", None), ("failed", "source unavailable")]

        try:
            bootstrap_module.bootstrap(connection, "postgres")
        except ValueError:
            pass
        else:
            raise AssertionError("postgres was accepted as the serving role")
    finally:
        bootstrap_module._initialize_database = original_initialize
        bootstrap_module._ready = original_ready
        bootstrap_module._mark = original_mark


def test_public_deployer_has_isolated_state_and_cost_safe_defaults():
    versions = _text("infra/versions.tf")
    assert 'backend "gcs" {}' in versions
    assert "prereasoner-inference-tfstate" not in versions

    deploy = _text("deploy/gcp/deploy.sh")
    for required in (
        "-backend-config=\"bucket=${STATE_BUCKET}\"",
        "-backend-config=\"prefix=${STATE_PREFIX}\"",
        "-var=db_availability_type=ZONAL",
        "-var=min_instances=0",
        "-var=enable_external_llm=false",
        "-var=chat_llm_provider=gemini",
        "-var=gemini_model=gemini-3.8-flash",
        "community-seed-v4.dump",
        "db.sync.community_seed_import",
        "-var=enrichment_active_datasets=iana_country",
        "image_summary.digest",
        "@${digest}",
        "terraform -chdir=\"$ROOT/infra\" plan",
        "Temporary Prereasoner database bootstrap",
        "cleanup_bootstrap_identity",
        'build_service_account="${build_service_account##*/}"',
        "deploy/gcp/build_context.py --output",
        'status --porcelain --untracked-files=all',
        "engine.release_smoke",
        "--datasets,iana_country",
        'expected 401',
        "--target chat",
        "--target hosting",
        "cloudbuild.hosting.yaml",
        "roles/firebase.admin",
        "HOSTING_SITE_ID",
        "_HOSTING_SITE=${HOSTING_SITE}",
        "firebasehosting.googleapis.com/v1beta1/projects/${PROJECT_ID}/sites/${HOSTING_SITE}",
    ):
        assert required in deploy
    assert "prompt_and_store_chat_key" not in deploy
    assert "cleanup_chat_secret" not in deploy
    assert "Type %s to continue" in deploy
    assert "gcloud auth login --update-adc" in deploy
    assert "--allow-unauthenticated" not in deploy
    seed_import = _text("db/sync/community_seed_import.py")
    assert '"SET transaction_timeout = 0;"' in seed_import
    assert '"--clean", "--if-exists"' in seed_import
    assert 'startswith(b"\\\\restrict")' in seed_import
    assert 'startswith(b"\\\\unrestrict")' in seed_import
    assert 'b"DROP SCHEMA PUBLIC"' in seed_import
    assert 'b"CREATE SCHEMA PUBLIC"' in seed_import
    assert 'b"DROP SCHEMA IF EXISTS PUBLIC"' in seed_import
    assert 'b"CREATE SCHEMA IF NOT EXISTS PUBLIC"' in seed_import
    assert 'b"DROP SCHEMA KNOWLEDGEBASE"' in seed_import
    assert 'b"CREATE SCHEMA KNOWLEDGEBASE"' in seed_import
    assert '"psql", "--set=ON_ERROR_STOP=1"' in seed_import
    hosting = _text("cloudbuild.hosting.yaml")
    assert "hosting:sites:create \"${_HOSTING_SITE}\"" in hosting
    assert "site.site = process.env.HOSTING_SITE" in hosting
    assert "firebase deploy" in hosting and "--only=hosting" in hosting
    terraform = _text("infra/main.tf")
    assert 'resource "google_cloud_run_v2_job" "retention_cleanup"' in terraform
    assert 'command = ["python", "-m", "engine.retention_cleanup"]' in terraform
    retention = terraform.split(
        'resource "google_cloud_run_v2_job" "retention_cleanup"', 1
    )[1].split('resource "google_cloud_run_v2_job_iam_member"', 1)[0]
    for dependency in (
        "google_project_iam_member.run_cloudsql",
        "google_secret_manager_secret_iam_member.run_serving_db_password",
        "google_secret_manager_secret_version.serving_db_password",
        "google_project_iam_member.run_rtdb",
    ):
        assert dependency in retention
    artifact_registry = terraform.split(
        'resource "google_artifact_registry_repository" "engine"', 1
    )[1].split('resource "google_sql_database_instance"', 1)[0]
    assert "run_cloudsql" not in artifact_registry

    ci = _text(".github/workflows/ci.yml")
    assert "sed -i 's/backend \"gcs\" {}/backend \"local\" {}/'" in ci
    assert 'zz_ci_override.tf' not in ci


def test_release_smoke_rejects_a_non_reasoning_or_wrong_numeric_answer():
    from engine.release_smoke import _assert_reasoning_result

    _assert_reasoning_result({"result": {"rows": [["3.3"]]}})
    for result in (
        {"error": "planner failed", "result": {"rows": [["3.3"]]}},
        {"clarify": "which amount?", "result": {"rows": [["3.3"]]}},
        {"result": {"rows": [["3.3000000000000003"]]}},
    ):
        try:
            _assert_reasoning_result(result)
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid release-smoke answer was accepted")


def test_release_smoke_checks_current_chat_migration():
    smoke = _text("engine/release_smoke.py")
    for column in ("source_bytes", "state_bytes", "last_active_at", "expires_at"):
        assert column in smoke
    assert "INSERT INTO chat.conversation" in smoke
    assert "DELETE FROM chat.conversation" in smoke


def test_serving_identity_cannot_read_the_admin_database_secret():
    terraform = _text("infra/main.tf")
    assert 'resource "google_secret_manager_secret_iam_member" "run_db_password"' not in terraform
    sync_binding = terraform.split(
        'resource "google_secret_manager_secret_iam_member" "sync_db_password"', 1
    )[1].split("}", 1)[0]
    assert "google_service_account.sync.email" in sync_binding
    api = terraform.split('resource "google_cloud_run_v2_service" "api"', 1)[1].split(
        'resource "google_cloud_run_v2_service_iam_member"', 1
    )[0]
    assert "google_secret_manager_secret.db_password.secret_id" not in api


def test_public_build_needs_no_hugging_face_secret():
    dockerfile = _text("Dockerfile")
    cloudbuild = _text("cloudbuild.yaml")
    assert "id=hf_token" not in dockerfile
    assert "secretEnv: ['HF_TOKEN']" not in cloudbuild
    assert "availableSecrets:" not in cloudbuild
    assert "engine.fetch_weights" in _text("deploy/gcp/deploy.sh")


def test_chat_image_copy_list_and_build_context_agree():
    """Regression for an OBSERVED two-build failure (2026-09-07): engine/request_timing.py was added
    to Dockerfile.orchestrator's COPY but not deploy/gcp/build_context.py's CHAT_ALLOWLIST, so the
    docker build died on COPY inside Cloud Build. The two curated lists describe the SAME lean image
    and must name the same engine modules; either one drifting breaks the build (best case) or boots
    a crashing container (worst case, if COPY silently succeeded on a stale context)."""
    import re
    dockerfile = _text("Dockerfile.orchestrator")
    copy_line = next(line for line in dockerfile.splitlines()
                     if line.startswith("COPY engine/") and "/app/engine/" in line)
    copied = set(re.findall(r"engine/[a-z_]+\.py", copy_line))
    from deploy.gcp.build_context import SOURCE_CHAT_ALLOWLIST
    allowed = {entry for entry in SOURCE_CHAT_ALLOWLIST if entry.startswith("engine/")}
    assert copied == allowed, (
        f"Dockerfile.orchestrator COPY and build_context SOURCE_CHAT_ALLOWLIST disagree — "
        f"only in COPY: {sorted(copied - allowed)}; only in allowlist: {sorted(allowed - copied)}")


def test_uninstall_actually_removes_the_billable_deployment():
    """Regression for an OBSERVED uninstall failure (2026-09-16): `deploy.sh --destroy` aborted
    with three errors and left a running db-perf-optimized Cloud SQL instance behind, after
    telling the operator the deployment was removed. Three independent causes:

    1. Terraform reads `deletion_protection` from STATE, so passing -var=deletion_protection=false
       to `destroy` alone leaves the guard armed -- it must be applied first.
    2. `google_sql_user` tried to DROP ROLE ahead of the instance, which PostgreSQL refuses while
       the seeded database still has objects owned by that role (362 for postgres, 97 for serving).
    3. After a partial destroy the root outputs are gone, so the `image` output guard reported
       "no deployment exists" and refused to clean up the survivors.

    An uninstall that silently leaves the most expensive resource running is worse than one that
    fails loudly, so all three paths are contract-tested."""
    deploy = _text("deploy/gcp/deploy.sh")
    destroy = deploy.split("destroy_deployment() {", 1)[1].split("\n}", 1)[0]
    # (1) the protection-clearing apply must run BEFORE the destroy.
    apply_at = destroy.find('apply -auto-approve -input=false "${unprotect[@]}"')
    destroy_at = destroy.find('destroy -auto-approve -input=false "${variables[@]}"')
    assert apply_at > 0 and destroy_at > apply_at, \
        "the uninstall must clear deletion_protection with an apply before destroying"
    # (3) a missing output must not be read as "nothing to destroy".
    assert "state list" in destroy, \
        "the uninstall must fall back to remaining state when the outputs are gone"
    # (2) the roles are abandoned with the instance instead of being dropped first.
    terraform = _text("infra/main.tf")
    for role in ('resource "google_sql_user" "postgres"', 'resource "google_sql_user" "serving"'):
        block = terraform.split(role, 1)[1].split("\n}", 1)[0]
        assert 'deletion_policy = "ABANDON"' in block, f"{role} would block the instance delete"


def test_database_edition_is_stated_so_a_trial_is_not_20x_overprovisioned():
    """Regression for an OBSERVED cost-and-capacity defect (2026-09-16): `edition` was never set,
    so the API resolved it to ENTERPRISE_PLUS, which rejects every shared-core tier -- forcing
    db-perf-optimized-N-2 on a guided install that calls itself cost-aware. That is roughly 20x
    the machine the reference deployment actually serves the FULL world DB from, and its regional
    capacity shortage failed a fresh install with "The zone or region does not have sufficient
    resources"."""
    variables = _text("infra/variables.tf")
    main = _text("infra/main.tf")
    assert 'variable "db_edition"' in variables, "the edition must be explicit, never inferred"
    edition = variables.split('variable "db_edition"', 1)[1].split("\n}", 1)[0]
    assert 'default     = "ENTERPRISE"' in edition
    tier = variables.split('variable "db_tier"', 1)[1].split("\n}", 1)[0]
    assert "db-perf-optimized" not in tier, "a one-click trial must not default to Enterprise Plus"
    assert "edition           = var.db_edition" in main or "edition = var.db_edition" in main
    # The installer must not quietly reintroduce the expensive tier.
    assert "db-perf-optimized" not in _text("deploy/gcp/deploy.sh")


def test_first_install_survives_cloud_build_permission_propagation():
    """Regression for an OBSERVED first-install failure (2026-09-16): the installer enables the
    Cloud Build API and submits a build seconds later, and the submission was rejected with
    PERMISSION_DENIED before the grant became effective. The identical command succeeded minutes
    later with nothing changed, proving propagation rather than a missing role. A re-install never
    sees it because the API is already on -- the same blind spot as the bootstrap identity race.

    The retry must be narrow: a build that actually ran and FAILED has to surface immediately,
    never be silently run again."""
    deploy = _text("deploy/gcp/deploy.sh")
    assert "submit_build()" in deploy
    # Every real submission goes through the helper; only the helper calls gcloud directly.
    assert deploy.count("gcloud builds submit") == 1, \
        "a build submission bypasses the propagation retry"
    assert deploy.count("submit_build \"$") == 3, \
        "expected the engine, chat and hosting builds to share one submit path"
    # Narrow by construction: retry only on the propagation denial.
    assert "PERMISSION_DENIED" in deploy
    assert 'die "Cloud Build failed"' in deploy, \
        "a genuine build failure must abort instead of retrying"
    assert "tee" in deploy, "the live build log must keep streaming while failures are classified"


def test_first_install_waits_for_the_bootstrap_identity_to_resolve():
    """Regression for an OBSERVED first-install failure (2026-09-16): `gcloud iam
    service-accounts create` returns before the new identity resolves in the policy APIs, so the
    binding on the next line aborted the install with "Service account ... does not exist".

    Only a FIRST install is exposed, because a re-run finds the account already there and never
    waits. That is precisely the path every new user takes and the one path repeated test runs
    against an existing deployment never exercised."""
    deploy = _text("deploy/gcp/deploy.sh")
    assert "grant_bootstrap_role()" in deploy
    # Fold shell line continuations so each binding is one statement.
    joined = re.sub(r"\\\n\s*", " ", deploy)
    grants = [line.strip() for line in joined.splitlines()
              if "add-iam-policy-binding" in line and "${BOOTSTRAP_SA}" in line
              and "remove-iam-policy-binding" not in line]
    assert grants, "the bootstrap identity bindings disappeared"
    unguarded = [line for line in grants if not line.startswith("grant_bootstrap_role")]
    assert not unguarded, f"bootstrap role bound without the propagation retry: {unguarded}"
    assert len(grants) == 2, f"expected the Cloud SQL and secret bindings, got {grants}"
    # A bounded retry that still fails loudly, never an unbounded wait or a silent skip.
    assert 'die "could not grant' in deploy


def test_community_install_provisions_an_auth_provider_it_can_actually_enable():
    """A Community install must need ZERO console configuration, and Google sign-in cannot meet
    that bar. Measured on a fresh project (pr-ce-probe-0916, 2026-09-16):

      - enabling the google.com provider returns "INVALID_CONFIG : client_id cannot be empty";
      - creating a Web app does not auto-provision an OAuth client;
      - the only documented OAuth-client-creation API (IAP brands) answers
        "Project must belong to an organization", so a personal account cannot use it;
      - enabling ANONYMOUS sign-in is a single PATCH that succeeds.

    So the installer enables anonymous auth and tells the client to use it. The uid and ID token
    stay real, so tenant isolation and the engine's Bearer check are untouched."""
    release = _text("deploy/gcp/hosting_release.js")
    assert "signIn.anonymous.enabled" in release
    assert "identityPlatform:initializeAuth" in release, \
        "a provider cannot be enabled before Auth is initialized"
    assert 'export const AUTH_PROVIDER = "anonymous";' in release, \
        "the generated client config must select the provider the install actually enabled"
    # Enabling must be fatal, never best-effort: a deployment whose provider is off cannot sign in.
    assert "enabling anonymous sign-in failed" in release
    # The release stage must actually run it, and it must ship in the build context.
    assert "node deploy/gcp/hosting_release.js" in _text("cloudbuild.hosting.yaml")
    from deploy.gcp.build_context import SOURCE_HOSTING_ALLOWLIST
    assert "deploy/gcp/hosting_release.js" in SOURCE_HOSTING_ALLOWLIST, \
        "the release script would be missing from the Cloud Build source archive"

    config = _text("web/public/lib/config.js")
    # The reference deployment keeps Google sign-in; only the generated Community copy switches.
    assert 'export const AUTH_PROVIDER = "google";' in config
    # THE SUBTLE ONE: the hosting build rewrites exactly the span between these two markers. If
    # AUTH_PROVIDER ever drifts outside it, a Community install would silently publish "google"
    # and every deployment would be unable to sign in again -- the original launch blocker.
    start = config.index("const HOSTING_DOMAINS =")
    end = config.index("// Google Picker credentials", start)
    assert "AUTH_PROVIDER" in config[start:end], \
        "AUTH_PROVIDER sits outside the block the hosting release regenerates"

    client = _text("web/public/lib/firebase-init.js")
    assert "signInAnonymously" in client
    assert "AUTH_PROVIDER" in client, "the client must honour the provider the install enabled"


def test_hosting_release_authorizes_its_own_sign_in_domains():
    """Regression for an OBSERVED launch blocker (2026-09-16): the hosting release created
    <site>.web.app and pinned the client's authDomain to it, but never added that origin to
    Firebase Auth's authorized domains. Pressing Ask on community-v4-test2.web.app failed with
    auth/unauthorized-domain, so the default prompt could not run at all. A deployment-scoped
    site is ALWAYS a domain the project has never seen, so this broke every Community install;
    the reference deployment only worked because its domains were authorized by hand long ago."""
    release = _text("deploy/gcp/hosting_release.js")
    assert "identitytoolkit.googleapis.com/admin/v2/projects/" in release
    assert "?updateMask=authorizedDomains" in release
    assert 'method: "PATCH"' in release
    # PATCH replaces the whole list, so the install must merge and never revoke an origin it did
    # not create (a custom domain, localhost, or another deployment's site).
    assert "trusted.concat(missing)" in release
    # ONE definition of this deployment's origins feeds BOTH the authorization and the client
    # config; a second copy would let the trusted set and the pinned authDomain drift apart.
    assert release.count('[hostingSite + ".web.app", hostingSite + ".firebaseapp.com"]') == 1
    assert "JSON.stringify(hostingDomains)" in release
    # Publishing a UI that cannot sign in is worse than failing the release.
    assert "process.exit(1)" in release


def test_cloud_build_steps_stay_within_the_argument_limit():
    """Regression for an OBSERVED release failure (2026-09-16): a Cloud Build step argument is
    capped at 10000 characters, and the hosting step's inline script grew to 10256 -- so the
    release died with "build step 0 arg 1 too long" AFTER the 35-minute seed restore had already
    run. Inline scripts have no natural pressure to stay small, so the ceiling is asserted here
    rather than rediscovered at the most expensive possible moment."""
    import yaml

    limit = 10000
    for name in ("cloudbuild.yaml", "cloudbuild.hosting.yaml", "cloudbuild.orchestrator.yaml"):
        document = yaml.safe_load(_text(name))
        for index, step in enumerate(document.get("steps", [])):
            for position, argument in enumerate(step.get("args", []) or []):
                assert len(str(argument)) < limit, (
                    f"{name} step {index} arg {position} is {len(str(argument))} chars "
                    f"(limit {limit}); move the script into a file like "
                    f"deploy/gcp/hosting_release.js instead of growing it inline")


def test_marketing_button_opens_the_pinned_public_walkthrough():
    button = _text("deploy/gcp/button.html")
    start = button.index('href="') + len('href="')
    href = button[start:button.index('"', start)].replace("&amp;", "&")
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "shell.cloud.google.com"
    assert query["cloudshell_git_repo"] == [
        "https://github.com/ManiDoraisamy/prereasoner-data"
    ]
    assert query["cloudshell_git_branch"] == ["v0.2.21"]
    assert query["cloudshell_tutorial"] == ["deploy/gcp/cloudshell-tutorial.md"]
    assert 'target="_blank"' in button and 'rel="noopener noreferrer"' in button
    assert href in _text("README.md")


TESTS = [
    test_bootstrap_plan_is_minimal_deterministic_and_non_shell,
    test_state_projection_builder_does_not_pull_the_model_runtime,
    test_state_projection_rebuild_is_atomic_and_reports_unresolved_rows,
    test_bootstrap_builds_every_table_the_maintenance_catalog_promises,
    test_bootstrap_replays_and_abstains_after_ready_version,
    test_bootstrap_records_failure_and_rejects_privileged_serving_role,
    test_public_deployer_has_isolated_state_and_cost_safe_defaults,
    test_release_smoke_rejects_a_non_reasoning_or_wrong_numeric_answer,
    test_release_smoke_checks_current_chat_migration,
    test_serving_identity_cannot_read_the_admin_database_secret,
    test_public_build_needs_no_hugging_face_secret,
    test_chat_image_copy_list_and_build_context_agree,
    test_community_install_provisions_an_auth_provider_it_can_actually_enable,
    test_uninstall_actually_removes_the_billable_deployment,
    test_database_edition_is_stated_so_a_trial_is_not_20x_overprovisioned,
    test_first_install_survives_cloud_build_permission_propagation,
    test_first_install_waits_for_the_bootstrap_identity_to_resolve,
    test_hosting_release_authorizes_its_own_sign_in_domains,
    test_cloud_build_steps_stay_within_the_argument_limit,
    test_marketing_button_opens_the_pinned_public_walkthrough,
]


def main() -> None:
    failures = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\ncommunity deploy: {len(TESTS) - len(failures)} passed, {len(failures)} failed")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
