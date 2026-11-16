"""Policy ledger: an append-only, hash-chained record of policy-reducing operations (P0-5, §10 recovery).

Why it exists: a database backup cannot know about deletions and revocations made after it was taken, so
restoring one would silently re-expose deleted sources and revoked credentials. The ledger is written to a
location *separate* from the database volume and is replayed onto a restored database before it may serve.

Contents are identifiers only (no text, no credentials). Entries are appended and fsynced BEFORE the database
transaction that applies them commits, so a crash between the two can only cause an over-applied policy on a
later restore (fail closed), never a missed one. Duplicate entries are harmless: every replay is idempotent.

The hash chain makes edits and mid-file truncation detectable; it does not stop an attacker with write access
to the ledger volume from rewriting the whole file. Backups record the head they were taken at, so a ledger
that is shorter than the backup's recorded head is rejected.
"""
import fcntl
import hashlib
import json
import os

GENESIS = "0" * 64
TYPES = ("delete_experience", "revoke_principal", "grant_reduce", "permission_reduce")


class LedgerError(Exception):
    pass


def _hash(prev, seq, kind, data):
    body = json.dumps({"seq": seq, "type": kind, "data": data}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256((prev + body).encode("ascii")).hexdigest()


def _read(path):
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    raise LedgerError("ledger contains an unparsable line")
    return entries


def verify(path):
    """Returns (entries, head_seq, head_hash). Raises LedgerError if the chain is broken."""
    entries = _read(path)
    prev, expected = GENESIS, 1
    for entry in entries:
        if entry.get("seq") != expected or entry.get("prev") != prev or entry.get("type") not in TYPES:
            raise LedgerError("ledger chain broken at seq %s" % entry.get("seq"))
        if _hash(prev, entry["seq"], entry["type"], entry["data"]) != entry.get("hash"):
            raise LedgerError("ledger hash mismatch at seq %s" % entry["seq"])
        prev, expected = entry["hash"], expected + 1
    return entries, expected - 1, prev


def head(path):
    _, seq, digest = verify(path)
    return {"seq": seq, "hash": digest}


def ensure(path):
    """Create an empty ledger at service start. A ledger that exists but is empty is legitimate; a ledger that is
    MISSING when a backup says it should exist means history was lost, and recovery refuses to proceed."""
    if not path or os.path.exists(path):
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def append(path, kind, data):
    """Durably append one entry; a no-op when no ledger is configured (tests, local development)."""
    if not path:
        return None
    if kind not in TYPES:
        raise LedgerError("unknown ledger entry type")
    directory = os.path.dirname(os.path.abspath(path))
    created = not os.path.exists(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _, seq, prev = verify(path)
        seq += 1
        entry = {"seq": seq, "type": kind, "data": data, "prev": prev, "hash": _hash(prev, seq, kind, data)}
        os.write(fd, (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return seq
