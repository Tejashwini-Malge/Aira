"""Pure-helper tests for ai_quiz_bp — no request context, no network."""
from ai_quiz_bp import _build_resources, _interview_count


# --- _interview_count ---

def test_known_durations_map_to_their_question_counts():
    assert _interview_count(10) == 4
    assert _interview_count(20) == 6
    assert _interview_count(30) == 8


def test_string_durations_are_coerced():
    assert _interview_count("30") == 8


def test_unknown_or_invalid_durations_fall_back_to_five():
    assert _interview_count(15) == 5
    assert _interview_count(None) == 5
    assert _interview_count("soon") == 5


# --- _build_resources ---

def test_build_resources_attaches_search_urls_by_kind():
    out = _build_resources([{
        "area": "database indexing",
        "items": [
            {"title": "Indexing basics", "type": "video", "query": "sql indexing tutorial"},
            {"title": "Indexing deep dive", "type": "blog", "query": "b-tree index explained"},
            {"title": "Index research", "type": "paper", "query": "database index structures"},
        ],
    }])
    assert len(out) == 1 and out[0]["area"] == "database indexing"
    urls = [it["url"] for it in out[0]["items"]]
    assert urls[0].startswith("https://www.youtube.com/results?search_query=")
    assert urls[1].startswith("https://www.google.com/search?q=")
    assert urls[2].startswith("https://scholar.google.com/scholar?q=")
    # Queries are URL-encoded so links always resolve.
    assert "sql%20indexing%20tutorial" in urls[0]


def test_build_resources_normalises_loose_type_names():
    out = _build_resources([{
        "area": "REST APIs",
        "items": [
            {"title": "a", "type": "article", "query": "q1"},
            {"title": "b", "type": "research paper", "query": "q2"},
            {"title": "c", "type": "podcast", "query": "q3"},
        ],
    }])
    kinds = [it["type"] for it in out[0]["items"]]
    assert kinds == ["blog", "paper", "blog"]


def test_build_resources_caps_groups_and_items():
    raw = [{"area": f"a{i}", "items": [{"title": f"t{j}", "type": "video", "query": f"q{j}"}
                                       for j in range(5)]} for i in range(6)]
    out = _build_resources(raw)
    assert len(out) == 4
    assert all(len(g["items"]) == 3 for g in out)


def test_build_resources_drops_empty_groups_and_falls_back_to_area_query():
    out = _build_resources([
        # No area -> the whole group is dropped even though it has a usable item.
        {"area": "", "items": [{"title": "t", "type": "video", "query": "q"}]},
        # Blank title/query -> the area itself becomes the search query.
        {"area": "real", "items": [{"title": "", "type": "video", "query": ""}]},
        {"area": "kept", "items": [{"title": "t", "type": "video", "query": "q"}]},
    ])
    assert [g["area"] for g in out] == ["real", "kept"]
    assert out[0]["items"][0]["url"].endswith("real")


def test_build_resources_handles_none():
    assert _build_resources(None) == []
