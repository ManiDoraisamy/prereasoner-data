"""Disposable PostgreSQL schemas for serving-faithful live tests.

Production requests use validated ``c_<32 hex>`` conversation schemas created by
the non-superuser serving role. Live tests must exercise that contract too: fixed
developer schema names can be left behind under an admin owner and turn a healthy
request path into a misleading permission failure.
"""
from __future__ import annotations

import atexit
import os
import uuid
from dataclasses import dataclass


@dataclass
class LiveSchemaLease:
    name: str
    managed: bool
    _closed: bool = False

    def close(self) -> None:
        """Drop only a schema allocated by this process; never touch an override."""
        if self._closed or not self.managed:
            return
        self._closed = True
        from engine.pg import _pg
        from engine.tables import qident

        connection = _pg()
        try:
            cursor = connection.cursor()
            cursor.execute(f"DROP SCHEMA IF EXISTS {qident(self.name)} CASCADE")
            connection.commit()
        finally:
            connection.close()


def live_schema(env_name: str = "AUTH_TEST_SUB") -> LiveSchemaLease:
    """Return an explicit test schema or allocate a disposable production-shaped one."""
    configured = os.environ.get(env_name)
    lease = LiveSchemaLease(configured or f"c_{uuid.uuid4().hex}", not configured)
    if lease.managed:
        atexit.register(lease.close)
    return lease
