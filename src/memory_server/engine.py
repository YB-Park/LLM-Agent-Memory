"""Reference Memory Engines reference/1 and reference/2 (§2.2, §3).

A pure function from one immutable input snapshot to artifact drafts. Engines have no DB, auth,
queue or generation access: the Application Service validates the drafts and decides storage and
publication. Both versions are the Phase 1 comparison baseline; changing behaviour means a new version.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .textnorm import normalize
from .validation import canonical_json, sha256_hex

PAYLOAD_SCHEMA = "reference-artifact/v1"
BUILD_ID = "reference-p0.1"
MAX_ARTIFACTS = 256
MAX_OUTPUT_BYTES = 1024 * 1024
_BLANK_LINE = re.compile(r"(?:\r?\n){2,}")


class EngineError(Exception):
    """Engine failure with a safe code. retryable marks transient failures only."""

    def __init__(self, code, message="", retryable=False):
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Snapshot:
    experience_id: str
    source_version: int
    text: str
    source: Dict[str, Any]
    submission: Dict[str, Any]


@dataclass
class ArtifactDraft:
    local_key: str
    payload_schema: str
    payload: Dict[str, Any]
    display_text: str
    search_text: str
    source_refs: List[Dict[str, Any]] = field(default_factory=list)


def reference_spec(version):
    return {"engine": "reference", "version": version, "build_id": BUILD_ID,
            "input_format": "reference-input/v1", "output_format": PAYLOAD_SCHEMA,
            "config": {"projection": "whole" if version == "1" else "paragraph"},
            "model": None, "prompt": None}


def spec_digest(spec):
    return sha256_hex(canonical_json(spec))


def engine_label(spec):
    return "%s/%s" % (spec.get("engine"), spec.get("version"))


class ReferenceEngine(object):
    def __init__(self, version):
        self.version = version
        self.paragraph_mode = version == "2"

    def process(self, snap):
        sub = snap.submission
        if not snap.text.strip():
            raise EngineError("EMPTY_INPUT", "blank text")
        explicit = sub.get("intent") == "explicit_memory"
        if explicit:
            pieces = [("0", snap.text, 0, len(snap.text.encode("utf-8")), "explicit_memory")]
        elif self.paragraph_mode:
            pieces = self._paragraphs(snap.text)
        else:
            pieces = [("0", snap.text, 0, len(snap.text.encode("utf-8")), "source_projection_whole")]
        drafts = [self._draft(snap, key, text, start, end, rep, explicit)
                  for key, text, start, end, rep in pieces]
        if sum(len(canonical_json(d.payload)) + len(d.display_text) + len(d.search_text)
               for d in drafts) > MAX_OUTPUT_BYTES:
            raise EngineError("OUTPUT_LIMIT", "output too large")
        return drafts

    @staticmethod
    def _paragraphs(text):
        """Split on blank lines; spans are UTF-8 byte ranges into the untouched original text."""
        bounds, pos = [], 0
        for m in _BLANK_LINE.finditer(text):
            bounds.append((pos, m.start()))
            pos = m.end()
        bounds.append((pos, len(text)))
        out, cursor_char, cursor_byte = [], 0, 0
        for start, end in bounds:
            chunk = text[start:end]
            if not chunk.strip():
                continue
            if len(out) >= MAX_ARTIFACTS:
                raise EngineError("OUTPUT_LIMIT", "too many paragraphs")
            cursor_byte += len(text[cursor_char:start].encode("utf-8"))
            cursor_char = start
            size = len(chunk.encode("utf-8"))
            out.append((str(len(out)), chunk, cursor_byte, cursor_byte + size, "source_projection_paragraph"))
        return out

    def _draft(self, snap, key, text, start, end, representation, explicit):
        sub = snap.submission
        title = sub.get("title")
        hints = list(sub.get("retrieval_hints") or [])
        payload = {
            "schema": PAYLOAD_SCHEMA,
            "representation": representation,
            "title": title,
            "hints": hints,
            "actor_reported": sub.get("actor_reported", "unknown"),
            "actor_basis": "client_reported",
            "verification": "not_verified" if explicit else "not_assessed",
            "evidence_status": "no_source_linked" if explicit else "not_applicable",
            "primary_experience_id": snap.experience_id,
            "supporting_experience_ids": [],
            "search": {"title": normalize(title) if title else None, "hints": [normalize(h) for h in hints]},
        }
        ref = {"experience_id": snap.experience_id, "source_version": snap.source_version}
        if not explicit:
            ref["start_byte"], ref["end_byte"] = start, end
        return ArtifactDraft(local_key=key, payload_schema=PAYLOAD_SCHEMA, payload=payload,
                             display_text=text, search_text=normalize(text), source_refs=[ref])


_ENGINES = {("reference", "1"): ReferenceEngine("1"), ("reference", "2"): ReferenceEngine("2")}


def engine_for_spec(spec) -> Optional[ReferenceEngine]:
    """The engine code available in this build for a stored spec, or None (ENGINE_VERSION_UNAVAILABLE)."""
    return _ENGINES.get((spec.get("engine"), spec.get("version")))
