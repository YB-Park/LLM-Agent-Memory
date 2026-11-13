import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    database_url: str
    hmac_key: bytes
    max_text_bytes: int = 65536
    max_request_bytes: int = 96 * 1024
    max_metadata_bytes: int = 4096
    max_retained_per_namespace: int = 1000
    max_open_jobs: int = 10000
    lock_timeout_ms: int = 1000
    statement_timeout_ms: int = 2000
    db_retries: int = 3

    @classmethod
    def from_env(cls):
        url = os.environ.get("MEMORY_DATABASE_URL")
        key = os.environ.get("MEMORY_HMAC_KEY")
        if not url:
            raise SystemExit("MEMORY_DATABASE_URL is required")
        if not key or len(key) < 32:
            raise SystemExit("MEMORY_HMAC_KEY must be set (>= 32 characters)")
        return cls(database_url=url, hmac_key=key.encode("utf-8"))
