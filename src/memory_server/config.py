import os
from dataclasses import dataclass


def _setting(env, name, required=False):
    """Read NAME, or the contents of the file named by NAME_FILE (container secrets). Never both."""
    direct, path = env.get(name), env.get(name + "_FILE")
    if direct and path:
        raise SystemExit("set only one of %s and %s_FILE" % (name, name))
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                direct = fh.read().strip()
        except OSError:
            raise SystemExit("cannot read the file named by %s_FILE" % name)
    if required and not direct:
        raise SystemExit("%s (or %s_FILE) is required" % (name, name))
    return direct


def _csv(value):
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


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
    lease_seconds: int = 30
    heartbeat_seconds: int = 10
    max_job_attempts: int = 3
    # HTTP edge (P0-5). Empty allowed_hosts means "no Host check", which from_env never produces for a server.
    allowed_hosts: tuple = ()
    allowed_origins: tuple = ()
    policy_ledger: str = ""
    require_ledger: bool = False
    auth_fail_limit: int = 20
    auth_fail_window_seconds: int = 60
    request_limit_per_minute: int = 600
    request_timeout_seconds: int = 30

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        url = _setting(env, "MEMORY_DATABASE_URL", required=True)
        key = _setting(env, "MEMORY_HMAC_KEY", required=True)
        if len(key) < 32:
            raise SystemExit("MEMORY_HMAC_KEY must be at least 32 characters")
        extra = {}
        for field, name in (("auth_fail_limit", "MEMORY_AUTH_FAIL_LIMIT"),
                            ("request_limit_per_minute", "MEMORY_REQUEST_LIMIT_PER_MINUTE")):
            if env.get(name):
                extra[field] = int(env[name])
        require = env.get("MEMORY_REQUIRE_LEDGER", "") in ("1", "true", "yes")
        if require and not env.get("MEMORY_POLICY_LEDGER"):
            raise SystemExit("MEMORY_REQUIRE_LEDGER is set but MEMORY_POLICY_LEDGER is empty: refusing to start "
                             "without the policy ledger that makes restores safe")
        return cls(database_url=url, require_ledger=require, hmac_key=key.encode("utf-8"),
                   allowed_hosts=_csv(env.get("MEMORY_ALLOWED_HOSTS")),
                   allowed_origins=_csv(env.get("MEMORY_ALLOWED_ORIGINS")),
                   policy_ledger=env.get("MEMORY_POLICY_LEDGER", ""), **extra)
