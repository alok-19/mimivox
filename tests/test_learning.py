"""Learning mode: tokens, gating, vocabulary, export (no models required)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox import learning, session_archive
from mimivox.api_adapter import APISessionState, api_state, router
from mimivox.languages import DEFAULT_PAIR, parse_pair
from mimivox.settings_store import default_settings

JA = "田中さんから新しい見積もりが届きました。"


def _events():
    rows = [
        (0, JA, "A new quote arrived."),
        (1, "まだ返事がありません。", "No reply yet."),
        (2, "見積もりを確認します。", "I will check the quote."),
    ]
    out = []
    for i, ja, en in rows:
        out.append({"type": "AsrCompletedEvent", "index": i, "start": float(i), "text": ja})
        out.append(
            {
                "type": "TranslationCompletedEvent",
                "index": i,
                "start": float(i),
                "text": en,
                "source_indexes": [i],
            }
        )
    return out


def test_tokens_concatenate_and_read_kanji_only():
    toks = learning.tokenize(JA)
    assert "".join(t["surface"] for t in toks) == JA
    by = {t["surface"]: t for t in toks}
    assert by["新しい"]["reading"] == "あたらしい"
    assert "reading" not in by["から"] and "base" not in by["から"]
    assert by["届き"]["base"] == "届く" and by["届き"]["gloss"].startswith("to reach")


def test_kana_word_gets_gloss_and_names_do_not():
    by = {t["surface"]: t for t in learning.tokenize("まだ田中です")}
    assert "yet" in by["まだ"]["gloss"]
    assert "base" not in by["田中"]


def test_annotate_is_gated_by_setting_and_event_kind():
    ev = {"kind": "final", "ja": JA}
    learning.annotate(ev, {"learning_mode": False})
    learning.annotate({"kind": "partial", "ja": JA}, {"learning_mode": True})
    assert "tokens" not in ev
    learning.annotate(ev, {"learning_mode": True})
    assert ev["tokens"][0]["surface"] == "田中"


def test_adapter_forwards_tokens_on_asr_and_revision_events():
    state = APISessionState()
    state.handle_pipeline_event({"kind": "session_start", "wav": "x"})
    toks = learning.tokenize(JA)
    state.handle_pipeline_event(
        {"kind": "final", "id": 3, "ts": "00:01", "ja": JA, "en": "x", "tokens": toks}
    )
    state.handle_pipeline_event(
        {"kind": "revision", "target_id": 3, "ts": "00:01", "ja": JA, "en": "y", "tokens": toks}
    )
    by_type = {e["type"]: e for e in state.events}
    assert by_type["AsrCompletedEvent"]["tokens"] == toks
    assert by_type["TranscriptRevisionEvent"]["tokens"] == toks
    assert "tokens" not in by_type["TranslationCompletedEvent"]


def test_final_without_tokens_stays_unchanged():
    state = APISessionState()
    state.handle_pipeline_event({"kind": "final", "id": 1, "ts": "00:01", "ja": JA, "en": "x"})
    assert all("tokens" not in e for e in state.events)


def test_vocabulary_counts_dedupes_and_keeps_first_example():
    from mimivox.export_session import pair_events

    words = {w["word"]: w for w in learning.vocabulary(pair_events(_events()))}
    quote = words["見積もり"]
    assert quote["count"] == 2 and quote["reading"] == "みつもり"
    assert quote["example_en"] == "A new quote arrived." and quote["line_id"] == 0
    assert words["届く"]["reading"] == "とどく"
    assert "田中" not in words and "する" not in words


def test_vocabulary_includes_used_business_glossary_terms():
    rows = [
        {"index": 0, "ja": "見積もりを確認します。", "en": "I will check ROI."},
        {"index": 1, "ja": "見積もりを送ります。", "en": "I will send the quote."},
    ]
    settings = {
        "glossary_terms": [{"source": "てっこう", "target": "結構"}],
        "glossary_profiles": [
            {
                "name": "Sales",
                "entries": [
                    {"source": "見積もり", "target": "business quote"},
                    {"source": "ROI", "target": "return on investment"},
                ],
            }
        ],
        "active_glossary_profile": "Sales",
    }
    words = {word["word"]: word for word in learning.vocabulary(rows, settings)}
    assert words["見積もり"]["gloss"] == "business quote"
    assert words["見積もり"]["count"] == 2 and words["見積もり"]["example_ja"] == rows[0]["ja"]
    assert words["ROI"]["gloss"] == "return on investment" and words["ROI"]["count"] == 1
    assert "てっこう" not in words


def test_vocabulary_does_not_treat_chinese_source_as_japanese():
    rows = [{"index": 0, "ja": "合同已签署。", "en": "The agreement was signed."}]
    settings = {
        "source_lang": "zh",
        "target_lang": "en",
        "glossary_profiles": [
            {"name": "Legal", "entries": [{"source": "合同", "target": "agreement"}]}
        ],
        "active_glossary_profile": "Legal",
    }
    assert learning.vocabulary(rows, settings) == []


def test_vocabulary_supports_english_source_and_japanese_target():
    from mimivox.export_session import pair_events

    english = "We signed a contract today and checked the budget."
    japanese = "今日は契約に署名し、予算を確認しました。"
    rows = pair_events(
        [
            {"type": "AsrCompletedEvent", "index": 0, "start": 0, "text": english},
            {
                "type": "TranslationCompletedEvent",
                "index": 0,
                "start": 0,
                "text": japanese,
                "source_indexes": [0],
            },
        ]
    )
    settings = {
        "source_lang": "en",
        "target_lang": "ja",
        "glossary_profiles": [
            {
                "name": "Legal",
                "entries": [
                    {"source": "contract", "target": "契約"},
                    {"source": "予算", "target": "budget"},
                    {"source": "てっこう", "target": "結構"},
                ],
            }
        ],
        "active_glossary_profile": "Legal",
    }
    word = next(item for item in learning.vocabulary(rows, settings) if item["word"] == "契約")
    assert word["gloss"] == "contract"
    assert word["example_ja"] == japanese and word["example_en"] == english
    budget = next(item for item in learning.vocabulary(rows, settings) if item["word"] == "予算")
    assert budget["gloss"] == "budget"
    assert "てっこう" not in {item["word"] for item in learning.vocabulary(rows, settings)}


def test_render_csv_and_anki():
    words = [
        {
            "word": "届く",
            "reading": "とどく",
            "gloss": "to reach",
            "count": 1,
            "example_ja": "届きました。",
            "example_en": "It arrived.\tok",
        }
    ]
    rows = list(csv.reader(io.StringIO(learning.render_csv(words))))
    assert rows[0][:3] == ["word", "reading", "meaning"] and rows[1][0] == "届く"
    anki = learning.render_anki(words).splitlines()
    assert anki[0] == "#separator:tab" and anki[-1].split("\t")[:3] == [
        "届く",
        "とどく",
        "to reach",
    ]
    assert len(anki[-1].split("\t")) == 6


def test_flashcard_exports_include_both_directions_and_examples():
    words = [
        {
            "word": "見積もり",
            "reading": "みつもり",
            "gloss": "business quote",
            "example_ja": "見積もりを\t送ります。",
            "example_en": "I will\nsend the quote.",
        }
    ]
    anki = learning.render_flashcards_anki(words)
    assert "#notetype:Basic (and reversed card)\n" in anki
    assert "#guid column:1\n" in anki
    assert "#columns:GUID\tFront\tBack\tTags\n" in anki
    cards = list(csv.reader(io.StringIO(anki), delimiter="\t"))[7:]
    assert len(cards) == 1 and len(cards[0]) == 4
    assert cards[0][1] == "見積もり (みつもり)"
    assert "見積もりを\t送ります。" in cards[0][2]
    assert "I will\nsend the quote." in cards[0][2]
    rows = list(csv.reader(io.StringIO(learning.render_flashcards_csv(words))))
    assert rows[0] == ["front", "back", "tags"]
    assert rows[1][0] == "見積もり (みつもり)" and rows[2][0] == "business quote"
    assert "I will\nsend the quote." in rows[1][1]


def test_flashcard_anki_guid_is_stable_and_distinct_for_shared_meanings():
    first = {
        "word": "届く",
        "reading": "とどく",
        "gloss": "to arrive",
        "example_ja": "",
        "example_en": "",
    }
    second = {**first, "word": "着く", "reading": "つく"}
    again = learning.render_flashcards_anki([first, second])
    rows = list(csv.reader(io.StringIO(again), delimiter="\t"))[7:]
    assert len(rows) == 2 and rows[0][0] != rows[1][0]
    assert (
        rows
        == list(
            csv.reader(
                io.StringIO(learning.render_flashcards_anki([first, second])), delimiter="\t"
            )
        )[7:]
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session_archive, "_SESSIONS_DIR", root)
    monkeypatch.setattr(session_archive, "_INDEX_PATH", root / "index.json")
    monkeypatch.setattr("mimivox.api_adapter.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(api_state, "events", [])
    monkeypatch.setattr(api_state, "session_id", None)
    monkeypatch.setattr(api_state, "settings", default_settings())
    monkeypatch.setattr(api_state, "session_pair", DEFAULT_PAIR)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_vocabulary_endpoint_and_export_for_archived_session(client, tmp_path, monkeypatch):
    settings = {
        "glossary_profiles": [
            {"name": "Sales", "entries": [{"source": "見積もり", "target": "business quote"}]}
        ],
        "active_glossary_profile": "Sales",
    }
    session_archive.archive_session("s1", _events(), settings=settings, label="x")
    monkeypatch.setattr(
        api_state,
        "settings",
        {
            "glossary_profiles": [
                {"name": "Unrelated", "entries": [{"source": "見積もり", "target": "unrelated"}]}
            ],
            "active_glossary_profile": "Unrelated",
        },
    )
    body = client.post("/api/session/vocabulary", json={"session_id": "s1"}).json()
    assert body["count"] == len(body["words"]) > 3
    quote = next(word for word in body["words"] if word["word"] == "見積もり")
    assert quote["gloss"] == "business quote"
    r = client.post("/api/session/vocabulary/export?format=anki&session_id=s1")
    path = Path(r.json()["path"])
    assert (
        r.status_code == 200
        and path.name == "s1-vocabulary-anki.txt"
        and "見積もり" in path.read_text("utf-8")
    )
    cards = client.post("/api/session/vocabulary/export?format=anki-cards&session_id=s1")
    card_path = Path(cards.json()["path"])
    assert card_path.name == "s1-flashcards-anki.txt"
    assert "#columns:GUID\tFront\tBack\tTags" in card_path.read_text("utf-8")
    assert "business quote" in card_path.read_text("utf-8")
    csv_cards = client.post("/api/session/vocabulary/export?format=csv-cards&session_id=s1")
    csv_path = Path(csv_cards.json()["path"])
    exported_cards = list(csv.reader(io.StringIO(csv_path.read_text("utf-8"))))
    assert csv_path.name == "s1-flashcards.csv"
    assert exported_cards[0] == ["front", "back", "tags"]
    assert any(
        row[0].startswith("見積もり") and "business quote" in row[1] for row in exported_cards
    )
    session_archive.archive_session("old", _events(), settings={}, label="old")
    old = client.post("/api/session/vocabulary", json={"session_id": "old"}).json()
    old_quote = next(word for word in old["words"] if word["word"] == "見積もり")
    assert old_quote["gloss"] != "unrelated"


def test_archived_english_to_japanese_vocabulary(client):
    english = "We signed a contract today."
    japanese = "今日は契約に署名しました。"
    events = [
        {"type": "AsrCompletedEvent", "index": 0, "start": 0, "text": english},
        {
            "type": "TranslationCompletedEvent",
            "index": 0,
            "start": 0,
            "text": japanese,
            "source_indexes": [0],
        },
    ]
    settings = {
        "source_lang": "en",
        "target_lang": "ja",
        "glossary_profiles": [
            {
                "name": "Legal",
                "entries": [
                    {"source": "contract", "target": "契約"},
                    {"source": "予算", "target": "budget"},
                ],
            }
        ],
        "active_glossary_profile": "Legal",
    }
    session_archive.archive_session("en-ja", events, settings=settings, label="x")
    words = client.post("/api/session/vocabulary", json={"session_id": "en-ja"}).json()["words"]
    contract = next(word for word in words if word["word"] == "契約")
    assert contract["gloss"] == "contract"
    assert contract["example_ja"] == japanese and contract["example_en"] == english
    assert client.post("/api/session/vocabulary/export?format=pdf&session_id=s1").status_code == 400
    assert client.post("/api/session/vocabulary", json={"session_id": "nope"}).status_code == 404


def test_vocabulary_for_live_session(client):
    api_state.session_id, api_state.events = "live", _events()
    assert client.post("/api/session/vocabulary", json={}).json()["count"] > 3


def test_live_vocabulary_uses_resolved_english_to_japanese_pair(client, monkeypatch):
    english = "We signed a contract today."
    japanese = "今日は契約に署名しました。"
    api_state.session_id = "live-en-ja"
    api_state.events = [
        {"type": "AsrCompletedEvent", "index": 0, "start": 0, "text": english},
        {
            "type": "TranslationCompletedEvent",
            "index": 0,
            "start": 0,
            "text": japanese,
            "source_indexes": [0],
        },
    ]
    monkeypatch.setattr(api_state, "session_pair", parse_pair("en", "ja"))
    monkeypatch.setattr(
        api_state,
        "settings",
        {
            "source_lang": "ja",
            "target_lang": "en",
            "glossary_profiles": [
                {"name": "Legal", "entries": [{"source": "contract", "target": "契約"}]}
            ],
            "active_glossary_profile": "Legal",
        },
    )
    words = client.post("/api/session/vocabulary", json={}).json()["words"]
    contract = next(word for word in words if word["word"] == "契約")
    assert contract["gloss"] == "contract"
    assert contract["example_ja"] == japanese and contract["example_en"] == english


def test_learning_mode_defaults_off():
    from mimivox.settings_store import default_settings

    assert default_settings()["learning_mode"] is False
