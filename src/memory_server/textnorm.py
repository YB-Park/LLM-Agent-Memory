"""Search-time text normalization shared by the engine (index side) and search (query side) (§7.1).

NFC plus ASCII A-Z lowercasing only. DB locale case folding is deliberately not used.
"""
import re
import unicodedata

_ASCII_UPPER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}
_TERM_RE = re.compile(r"\w+")  # Unicode letters/digits/underscore runs


def normalize(text):
    return unicodedata.normalize("NFC", text).translate(_ASCII_UPPER)


def extract_terms(normalized_query, max_terms):
    """Ordered, de-duplicated runs of letters/digits/underscore; one-scalar runs are dropped.

    Returns None when more than max_terms remain.
    """
    seen, out = set(), []
    for term in _TERM_RE.findall(normalized_query):
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        out.append(term)
    if len(out) > max_terms:
        return None
    return out
