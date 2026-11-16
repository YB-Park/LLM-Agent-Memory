"""Submit input validation and canonicalization (§2, §2.1, §6.2)."""
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timezone

from .errors import invalid, ServiceError

_TOP = {"content", "source", "submission"}
_SOURCE_FIELDS = ("kind", "source_ref", "source_revision", "session_ref", "occurred_at", "harness")
_SUBMISSION_FIELDS = {"intent", "actor_reported", "title", "retrieval_hints"}
_INTENTS = ("experience", "explicit_memory")
_ACTORS = ("user", "agent", "unknown")
_KEY_RE = re.compile(r"^[\x21-\x7e]{1,128}$")
_SOURCE_STR_MAX = 512
SUBMISSION_PROFILE = "reference-input/v1"


def parse_uuid(value, field):
    if not isinstance(value, str):
        raise invalid("%s must be a UUID string" % field)
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise invalid("%s must be a UUID string" % field)


def iso(dt):
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hmac_hex(key, text):
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def validate_idempotency_key(key):
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise invalid("Idempotency-Key must be 1-128 printable ASCII characters", "INVALID_IDEMPOTENCY_KEY")
    return key


def _scalar_len(s):
    return len(s)


def _check_text(text, config):
    if not isinstance(text, str):
        raise invalid("content.text must be a string")
    if "\x00" in text:
        raise invalid("content.text must not contain NUL")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise invalid("content.text must be valid UTF-8")
    if not text.strip():
        raise invalid("content.text must not be blank")
    if len(encoded) > config.max_text_bytes:
        raise ServiceError(413, "PAYLOAD_TOO_LARGE", "content.text exceeds %d bytes" % config.max_text_bytes)
    return text


def _normalize_occurred_at(value):
    if not isinstance(value, str):
        raise invalid("source.occurred_at must be an RFC 3339 timestamp string")
    raw = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise invalid("source.occurred_at must be an RFC 3339 timestamp string")
    if dt.tzinfo is None:
        raise invalid("source.occurred_at must include a timezone")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _validate_source(source):
    if source is None:
        return {}
    if not isinstance(source, dict):
        raise invalid("source must be an object")
    extra = set(source) - set(_SOURCE_FIELDS)
    if extra:
        raise invalid("unknown source field: %s" % sorted(extra)[0], "UNKNOWN_FIELD")
    out = {}
    for name in _SOURCE_FIELDS:
        if name not in source:
            continue
        val = source[name]
        if name == "occurred_at":
            out[name] = _normalize_occurred_at(val)
            continue
        if not isinstance(val, str) or not val.strip() or _scalar_len(val) > _SOURCE_STR_MAX:
            raise invalid("source.%s must be a non-empty string up to %d characters" % (name, _SOURCE_STR_MAX))
        if "\x00" in val:
            raise invalid("source.%s must not contain NUL" % name)
        out[name] = val
    return out


def _validate_submission(sub):
    if sub is None:
        sub = {}
    if not isinstance(sub, dict):
        raise invalid("submission must be an object")
    if "evidence_refs" in sub:
        raise invalid("submission.evidence_refs is not supported in Phase 0", "UNSUPPORTED_FIELD")
    extra = set(sub) - _SUBMISSION_FIELDS
    if extra:
        raise invalid("unknown submission field: %s" % sorted(extra)[0], "UNKNOWN_FIELD")
    intent = sub.get("intent", "experience")
    if intent not in _INTENTS:
        raise invalid("submission.intent must be one of %s" % (_INTENTS,))
    actor = sub.get("actor_reported", "unknown")
    if actor not in _ACTORS:
        raise invalid("submission.actor_reported must be one of %s" % (_ACTORS,))
    title = sub.get("title")
    if title is not None:
        if not isinstance(title, str) or not title.strip() or _scalar_len(title) > 120 or "\x00" in title:
            raise invalid("submission.title must be a non-empty string up to 120 characters")
    hints = sub.get("retrieval_hints", [])
    if not isinstance(hints, list) or len(hints) > 8:
        raise invalid("submission.retrieval_hints must be a list of at most 8 strings")
    for h in hints:
        if not isinstance(h, str) or not h.strip() or _scalar_len(h) > 64 or "\x00" in h:
            raise invalid("each retrieval hint must be a non-empty string up to 64 characters")
    return {"intent": intent, "actor_reported": actor, "title": title, "retrieval_hints": list(hints)}


def validate_submit(body, config):
    """Return canonical submit input. Raises ServiceError on any violation."""
    if not isinstance(body, dict):
        raise invalid("request body must be a JSON object")
    extra = set(body) - _TOP
    if extra:
        raise invalid("unknown field: %s" % sorted(extra)[0], "UNKNOWN_FIELD")
    content = body.get("content")
    if not isinstance(content, dict) or set(content) - {"media_type", "text"}:
        raise invalid("content must be an object with media_type and text")
    if content.get("media_type") != "text/plain":
        raise invalid("content.media_type must be text/plain")
    text = _check_text(content.get("text"), config)
    source = _validate_source(body.get("source"))
    submission = _validate_submission(body.get("submission"))
    meta_size = len(canonical_json({"source": source, "submission": submission}).encode("utf-8"))
    if meta_size > config.max_metadata_bytes:
        raise ServiceError(413, "PAYLOAD_TOO_LARGE", "metadata exceeds %d bytes" % config.max_metadata_bytes)
    return {"text": text, "source": source, "submission": submission}


def request_digest(config, inp, legacy_id=None):
    doc = {"api_version": "v2", "text": inp["text"], "source": inp["source"], "submission": inp["submission"]}
    if legacy_id is not None:
        doc.update(api_version="v1", namespace_id=legacy_id)
    return hmac_hex(config.hmac_key, canonical_json(doc))


def job_input_digest(inp):
    return sha256_hex(canonical_json({"text": inp["text"], "source": inp["source"],
                                      "submission": inp["submission"]}))


def stored_metadata(inp):
    sub = dict(inp["submission"])
    sub["submission_profile"] = SUBMISSION_PROFILE
    return {"source": inp["source"], "submission": sub}
