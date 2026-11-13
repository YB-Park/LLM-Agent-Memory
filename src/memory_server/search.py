"""Search over the active generation: reference-keywords/v1 and literal-substring/v1 (§7, §7.1).

Runs inside the caller's transaction, which already holds the control lock (SHARE) and has verified the
principal's read grant. Normalized strings are passed as SQL parameters; no user text becomes a pattern.
There is no raw-text fallback: only valid artifacts whose provenance experiences are all retained
are candidates, in the active generation only.
"""
import unicodedata

from .engine import engine_label
from .errors import invalid
from .textnorm import extract_terms, normalize
from .validation import iso

MAX_QUERY_CHARS = 256
MAX_AUTO_TERMS = 12
MAX_EXPLICIT_KEYWORDS = 8
MAX_KEYWORD_CHARS = 64
DEFAULT_LIMIT, MAX_LIMIT = 5, 20
DEFAULT_CHARS, MAX_CHARS = 4000, 16000
_FIELDS = {"namespace_id", "query", "keywords", "limit", "max_chars"}

_VALID_AND_RETAINED = """
    a.namespace_id = %(ns)s AND a.generation_id = %(gen)s AND a.state = 'valid' AND j.state = 'succeeded'
    AND EXISTS (SELECT 1 FROM artifact_sources s WHERE s.artifact_id = a.id)
    AND NOT EXISTS (SELECT 1 FROM artifact_sources s
                    JOIN experiences e ON e.id = s.experience_id AND e.namespace_id = s.namespace_id
                                      AND e.source_version = s.source_version
                    WHERE s.artifact_id = a.id AND e.state <> 'retained')
"""

_KEYWORD_SQL = """
SELECT a.id, a.local_key, a.display_text, a.search_text, a.payload, j.experience_id,
       m.body, m.title, m.hints,
       (SELECT count(DISTINCT x) FROM unnest(m.body || m.title || m.hints) x) AS matched
FROM artifacts a
JOIN jobs j ON j.id = a.job_id
CROSS JOIN LATERAL (
    SELECT
      coalesce(array_agg(u.t ORDER BY u.ord) FILTER (WHERE strpos(a.search_text, u.t) > 0), '{}') AS body,
      coalesce(array_agg(u.t ORDER BY u.ord) FILTER (
          WHERE strpos(coalesce(a.payload #>> '{search,title}', ''), u.t) > 0), '{}') AS title,
      coalesce(array_agg(u.t ORDER BY u.ord) FILTER (WHERE EXISTS (
          SELECT 1 FROM jsonb_array_elements_text(coalesce(a.payload #> '{search,hints}', '[]'::jsonb)) h(v)
          WHERE strpos(h.v, u.t) > 0)), '{}') AS hints
    FROM unnest(%(terms)s::text[]) WITH ORDINALITY AS u(t, ord)
) m
WHERE """ + _VALID_AND_RETAINED + """
  AND cardinality(m.body || m.title || m.hints) > 0
ORDER BY matched DESC, cardinality(m.title) DESC, cardinality(m.hints) DESC, cardinality(m.body) DESC,
         j.experience_id ASC, a.local_key ASC
LIMIT %(limit)s
"""

_LITERAL_SQL = """
SELECT a.id, a.local_key, a.display_text, a.search_text, a.payload, j.experience_id,
       ARRAY[%(q)s]::text[] AS body, '{}'::text[] AS title, '{}'::text[] AS hints, 1 AS matched
FROM artifacts a
JOIN jobs j ON j.id = a.job_id
WHERE """ + _VALID_AND_RETAINED + """
  AND strpos(a.search_text, %(q)s) > 0
ORDER BY strpos(a.search_text, %(q)s) ASC, j.experience_id ASC, a.local_key ASC
LIMIT %(limit)s
"""

_SOURCES_SQL = """
SELECT s.artifact_id, s.experience_id, s.source_version, s.spans, e.received_at, e.source_metadata
FROM artifact_sources s
JOIN experiences e ON e.id = s.experience_id AND e.namespace_id = s.namespace_id
                  AND e.source_version = s.source_version
WHERE s.artifact_id = ANY(%s::uuid[]) AND e.state = 'retained'
ORDER BY s.artifact_id, s.experience_id
"""


def _int_param(body, name, default, low, high):
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise invalid("%s must be an integer between %d and %d" % (name, low, high))
    return value


def parse_request(body, profile):
    """Validate the request and return (query, terms, limit, max_chars). terms is the effective keyword list."""
    extra = set(body) - _FIELDS
    if extra:
        raise invalid("unknown field: %s" % sorted(extra)[0], "UNKNOWN_FIELD")
    query = body.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS or "\x00" in query:
        raise invalid("query must be a non-empty string up to %d characters" % MAX_QUERY_CHARS)
    limit = _int_param(body, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    max_chars = _int_param(body, "max_chars", DEFAULT_CHARS, 1, MAX_CHARS)
    keywords = body.get("keywords")
    if profile == "literal-substring/v1":
        if keywords is not None:
            raise invalid("this index profile does not support keywords", "UNSUPPORTED_SEARCH_OPTION")
        return query, [normalize(query).strip()], limit, max_chars
    if keywords is not None:
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= MAX_EXPLICIT_KEYWORDS:
            raise invalid("keywords must be a list of 1-%d strings" % MAX_EXPLICIT_KEYWORDS)
        terms = []
        for kw in keywords:
            if not isinstance(kw, str) or "\x00" in kw or not kw.strip() or len(kw.strip()) > MAX_KEYWORD_CHARS:
                raise invalid("each keyword must be a non-empty string up to %d characters" % MAX_KEYWORD_CHARS)
            norm = normalize(kw.strip())
            if norm not in terms:
                terms.append(norm)
        return query, terms, limit, max_chars
    terms = extract_terms(normalize(query), MAX_AUTO_TERMS)
    if terms is None:
        raise invalid("query yields more than %d search terms; retry with fewer words or explicit keywords"
                      % MAX_AUTO_TERMS, "TOO_MANY_SEARCH_TERMS")
    if not terms:
        raise invalid("query yields no search terms; retry with explicit keywords", "EMPTY_SEARCH_TERMS")
    return query, terms, limit, max_chars


def _excerpt(display, search_text, matched_body, budget):
    """Whole text if it fits; otherwise a window around the first body match when positions map 1:1."""
    if len(display) <= budget:
        return display, False
    positions = [search_text.find(t) for t in matched_body]
    positions = [p for p in positions if p >= 0]
    mappable = positions and len(display) == len(search_text) and unicodedata.is_normalized("NFC", display)
    start = max(0, min(positions) - budget // 4) if mappable else 0
    end = min(len(display), start + budget)
    start = max(0, end - budget)
    return display[start:end], True


def run(cur, namespace_id, generation, body):
    """generation: row with id, engine_spec, index_profile. Returns the response body (no request_id)."""
    profile = generation["index_profile"]
    query, terms, limit, max_chars = parse_request(body, profile)
    params = {"ns": namespace_id, "gen": generation["id"], "limit": limit + 1}
    if profile == "literal-substring/v1":
        params["q"] = terms[0]
        cur.execute(_LITERAL_SQL, params)
    else:
        params["terms"] = terms
        cur.execute(_KEYWORD_SQL, params)
    rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]

    sources = {}
    if rows:
        cur.execute(_SOURCES_SQL, ([str(r["id"]) for r in rows],))
        for s in cur.fetchall():
            sources.setdefault(str(s["artifact_id"]), []).append(s)

    results, remaining, budget_hit = [], max_chars, False
    for r in rows:
        srcs = sources.get(str(r["id"]))
        if not srcs:
            continue  # provenance no longer retained between statements: never publish
        payload = r["payload"]
        title = payload.get("title")
        avail = remaining - len(title or "")
        if avail < 1:
            budget_hit = True
            break
        text, truncated = _excerpt(r["display_text"], r["search_text"], r["body"], avail)
        remaining -= len(text) + len(title or "")
        fields = [f for f, hit in (("title", r["title"]), ("hints", r["hints"]), ("body", r["body"])) if hit]
        matched = []
        for term in terms:
            if term in r["body"] or term in r["title"] or term in r["hints"]:
                matched.append(term)
        results.append({
            "artifact_id": str(r["id"]), "kind": "derived_memory", "validity": "valid",
            "text": text, "truncated": truncated,
            "sources": [_source(s) for s in srcs],
            "engine_data": {
                "schema": "reference-result/v1", "representation": payload["representation"], "title": title,
                "actor_reported": payload["actor_reported"], "actor_basis": payload["actor_basis"],
                "verification": payload["verification"], "evidence_status": payload["evidence_status"],
                "primary_experience_id": payload["primary_experience_id"],
                "supporting_experience_ids": payload["supporting_experience_ids"],
                "matched_keywords": matched, "matched_fields": fields,
            },
        })
        budget_hit = budget_hit or truncated

    cur.execute("SELECT count(*) FILTER (WHERE j.state IN ('queued', 'running')) AS pending, "
                "count(*) FILTER (WHERE j.state = 'failed') AS failed FROM jobs j "
                "JOIN experiences e ON e.id = j.experience_id AND e.state = 'retained' "
                "WHERE j.namespace_id = %s AND j.generation_id = %s", (namespace_id, generation["id"]))
    idx = cur.fetchone()
    return {
        "generation_id": str(generation["id"]), "engine": engine_label(generation["engine_spec"]),
        "status": "ok", "index_profile": profile, "effective_keywords": terms,
        "indexing": {"scope": "namespace", "has_pending": idx["pending"] > 0, "has_failed": idx["failed"] > 0},
        "results": results, "truncated": more or budget_hit or len(results) < len(rows),
        "content_trust": "untrusted_data",
    }


def _source(s):
    meta = s["source_metadata"].get("source", {})
    out = {"experience_id": str(s["experience_id"]), "source_version": s["source_version"],
           "received_at": iso(s["received_at"]),
           "locator": {"kind": meta.get("kind"), "source_ref": meta.get("source_ref"), "verified": False}}
    if s["spans"]:
        out["span"] = {"unit": "utf8_byte", "start": s["spans"]["start_byte"], "end": s["spans"]["end_byte"]}
    return out
