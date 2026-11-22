"""Meeting library: indexing, backfill, JA/EN search, filters, tags, API."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox import session_archive, session_library
from mimivox.api_adapter import router


def _events(lines: list[tuple[str, str, str]]) -> list[dict]:
    """(speaker label, ja, en) -> ASR + translation events with ids 1..n."""
    out: list[dict] = []
    for i, (who, ja, en) in enumerate(lines, start=1):
        sp = {"speaker": {"id": who[-1], "label": who}} if who else {}
        base = {"index": i, "start": float(i), "end": float(i) + 0.5}
        out.append({"type": "AsrCompletedEvent", "text": ja, **base, **sp})
        out.append(
            {"type": "TranslationCompletedEvent", "text": en, "source_indexes": [i], **base, **sp}
        )
    return out


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session_archive, "_SESSIONS_DIR", root)
    monkeypatch.setattr(session_archive, "_INDEX_PATH", root / "index.json")
    session_archive.archive_session(
        "budget",
        _events(
            [
                (
                    "部長",
                    "部長が来期の予算について説明しました。",
                    "The director explained next year's budget.",
                ),
                ("田中", "了解しました。", "Understood."),
            ]
        ),
        started_at="2026-07-01T09:00:00+00:00",
        label="Weekly sync",
    )
    session_archive.archive_session(
        "hiring",
        _events(
            [
                ("田中", "採用計画を見直しましょう。", "Let's revisit the hiring plan."),
                ("部長", "予算は変わりません。", "The budget does not change."),
            ]
        ),
        started_at="2026-07-10T09:00:00+00:00",
        label="Hiring review",
    )
    return root


def _search(**kw):
    return session_archive.search_sessions(**kw)


def _ids(resp):
    return [r["id"] for r in resp["results"]]


def test_archive_indexes_incrementally(lib):
    assert sorted(_ids(_search())) == ["budget", "hiring"]
    session_archive.archive_session(
        "new", _events([("A", "新規案件", "A new project")]), started_at="2026-07-20T00:00:00+00:00"
    )
    assert _ids(_search(q="新規案件")) == ["new"]


def test_japanese_substring_without_spaces(lib):
    # 3+ chars go through FTS5 trigram, matching mid-sentence.
    assert _ids(_search(q="予算について")) == ["budget"]
    assert _ids(_search(q="見直しま")) == ["hiring"]


def test_two_character_japanese_term_falls_back(lib):
    resp = _search(q="予算")
    assert _ids(resp) == ["hiring", "budget"]  # newest first
    assert resp["results"][1]["lines"][0]["line_id"] == 1


def test_english_search_is_case_insensitive_and_returns_line_ids(lib):
    resp = _search(q="HIRING plan")
    assert _ids(resp) == ["hiring"]
    line = resp["results"][0]["lines"][0]
    assert line["line_id"] == 1
    assert "en" in resp["results"][0]["matched_in"]
    hits = [seg["t"] for seg in line["en_snippet"] if seg["hit"]]
    assert [h.lower() for h in hits] == ["hiring", "plan"]


def test_snippet_highlights_original_text(lib):
    line = _search(q="予算")["results"][1]["lines"][0]
    hits = [seg["t"] for seg in line["ja_snippet"] if seg["hit"]]
    assert hits == ["予算"]


def test_fullwidth_query_matches_via_nfkc(lib):
    assert "budget" in _ids(_search(q="ｂｕｄｇｅｔ"))


def test_multiple_terms_must_share_a_line(lib):
    assert _ids(_search(q="部長 予算")) == ["hiring", "budget"]
    assert _ids(_search(q="部長 採用")) == []


def test_date_range_filter(lib):
    assert _ids(_search(q="予算", since="2026-07-05")) == ["hiring"]
    assert _ids(_search(q="予算", until="2026-07-01")) == ["budget"]  # inclusive day
    assert _ids(_search(q="予算", since="2026-07-02", until="2026-07-09")) == []
    with pytest.raises(ValueError):
        _search(q="予算", since="last tuesday")


def test_speaker_filter_limits_sessions_and_lines(lib):
    resp = _search(q="予算", speaker="田中")
    assert resp["results"] == []
    resp = _search(q="予算", speaker="部長")
    assert [r["match_count"] for r in resp["results"]] == [1, 1]
    # Speaker alone lists that speaker's lines.
    resp = _search(speaker="田中")
    assert sorted(_ids(resp)) == ["budget", "hiring"]
    assert resp["speakers"] == ["田中", "部長"]


def test_speaker_label_is_searchable_text(lib):
    resp = _search(q="田中")
    assert resp["results"][0]["matched_in"] == ["speaker"]


def test_tags_add_remove_and_search(lib):
    assert session_archive.update_tags("budget", add=["#Finance", "q3", "finance"]) == [
        "Finance",
        "q3",
    ]
    assert _ids(_search(q="finance")) == ["budget"]
    assert _search(q="finance")["results"][0]["matched_in"] == ["tag"]
    assert _ids(_search(tag="q3")) == ["budget"]
    assert _search()["tags"] == ["Finance", "q3"]
    assert session_archive.update_tags("budget", remove=["FINANCE"]) == ["q3"]
    assert _ids(_search(q="finance")) == []
    assert session_archive.update_tags("missing", add=["x"]) is None
    with pytest.raises(ValueError):
        session_archive.update_tags("budget", add=["   "])


def test_tags_survive_rearchive_and_are_in_session_json(lib):
    session_archive.update_tags("budget", add=["keep"])
    session_archive.archive_session(
        "budget",
        _events([("A", "更新", "Updated")]),
        started_at="2026-07-01T09:00:00+00:00",
        label="Weekly sync",
    )
    assert session_archive.load_session("budget")["tags"] == ["keep"]
    assert _ids(_search(tag="keep")) == ["budget"]


def test_relabel_speaker_reindexes(lib):
    session_archive.relabel_speaker("budget", "長", "鈴木")
    session_archive.relabel_speaker("budget", "中", "山田")
    assert _ids(_search(q="山田")) == ["budget"]


def test_delete_removes_from_index(lib):
    session_archive.delete_session("budget")
    assert _ids(_search(q="来期")) == []
    assert _ids(_search()) == ["hiring"]


def test_title_and_date_are_searchable(lib):
    assert _ids(_search(q="weekly")) == ["budget"]
    assert _ids(_search(q="2026-07-10")) == ["hiring"]


def test_backfill_is_idempotent_and_rebuilds_from_archives(lib):
    session_archive.library_path().unlink()
    first = session_archive.backfill_index()
    assert first == {"indexed": 2, "unchanged": 0, "removed": 0, "skipped": 0}
    second = session_archive.backfill_index()
    assert second == {"indexed": 0, "unchanged": 2, "removed": 0, "skipped": 0}
    conn = sqlite3.connect(session_archive.library_path())
    n = conn.execute("SELECT count(*) FROM lines").fetchone()[0]
    session_archive.backfill_index()
    assert conn.execute("SELECT count(*) FROM lines").fetchone()[0] == n
    assert conn.execute("SELECT count(*) FROM fts").fetchone()[0] == n
    assert sorted(_ids(_search(q="予算"))) == ["budget", "hiring"]


def test_backfill_picks_up_edits_and_deletions_and_bad_files(lib):
    session_archive.backfill_index()
    # Tags written straight to a file (e.g. restored from backup) are picked up.
    data = session_archive.load_session("hiring")
    data["tags"] = ["restored"]
    import json

    session_archive.session_file("hiring").write_text(json.dumps(data), encoding="utf-8")
    session_archive.session_file("budget").unlink()
    (lib / "junk.json").write_text("{not json", encoding="utf-8")
    stats = session_archive.backfill_index()
    assert stats == {"indexed": 1, "unchanged": 0, "removed": 1, "skipped": 1}
    assert _ids(_search(tag="restored")) == ["hiring"]


def test_snippet_windows_long_text():
    text = "あ" * 100 + "予算" + "い" * 100
    segs = session_library.snippet(text, ["予算"])
    assert segs[0]["t"] == "…" and segs[-1]["t"] == "…"
    assert [s["t"] for s in segs if s["hit"]] == ["予算"]
    assert len("".join(s["t"] for s in segs)) < len(text)


# -- HTTP ---------------------------------------------------------------------


@pytest.fixture
def client(lib):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_search_and_filters(client):
    r = client.get(
        "/api/sessions/search", params={"q": "予算", "speaker": "部長", "since": "2026-07-05"}
    )
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body["results"]] == ["hiring"]
    line = body["results"][0]["lines"][0]
    assert line["line_id"] == 2 and line["speaker"] == "部長"
    assert any(seg["hit"] for seg in line["ja_snippet"])


def test_api_search_is_not_shadowed_by_session_id_route(client):
    assert client.get("/api/sessions/search").json()["total_sessions"] == 2
    assert client.get("/api/sessions/budget").json()["id"] == "budget"


def test_api_bad_date_is_400(client):
    assert client.get("/api/sessions/search", params={"since": "nope"}).status_code == 400


def test_api_tags_roundtrip(client):
    r = client.post("/api/sessions/budget/tags", json={"add": ["Finance"]})
    assert r.json() == {"session_id": "budget", "tags": ["Finance"]}
    found = client.get("/api/sessions/search", params={"q": "finance"}).json()
    assert [s["id"] for s in found["results"]] == ["budget"]
    assert client.get("/api/sessions/budget").json()["tags"] == ["Finance"]
    r = client.post("/api/sessions/budget/tags", json={"remove": ["finance"]})
    assert r.json()["tags"] == []
    assert client.post("/api/sessions/nope/tags", json={"add": ["x"]}).status_code == 404
    assert client.post("/api/sessions/budget/tags", json={"add": [""]}).status_code == 400


def test_delete_removes_session_from_search_via_delete_hook(lib):
    """delete_session is the single delete path; the library hook must clean the index."""
    assert session_archive._remove_from_library in session_archive._delete_hooks
    assert _ids(_search(q="来期")) == ["budget"]
    assert session_archive.delete_session("budget") is True
    assert _ids(_search(q="来期")) == []
    assert _ids(_search(q="Weekly")) == []
    assert _ids(_search(speaker="部長")) == ["hiring"]
    conn = sqlite3.connect(session_archive.library_path())
    assert conn.execute("SELECT count(*) FROM lines WHERE session_id='budget'").fetchone()[0] == 0
    assert (
        conn.execute("SELECT count(*) FROM fts").fetchone()[0]
        == conn.execute("SELECT count(*) FROM lines").fetchone()[0]
    )


def test_delete_all_history_leaves_no_search_index(lib):
    from mimivox import privacy

    assert session_archive.library_path().exists()
    assert privacy.delete_all_history()["sessions"] == 2
    assert not session_archive.library_path().exists()
    assert _ids(_search(q="予算")) == []
