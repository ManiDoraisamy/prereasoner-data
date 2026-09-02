"""Cross-instance request quotas for paid external processing.

The serving role receives DML only on the two ``chat`` budget tables. PostgreSQL
advisory transaction locks serialize one operation's short accounting transaction;
the external call itself never holds a database lock or connection.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from engine.request_limits import RequestLease


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetPolicy:
    user_requests_per_minute: int
    global_requests_per_minute: int
    user_in_flight: int
    global_in_flight: int
    lease_seconds: int = 300
    user_requests_per_day: int = 1000
    global_requests_per_day: int = 10000


class PostgresRequestBudget:
    def __init__(self, connect: Callable, policies: dict[str, BudgetPolicy]):
        self._connect = connect
        self._policies = dict(policies)

    @staticmethod
    def _subject_key(subject: str) -> str:
        return hashlib.sha256(subject.encode("utf-8")).hexdigest()

    def acquire(self, subject: str, operation: str) -> tuple[RequestLease | None, int, str | None]:
        policy = self._policies[operation]
        subject_key = self._subject_key(subject)
        lease_id = uuid.uuid4().hex
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"paid-budget:{operation}",))
            cursor.execute(
                "DELETE FROM chat.request_lease WHERE expires_at <= clock_timestamp(); "
                "DELETE FROM chat.request_usage WHERE bucket_start < date_trunc('day', clock_timestamp()) "
                "- interval '31 days'"
            )
            periods = (
                ("minute", "minute", policy.user_requests_per_minute,
                 policy.global_requests_per_minute, 60),
                ("day", "day", policy.user_requests_per_day,
                 policy.global_requests_per_day, 86400),
            )
            for period, truncation, user_limit, global_limit, retry in periods:
                cursor.execute(
                    "SELECT subject_key, request_count FROM chat.request_usage "
                    f"WHERE period=%s AND operation=%s AND bucket_start=date_trunc('{truncation}', "
                    "clock_timestamp()) AND subject_key IN (%s,%s)",
                    (period, operation, subject_key, "__global__"),
                )
                counts = dict(cursor.fetchall())
                if counts.get(subject_key, 0) >= user_limit:
                    connection.rollback()
                    return None, retry, f"user_{period}_rate"
                if counts.get("__global__", 0) >= global_limit:
                    connection.rollback()
                    return None, retry, f"global_{period}_rate"
            cursor.execute(
                "SELECT count(*) FILTER (WHERE subject_key=%s), count(*) "
                "FROM chat.request_lease WHERE operation=%s AND expires_at > clock_timestamp()",
                (subject_key, operation),
            )
            user_active, global_active = cursor.fetchone()
            if user_active >= policy.user_in_flight:
                connection.rollback()
                return None, 1, "user_concurrency"
            if global_active >= policy.global_in_flight:
                connection.rollback()
                return None, 1, "global_concurrency"
            for period, truncation, _user_limit, _global_limit, _retry in periods:
                for key in (subject_key, "__global__"):
                    cursor.execute(
                        "INSERT INTO chat.request_usage(period,bucket_start,subject_key,operation,request_count) "
                        f"VALUES (%s,date_trunc('{truncation}',clock_timestamp()),%s,%s,1) "
                        "ON CONFLICT (period,bucket_start,subject_key,operation) DO UPDATE "
                        "SET request_count=chat.request_usage.request_count+1",
                        (period, key, operation),
                    )
            cursor.execute(
                "INSERT INTO chat.request_lease(lease_id,subject_key,operation,expires_at) "
                "VALUES (%s,%s,%s,clock_timestamp()+(%s * interval '1 second'))",
                (lease_id, subject_key, operation, policy.lease_seconds),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        def release() -> None:
            connection = self._connect()
            try:
                cursor = connection.cursor()
                cursor.execute("DELETE FROM chat.request_lease WHERE lease_id=%s", (lease_id,))
                connection.commit()
            except Exception:
                connection.rollback()
                LOG.exception("paid request lease %s could not be released", lease_id)
            finally:
                connection.close()

        return RequestLease(release), 0, None
