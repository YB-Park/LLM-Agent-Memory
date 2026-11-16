"""Administrator CLI for bootstrap operations (§4.1, §9.3). Talks to the DB directly.

Usage: python3 -m memory_server.admin <command> [options]
  create-principal --name NAME [--kind device|admin]   (prints the token once)
  rotate-credential --principal ID --name NEWNAME   (new principal with the same permissions; the old one is revoked)
  set-permissions --principal ID [--read] [--write] [--manage]
  delete-experience --experience ID          (tombstone source, invalidate derived results in all generations)
  revoke-principal --principal ID            (credential revoked, its open jobs cancelled, shared data kept)
  retry-job --job ID --expected-attempt N --principal ID   (principal needs manage permission)
  start-generation --principal ID --expected-active ID --engine-version 1|2 [--operation-id ID]
  generation-status [--generation ID]
  compare-generation --principal ID --generation ID --queries-file FILE.json   (JSON list of up to 20 strings)
  activate-generation --principal ID --generation ID --expected-active ID --receipt ID   (also rollback)
  prepare-rollback --principal ID --generation ID --expected-active ID
  abort-generation --principal ID --generation ID --expected-active ID
  resume-ingest --principal ID --expected-active ID
"""
import argparse
import json
import secrets
import sys
import uuid

from .config import Config
from .errors import ServiceError, not_found
from .db import connect
from . import generations, ledger
from .operations import (_audit, _revoke_tx, bump_policy, delete_experience, fence_principal_jobs,
                         retry_job, revoke_principal)
from .service import credential_digest
from .validation import parse_uuid



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


def _create_principal(cur, name, kind, read=False, write=False, manage=False):
    principal_id = str(uuid.uuid4())
    token = "mem_" + secrets.token_urlsafe(32)
    cur.execute("INSERT INTO principals (id, name, credential_digest, kind, can_read, can_write, can_manage) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (principal_id, name, credential_digest(token), kind, read, write, manage))
    _audit(cur, "principal_created", None, {"principal_id": principal_id})
    return {"principal_id": principal_id, "token": token}


def create_principal(dsn, name, kind="device"):
    return _tx(dsn, lambda cur: _create_principal(cur, name, kind))


def rotate_credential(config, principal_id, new_name):
    """Atomically issue a replacement Principal and revoke the old one."""
    pid = parse_uuid(principal_id, "principal_id")

    def fn(cur):
        cur.execute("SELECT kind, can_read, can_write, can_manage FROM principals "
                    "WHERE id = %s AND revoked_at IS NULL", (pid,))
        old = cur.fetchone()
        if old is None:
            raise not_found()
        new = _create_principal(cur, new_name, old["kind"], old["can_read"], old["can_write"], old["can_manage"])
        revoked = _revoke_tx(cur, pid, None, config.policy_ledger)
        return {**new, "revoked_principal_id": pid, "jobs_cancelled": revoked["jobs_cancelled"]}

    return _tx(config.database_url, fn)


def set_permissions(dsn, principal_id, read=False, write=False, manage=False, ledger_path=""):
    pid = parse_uuid(principal_id, "principal_id")
    read = read or write or manage
    write = write or manage

    def fn(cur):
        cur.execute("SELECT can_read, can_write, can_manage FROM principals WHERE id = %s AND revoked_at IS NULL",
                    (pid,))
        old = cur.fetchone()
        if old is None:
            raise not_found()
        reduced = ((old["can_read"] and not read) or (old["can_write"] and not write)
                   or (old["can_manage"] and not manage))
        fenced = 0
        if reduced:
            ledger.append(ledger_path, "permission_reduce", {"principal_id": pid,
                                                             "read": read, "write": write, "manage": manage})
            fenced = len(fence_principal_jobs(cur, pid))
        cur.execute("UPDATE principals SET can_read = %s, can_write = %s, can_manage = %s, "
                    "auth_epoch = auth_epoch + %s WHERE id = %s", (read, write, manage, int(reduced), pid))
        bump_policy(cur)
        _audit(cur, "principal_permissions_set", None, {"principal_id": pid},
               {"read": read, "write": write, "manage": manage})
        return {"principal_id": pid, "can_read": read, "can_write": write, "can_manage": manage,
                "jobs_cancelled": fenced}

    return _tx(dsn, fn)


def main(argv=None):
    p = argparse.ArgumentParser(prog="memory_server.admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("create-principal")
    s.add_argument("--name", required=True)
    s.add_argument("--kind", choices=["device", "admin"], default="device")
    s = sub.add_parser("rotate-credential")
    s.add_argument("--principal", required=True)
    s.add_argument("--name", required=True)
    s = sub.add_parser("set-permissions")
    s.add_argument("--principal", required=True)
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
    s = sub.add_parser("start-generation")
    s.add_argument("--principal", required=True)
    s.add_argument("--expected-active", required=True)
    s.add_argument("--engine-version", required=True)
    s.add_argument("--operation-id", default=None)
    s.add_argument("--index-profile", default=None)
    s = sub.add_parser("generation-status")
    s.add_argument("--generation")
    s = sub.add_parser("compare-generation")
    s.add_argument("--principal", required=True)
    s.add_argument("--generation", required=True)
    s.add_argument("--queries-file", required=True)
    s = sub.add_parser("activate-generation")
    s.add_argument("--principal", required=True)
    s.add_argument("--generation", required=True)
    s.add_argument("--expected-active", required=True)
    s.add_argument("--receipt", required=True)
    s = sub.add_parser("prepare-rollback")
    s.add_argument("--principal", required=True)
    s.add_argument("--generation", required=True)
    s.add_argument("--expected-active", required=True)
    s = sub.add_parser("abort-generation")
    s.add_argument("--principal", required=True)
    s.add_argument("--generation", required=True)
    s.add_argument("--expected-active", required=True)
    s = sub.add_parser("resume-ingest")
    s.add_argument("--principal", required=True)
    s.add_argument("--expected-active", required=True)
    args = p.parse_args(argv)
    config = Config.from_env()
    dsn = config.database_url
    ledger.ensure(config.policy_ledger)
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
    if args.cmd == "rotate-credential":
        return rotate_credential(config, args.principal, args.name)
    if args.cmd == "retry-job":
        return retry_job(config, args.principal, args.job, args.expected_attempt)
    if args.cmd == "start-generation":
        return generations.start_generation(config, args.principal,
                                            args.operation_id or str(uuid.uuid4()), args.expected_active,
                                            args.engine_version, args.index_profile)
    if args.cmd == "generation-status":
        return generations.generation_status(config, args.generation)
    if args.cmd == "compare-generation":
        with open(args.queries_file, encoding="utf-8") as fh:
            queries = json.load(fh)
        return generations.compare_generation(config, args.principal, args.generation, queries)
    if args.cmd == "activate-generation":
        return generations.activate_generation(config, args.principal, args.generation, args.expected_active,
                                               args.receipt)
    if args.cmd == "prepare-rollback":
        return generations.prepare_rollback(config, args.principal, args.generation,
                                            args.expected_active)
    if args.cmd == "abort-generation":
        return generations.abort_generation(config, args.principal, args.generation, args.expected_active)
    if args.cmd == "resume-ingest":
        return generations.resume_ingest(config, args.principal, args.expected_active)
    if args.cmd == "create-principal":
        return create_principal(dsn, args.name, args.kind)
    return set_permissions(dsn, args.principal, args.read, args.write, args.manage, config.policy_ledger)


if __name__ == "__main__":
    main()
