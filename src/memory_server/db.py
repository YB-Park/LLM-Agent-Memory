import logging

import psycopg2
import psycopg2.errors
import psycopg2.extras

from .errors import ServiceError

log = logging.getLogger("memory_server.db")

_RETRYABLE = (
    psycopg2.errors.LockNotAvailable,
    psycopg2.errors.DeadlockDetected,
    psycopg2.errors.SerializationFailure,
)


CONNECT_TIMEOUT_SECONDS = 3  # a stopped database host must fail fast (retryable 503), not hang the request


def connect(dsn):
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor,
                            connect_timeout=CONNECT_TIMEOUT_SECONDS)
    conn.set_client_encoding("UTF8")
    return conn


def run_tx(config, fn):
    """Run fn(cursor) in one READ COMMITTED transaction with short timeouts.

    Lock timeouts and deadlocks roll back the whole transaction and are retried
    a bounded number of times (§5.1). Other DB failures map to a retryable 503.
    """
    attempts = 0
    while True:
        attempts += 1
        conn = None
        try:
            conn = connect(config.database_url)
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = %s", ("%dms" % config.lock_timeout_ms,))
                cur.execute("SET LOCAL statement_timeout = %s", ("%dms" % config.statement_timeout_ms,))
                result = fn(cur)
            conn.commit()
            return result
        except _RETRYABLE:
            if conn is not None:
                conn.rollback()
            if attempts >= config.db_retries:
                raise ServiceError(503, "DB_BUSY", "일시적으로 처리할 수 없습니다. 같은 요청으로 다시 시도하세요.",
                                   retryable=True, retry_after_ms=1000)
        except ServiceError:
            if conn is not None:
                conn.rollback()
            raise
        except psycopg2.OperationalError:
            log.warning("database unavailable")
            raise ServiceError(503, "DB_UNAVAILABLE", "저장소에 연결할 수 없습니다. 같은 요청으로 다시 시도하세요.",
                               retryable=True, retry_after_ms=1000)
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()
