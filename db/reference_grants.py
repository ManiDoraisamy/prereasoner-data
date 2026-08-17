"""Least-privilege grants and write-denial audit for activated reference datasets."""
from __future__ import annotations

import argparse
import re
from collections import defaultdict

from psycopg2 import errors, sql

from db.sync._conn import connect
from engine.enrichment.registry import Activation, PostgresStorage, REGISTRY


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


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
        relation = definition.storage.relation
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
    parser.add_argument("--datasets", required=True, help="comma-separated code-approved datasets")
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
