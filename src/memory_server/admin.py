"""Administrator CLI for bootstrap operations (§4.1, §9.3). Talks to the DB directly.

Usage: python3 -m memory_server.admin <command> [options]
  create-namespace --name NAME
  create-principal --name NAME [--kind device|admin]   (prints the token once)
  grant --principal ID --namespace ID [--read] [--write] [--manage]
  delete-experience --experience ID          (tombstone source, invalidate derived results in all generations)
  revoke-principal --principal ID            (credential revoked, its open jobs cancelled, shared data kept)
  retry-job --job ID --expected-attempt N --principal ID   (principal needs manage on the job's namespace)
"""
import argparse
import json
import secrets
import sys
import uuid

from psycopg2.extras import Json

from .config import Config
from .errors import ServiceError
from .engine import reference_spec, spec_digest
from .db import connect
from .operations import delete_experience, fence_principal_jobs, retry_job, revoke_principal
from .service import credential_digest



def _tx(dsn, fn):
    conn = connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM system_control WHERE id = 1 FOR UPDATE")
            out = fn(cur)
        conn.commit()
        return out
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_namespace(dsn, name, engine_version="1", index_profile="reference-keywords/v1"):
    """Namespace -> empty servable generation -> active pointer, in one transaction (§4.2)."""
    ns_id, gen_id = str(uuid.uuid4()), str(uuid.uuid4())
    spec = reference_spec(engine_version)

    def fn(cur):
        cur.execute("INSERT INTO namespaces (id, name) VALUES (%s, %s)", (ns_id, name))
        cur.execute("INSERT INTO generations (id, namespace_id, engine_spec, spec_digest, index_profile, state, "
                    "snapshot_ingest_revision) VALUES (%s, %s, %s, %s, %s, 'servable', 0)",
                    (gen_id, ns_id, Json(spec), spec_digest(spec), index_profile))
        cur.execute("UPDATE namespaces SET active_generation_id = %s WHERE id = %s", (gen_id, ns_id))
        cur.execute("INSERT INTO audit_events (event_type, object_ids) VALUES ('namespace_created', %s)",
                    (Json({"namespace_id": ns_id, "generation_id": gen_id}),))
        return {"namespace_id": ns_id, "generation_id": gen_id}

    return _tx(dsn, fn)


def create_principal(dsn, name, kind="device"):
    principal_id = str(uuid.uuid4())
    token = "mem_" + secrets.token_urlsafe(32)

    def fn(cur):
        cur.execute("INSERT INTO principals (id, name, credential_digest, kind) VALUES (%s, %s, %s, %s)",
                    (principal_id, name, credential_digest(token), kind))
        cur.execute("INSERT INTO audit_events (event_type, object_ids) VALUES ('principal_created', %s)",
                    (Json({"principal_id": principal_id}),))
        return {"principal_id": principal_id, "token": token}

    return _tx(dsn, fn)


def grant(dsn, principal_id, namespace_id, read=False, write=False, manage=False):
    read = read or write or manage
    write = write or manage

    def fn(cur):
        cur.execute("SELECT can_read, can_write, can_manage FROM namespace_grants "
                    "WHERE principal_id = %s AND namespace_id = %s", (principal_id, namespace_id))
        old = cur.fetchone()
        fenced = 0
        reduced = old is not None and ((old["can_read"] and not read) or (old["can_write"] and not write)
                                       or (old["can_manage"] and not manage))
        cur.execute("INSERT INTO namespace_grants (principal_id, namespace_id, can_read, can_write, can_manage) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (principal_id, namespace_id) DO UPDATE "
                    "SET can_read = EXCLUDED.can_read, can_write = EXCLUDED.can_write, "
                    "can_manage = EXCLUDED.can_manage",
                    (principal_id, namespace_id, read, write, manage))
        if reduced:
            cur.execute("UPDATE principals SET auth_epoch = auth_epoch + 1 WHERE id = %s", (principal_id,))
            fenced = len(fence_principal_jobs(cur, principal_id))
        cur.execute("UPDATE system_control SET policy_revision = policy_revision + 1")
        cur.execute("INSERT INTO audit_events (event_type, object_ids, safe_details) VALUES ('grant_set', %s, %s)",
                    (Json({"principal_id": principal_id, "namespace_id": namespace_id}),
                     Json({"read": read, "write": write, "manage": manage})))
        return {"principal_id": principal_id, "namespace_id": namespace_id,
                "can_read": read, "can_write": write, "can_manage": manage, "jobs_cancelled": fenced}

    return _tx(dsn, fn)


def main(argv=None):
    p = argparse.ArgumentParser(prog="memory_server.admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("create-namespace")
    s.add_argument("--name", required=True)
    s = sub.add_parser("create-principal")
    s.add_argument("--name", required=True)
    s.add_argument("--kind", choices=["device", "admin"], default="device")
    s = sub.add_parser("grant")
    s.add_argument("--principal", required=True)
    s.add_argument("--namespace", required=True)
    s.add_argument("--read", action="store_true")
    s.add_argument("--write", action="store_true")
    s.add_argument("--manage", action="store_true")
    s = sub.add_parser("delete-experience")
    s.add_argument("--experience", required=True)
    s = sub.add_parser("revoke-principal")
    s.add_argument("--principal", required=True)
    s = sub.add_parser("retry-job")
    s.add_argument("--job", required=True)
    s.add_argument("--expected-attempt", type=int, required=True)
    s.add_argument("--principal", required=True)
    args = p.parse_args(argv)
    config = Config.from_env()
    dsn = config.database_url
    try:
        out = _dispatch(args, config, dsn)
    except ServiceError as exc:
        json.dump(exc.to_dict(), sys.stderr)
        sys.stderr.write("\n")
        sys.exit(1)
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _dispatch(args, config, dsn):
    if args.cmd == "delete-experience":
        return delete_experience(config, args.experience)
    if args.cmd == "revoke-principal":
        return revoke_principal(config, args.principal)
    if args.cmd == "retry-job":
        return retry_job(config, args.principal, args.job, args.expected_attempt)
    if args.cmd == "create-namespace":
        out = create_namespace(dsn, args.name)
    elif args.cmd == "create-principal":
        out = create_principal(dsn, args.name, args.kind)
    else:
        out = grant(dsn, args.principal, args.namespace, args.read, args.write, args.manage)
    return out


if __name__ == "__main__":
    main()
