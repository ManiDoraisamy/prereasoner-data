"""Least-privilege serving grants and write-denial audit for reference datasets."""
from __future__ import annotations

import argparse
import re
from collections import defaultdict

from psycopg2 import errors, sql

from db.sync._conn import connect
from engine.enrichment.registry import Activation, PostgresStorage, REGISTRY


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
_CHAT_TABLES = (
    "user_profile", "conversation", "user_conversation", "request_usage", "request_lease",
)

# Older deployments exposed these admin-owned functions to the serving role for
# request-time Wikidata fill. Serving is now network-free and read-only against
# shared facts; bootstrap revokes any grant left by an older release.
_LEGACY_LAZY_FILL_FUNCTIONS = (
    "knowledgebase.lazy_ensure_table(text, text[])",
    "knowledgebase.lazy_upsert_entity(text, text[], text[])",
    "knowledgebase.lazy_register_word(text, text, text, text, text, text)",
)


def apply_chat_grants(cur, runtime_role: str) -> None:
    """Grant the serving role only the chat DML it needs after admin migration."""
    if not _IDENTIFIER.fullmatch(runtime_role or ""):
        raise ValueError("runtime_role must be a lowercase PostgreSQL identifier")
    role_id = sql.Identifier(runtime_role)
    cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
        sql.Identifier("chat"), role_id,
    ))
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
        sql.Identifier("chat"), role_id,
    ))
    for table_name in _CHAT_TABLES:
        table = sql.SQL("{}.{}").format(sql.Identifier("chat"), sql.Identifier(table_name))
        cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}").format(
            table, role_id,
        ))

    cur.execute(
        "SELECT has_schema_privilege(%s, 'chat', 'USAGE'), "
        "has_schema_privilege(%s, 'chat', 'CREATE')",
        (runtime_role, runtime_role),
    )
    has_usage, has_create = cur.fetchone()
    if not has_usage or has_create:
        raise RuntimeError(f"chat schema privilege audit failed for {runtime_role}")
    for table_name in _CHAT_TABLES:
        qualified = f"chat.{table_name}"
        cur.execute(
            "SELECT has_table_privilege(%s,%s,'SELECT,INSERT,UPDATE,DELETE'), "
            "has_table_privilege(%s,%s,'TRUNCATE,REFERENCES,TRIGGER')",
            (runtime_role, qualified, runtime_role, qualified),
        )
        can_dml, can_escalate = cur.fetchone()
        if not can_dml or can_escalate:
            raise RuntimeError(f"chat privilege audit failed for {runtime_role} on {qualified}")


def apply_shared_read_boundary(cur, runtime_role: str) -> None:
    """Revoke legacy write functions and prove shared serving data is read-only."""
    if not _IDENTIFIER.fullmatch(runtime_role or ""):
        raise ValueError("runtime_role must be a lowercase PostgreSQL identifier")
    role_id = sql.Identifier(runtime_role)
    for signature in _LEGACY_LAZY_FILL_FUNCTIONS:
        cur.execute("SELECT to_regprocedure(%s)", (signature,))
        if cur.fetchone()[0] is not None:
            cur.execute(sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM {}").format(
                sql.SQL(signature), role_id,
            ))
    for signature in _LEGACY_LAZY_FILL_FUNCTIONS:
        cur.execute("SELECT to_regprocedure(%s)", (signature,))
        if cur.fetchone()[0] is None:
            continue
        cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                    (runtime_role, signature))
        if cur.fetchone()[0]:
            raise RuntimeError(
                f"legacy shared-write function still executable by {runtime_role}: {signature}")
    cur.execute(
        "SELECT has_schema_privilege(%s, 'knowledgebase', 'CREATE'), "
        "has_table_privilege(%s, 'knowledgebase.words', 'INSERT,UPDATE,DELETE')",
        (runtime_role, runtime_role),
    )
    can_create, can_write = cur.fetchone()
    if can_create or can_write:
        raise RuntimeError(f"knowledgebase direct-write audit failed for {runtime_role}")

    # The serving freshness guard READS the maintenance catalog on the world path, so the grant is
    # required — but it must stay SELECT-only: only the sync jobs record a refresh.
    cur.execute("SELECT to_regclass('knowledgebase.schedule')")
    if cur.fetchone()[0] is None:
        raise RuntimeError("knowledgebase.schedule is missing; run db.sync.app_migrations first")
    cur.execute(sql.SQL('GRANT SELECT ON TABLE knowledgebase."schedule" TO {}').format(role_id))
    cur.execute(
        "SELECT has_table_privilege(%s,'knowledgebase.schedule','SELECT'), "
        "has_table_privilege(%s,'knowledgebase.schedule','INSERT,UPDATE,DELETE')",
        (runtime_role, runtime_role),
    )
    can_read, can_write = cur.fetchone()
    if not can_read or can_write:
        raise RuntimeError(f"schedule privilege audit failed for {runtime_role}")


def approved_reference_targets(dataset_names, registry=REGISTRY) -> dict[str, tuple[str, ...]]:
    """Return only the physical relations needed by code-approved datasets."""
    if isinstance(dataset_names, str):
        raise ValueError("dataset_names must be a collection")
    grouped: dict[str, set[str]] = defaultdict(set)
    for name in sorted(set(dataset_names)):
        definition = registry.get(name)
        if definition is None:
            raise ValueError(f"unknown enrichment dataset: {name}")
        if definition.activation != Activation.ACTIVE:
            raise ValueError(f"dataset is not code-approved for activation: {name}")
        if not isinstance(definition.storage, PostgresStorage):
            raise ValueError(f"activated dataset is not PostgreSQL-backed: {name}")
        relations = (definition.storage.relation, *definition.storage.related_relations)
        for relation in relations:
            grouped[relation.schema_name].update(("release", relation.table_name))
    return {schema: tuple(sorted(tables)) for schema, tables in sorted(grouped.items())}


def apply_reference_grants(cur, runtime_role: str, dataset_names) -> dict[str, tuple[str, ...]]:
    """Grant SELECT only, then prove the runtime role cannot issue a write."""
    if not _IDENTIFIER.fullmatch(runtime_role or ""):
        raise ValueError("runtime_role must be a lowercase PostgreSQL identifier")
    targets = approved_reference_targets(dataset_names)
    cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname=%s", (runtime_role,))
    role = cur.fetchone()
    if role is None:
        raise ValueError(f"PostgreSQL role does not exist: {runtime_role}")
    if role[0]:
        raise ValueError("the serving role must not be a superuser")

    role_id = sql.Identifier(runtime_role)
    apply_chat_grants(cur, runtime_role)
    apply_shared_read_boundary(cur, runtime_role)
    if not targets:
        return targets
    for schema_name, tables in targets.items():
        schema_id = sql.Identifier(schema_name)
        cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(schema_id, role_id))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_id, role_id))
        for table_name in tables:
            relation = sql.SQL("{}.{}").format(schema_id, sql.Identifier(table_name))
            cur.execute(sql.SQL("REVOKE {} ON TABLE {} FROM {}").format(
                sql.SQL(", ").join(map(sql.SQL, _WRITE_PRIVILEGES)), relation, role_id,
            ))
            cur.execute(sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(relation, role_id))

    for schema_name, tables in targets.items():
        for table_name in tables:
            qualified = f"{schema_name}.{table_name}"
            cur.execute(
                "SELECT has_table_privilege(%s,%s,'SELECT'), "
                "has_table_privilege(%s,%s,'INSERT,UPDATE,DELETE,TRUNCATE')",
                (runtime_role, qualified, runtime_role, qualified),
            )
            can_read, can_write = cur.fetchone()
            if not can_read or can_write:
                raise RuntimeError(f"reference privilege audit failed for {runtime_role} on {qualified}")

    # PostgreSQL checks DELETE permission even when the predicate cannot match a row.
    schema_name = next(iter(targets))
    table_name = targets[schema_name][0]
    cur.execute("SAVEPOINT reference_write_probe")
    try:
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(role_id))
        cur.execute(sql.SQL("DELETE FROM {}.{} WHERE false").format(
            sql.Identifier(schema_name), sql.Identifier(table_name),
        ))
    except errors.InsufficientPrivilege:
        cur.execute("ROLLBACK TO SAVEPOINT reference_write_probe")
        cur.execute("RESET ROLE")
    else:
        cur.execute("ROLLBACK TO SAVEPOINT reference_write_probe")
        cur.execute("RESET ROLE")
        raise RuntimeError("negative write-permission probe unexpectedly succeeded")
    return targets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, help="existing non-superuser serving role")
    parser.add_argument("--datasets", default="", help="comma-separated code-approved datasets")
    args = parser.parse_args()
    datasets = frozenset(name.strip() for name in args.datasets.split(",") if name.strip())
    connection = connect()
    try:
        with connection.cursor() as cur:
            targets = apply_reference_grants(cur, args.role, datasets)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(f"reference grants verified for {args.role}: {targets}")


if __name__ == "__main__":
    main()
