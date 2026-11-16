"""P0-2 search: reference-keywords/v1 and the literal-substring/v1 baseline (§7, §7.1), plus recorded limits."""
import pytest

from conftest import World  # noqa: F401  (fixture module)
from memory_server.errors import ServiceError


def submit_plain(world, token, key, text):
    return world.svc.submit(token, world.body(text), key)[0]


def ids(res):
    return [r["sources"][0]["experience_id"] for r in res["results"]]


def code_of(exc):
    return exc.value.code


def test_results_absent_until_worker_runs_and_report_pending(world):
    world.explicit(world.a["token"], "s1", "배포 DB는 PostgreSQL이다")
    res = world.search(world.a["token"], "PostgreSQL")
    assert res["results"] == [] and res["indexing"] == {"has_pending": True,
                                                         "has_failed": False}
    world.drain()
    res = world.search(world.a["token"], "PostgreSQL")
    assert len(res["results"]) == 1 and res["indexing"]["has_pending"] is False


def test_explicit_memory_roundtrip_with_provenance_and_trust(world):
    r = world.explicit(world.a["token"], "s2", "배포 DB는 PostgreSQL을 사용한다.", title="배포 DB",
                       hints=["database", "postgres"], actor="agent")
    world.drain()
    res = world.search(world.a["token"], "PostgreSQL 배포")
    assert res["content_trust"] == "untrusted_data" and res["engine"] == "reference/1"
    assert res["index_profile"] == "reference-keywords/v1" and res["status"] == "ok"
    [hit] = res["results"]
    assert hit["text"] == "배포 DB는 PostgreSQL을 사용한다."  # verbatim
    assert hit["kind"] == "derived_memory" and hit["validity"] == "valid"
    [src] = hit["sources"]
    assert src["experience_id"] == r["experience_id"] and src["source_version"] == 1
    assert src["locator"]["verified"] is False and "span" not in src
    ed = hit["engine_data"]
    assert ed["representation"] == "explicit_memory" and ed["title"] == "배포 DB"
    assert ed["actor_reported"] == "agent" and ed["actor_basis"] == "client_reported"
    assert ed["verification"] == "not_verified" and ed["evidence_status"] == "no_source_linked"
    assert ed["primary_experience_id"] == r["experience_id"]
    assert set(ed["matched_keywords"]) == {"postgresql", "배포"}


def test_title_and_hint_only_match_report_fields(world):
    world.explicit(world.a["token"], "s3", "내용에는 키워드가 없다", title="릴리스 체크리스트",
                   hints=["kubernetes"])
    world.drain()
    res = world.search(world.a["token"], "릴리스")
    assert [h["engine_data"]["matched_fields"] for h in res["results"]] == [["title"]]
    res = world.search(world.a["token"], "Kubernetes")
    assert [h["engine_data"]["matched_fields"] for h in res["results"]] == [["hints"]]
    assert world.search(world.a["token"], "없는단어")["results"] == []


def test_ranking_by_matched_keyword_count_then_deterministic_tie_break(world):
    one = world.explicit(world.a["token"], "r1", "alpha only")
    two = world.explicit(world.a["token"], "r2", "alpha beta together")
    tie1 = world.explicit(world.a["token"], "r3", "alpha beta again")
    world.drain()
    res = world.search(world.a["token"], "alpha beta")
    got = ids(res)
    assert got[-1] == one["experience_id"]
    assert got[:2] == sorted([two["experience_id"], tie1["experience_id"]])
    # stable across repeated calls
    assert ids(world.search(world.a["token"], "alpha beta")) == got


def test_case_insensitive_and_mixed_language_and_code_identifiers(world):
    world.explicit(world.a["token"], "c1", "parseHTTPResponse 는 foo_bar 모듈에 있다")
    world.drain()
    assert len(world.search(world.a["token"], "PARSEHTTPRESPONSE")["results"]) == 1
    assert len(world.search(world.a["token"], "foo_bar 모듈")["results"]) == 1
    assert len(world.search(world.a["token"], "x", keywords=["FOO_BAR"])["results"]) == 1


def test_limit_and_excerpt_budget(world):
    for i in range(4):
        world.explicit(world.a["token"], "l%d" % i, "shared token %d" % i)
    world.drain()
    res = world.search(world.a["token"], "shared", limit=2)
    assert len(res["results"]) == 2 and res["truncated"] is True
    long_text = "앞부분 " * 400 + "needle_word" + " 뒷부분" * 400
    world.explicit(world.a["token"], "l9", long_text)
    world.drain()
    res = world.search(world.a["token"], "needle_word", max_chars=200)
    [hit] = res["results"]
    assert hit["truncated"] is True and len(hit["text"]) <= 200 and "needle_word" in hit["text"]


def test_read_only_reader_searches_but_outsiders_and_anonymous_do_not(world):
    world.explicit(world.a["token"], "p2", "공개 메모 sharedplan")
    world.drain()
    for principal in (world.reader, world.b):
        assert len(world.search(principal["token"], "sharedplan")["results"]) == 1
    with pytest.raises(ServiceError) as exc:
        world.search(world.outsider["token"], "sharedplan")
    assert code_of(exc) == "NOT_FOUND"
    with pytest.raises(ServiceError) as exc:
        world.svc.search(None, {"query": "x"})
    assert exc.value.status == 401


@pytest.mark.parametrize("extra,code", [
    ({"query": ""}, "VALIDATION_ERROR"),
    ({"query": "   "}, "VALIDATION_ERROR"),
    ({"query": " ".join("w%02d" % i for i in range(13))}, "TOO_MANY_SEARCH_TERMS"),
    ({"query": "a ! ?"}, "EMPTY_SEARCH_TERMS"),
    ({"query": "ok", "limit": 0}, "VALIDATION_ERROR"),
    ({"query": "ok", "limit": 21}, "VALIDATION_ERROR"),
    ({"query": "ok", "limit": True}, "VALIDATION_ERROR"),
    ({"query": "ok", "max_chars": 0}, "VALIDATION_ERROR"),
    ({"query": "ok", "keywords": []}, "VALIDATION_ERROR"),
    ({"query": "ok", "keywords": [""]}, "VALIDATION_ERROR"),
    ({"query": "ok", "bogus": 1}, "UNKNOWN_FIELD"),
])
def test_validation_errors(world, extra, code):
    with pytest.raises(ServiceError) as exc:
        world.svc.search(world.a["token"], extra)
    assert exc.value.status == 400 and exc.value.code == code


def test_literal_baseline_profile(world):
    world.switch_engine(index_profile="literal-substring/v1")
    world.explicit(world.a["token"], "lit1", "배포 DB는 PostgreSQL을 사용한다.")
    world.drain()
    res = world.search(world.a["token"], "PostgreSQL을 사용")
    assert res["index_profile"] == "literal-substring/v1" and len(res["results"]) == 1
    with pytest.raises(ServiceError) as exc:
        world.search(world.a["token"], "x", keywords=["x"])
    assert exc.value.code == "UNSUPPORTED_SEARCH_OPTION"


# ---- Recorded failure cases (§7.1): the baseline is deliberately naive; these pin down its current limits so
# ---- Phase 1 engines can be compared against them. A change in these results must be a conscious decision.

def test_known_limit_korean_particles_not_stemmed(world):
    world.explicit(world.a["token"], "k1", "데이터베이스 백업 정책")
    world.drain()
    assert len(world.search(world.a["token"], "데이터베이스")["results"]) == 1
    assert world.search(world.a["token"], "데이터베이스를 백업")["results"][0]["engine_data"][
        "matched_keywords"] == ["백업"]
    assert world.search(world.a["token"], "데이터베이스를")["results"] == []


def test_known_limit_no_synonyms(world):
    world.explicit(world.a["token"], "k2", "자동차 정비 기록")
    world.drain()
    assert world.search(world.a["token"], "car maintenance")["results"] == []


def test_known_limit_symbols_need_explicit_keywords(world):
    world.explicit(world.a["token"], "k3", "C++ 템플릿 규칙")
    world.drain()
    with pytest.raises(ServiceError) as exc:
        world.search(world.a["token"], "C++")
    assert exc.value.code == "EMPTY_SEARCH_TERMS"
    assert len(world.search(world.a["token"], "c++ 규칙", keywords=["C++"])["results"]) == 1


def test_known_limit_substring_false_positive(world):
    world.explicit(world.a["token"], "k4", "catalog of tools")
    world.drain()
    assert len(world.search(world.a["token"], "cat")["results"]) == 1  # substring, not word match


def test_known_limit_literal_misses_natural_language_question(world):
    world.explicit(world.a["token"], "k5", "배포 DB는 PostgreSQL을 사용한다.")
    world.drain()
    question = "어떤 DB를 쓰는지 PostgreSQL 알려줘"
    assert len(world.search(world.a["token"], question)["results"]) == 1
    world.switch_engine(index_profile="literal-substring/v1")
    assert world.search(world.a["token"], question)["results"] == []


def test_paragraph_projection_search_returns_span(world):
    world.switch_engine("2")
    text = "첫 문단 apple\n\n둘째 문단 banana\n\n셋째 문단 cherry"
    r = submit_plain(world, world.a["token"], "pp1", text)
    world.drain()
    res = world.search(world.a["token"], "banana")
    [hit] = res["results"]
    assert res["engine"] == "reference/2" and hit["text"] == "둘째 문단 banana"
    assert hit["engine_data"]["representation"] == "source_projection_paragraph"
    [src] = hit["sources"]
    raw = text.encode("utf-8")
    assert src["experience_id"] == r["experience_id"] and src["span"]["unit"] == "utf8_byte"
    assert raw[src["span"]["start"]:src["span"]["end"]].decode("utf-8") == "둘째 문단 banana"
