import random
import wave
from pathlib import Path

import numpy as np
import pytest

from mimivox.eval import metrics, scoring
from mimivox.eval.fleurs import Clip, load_clips, parse_tsv
from mimivox.eval.meetings import (
    GAP_RANGE_S,
    SAMPLE_RATE,
    normalize_rms,
    plan_meeting,
    render_meeting,
    write_meeting,
)

LINES = [
    {"speaker": "A", "ja": "あ", "en": "a", "start_s": 1.0, "end_s": 4.0},
    {"speaker": "B", "ja": "い", "en": "b", "start_s": 5.0, "end_s": 8.0},
    {"speaker": "A", "ja": "う", "en": "c", "start_s": 9.0, "end_s": 12.0},
]


def test_normalize_ja_drops_punctuation_symbols_and_spaces():
    assert metrics.normalize_ja("場に、ふさわしい。 ７,000！") == "場にふさわしい7000"


def test_cer_ignores_punctuation_and_counts_edits():
    assert metrics.cer(["こんにちは"], ["こんにちは。"]) == 0.0
    assert metrics.cer(["こんにちは"], ["こんばんは"]) == pytest.approx(2 / 5)
    assert metrics.cer(["ab", "cd"], ["", "cd"]) == pytest.approx(2 / 4)  # empty hyp is all errors


def test_chrf_identical_is_100_and_different_is_lower():
    assert metrics.chrf(["hello world"], ["hello world"]) == pytest.approx(100.0)
    assert metrics.chrf(["hello world"], ["goodbye moon"]) < 30


def test_percentile_empty_is_nan():
    assert np.isnan(metrics.percentile([], 50))
    assert metrics.percentile([1, 2, 3], 50) == 2


def row(start, end, speaker=None, ja="x", en="y"):
    return metrics.Row(start, end, ja, en, speaker)


def test_align_groups_rows_with_lines_they_overlap_and_reports_misses():
    rows = [row(0.8, 2.0), row(2.1, 4.2), row(5.2, 7.5), row(30.0, 31.0)]
    al = metrics.align_rows(rows, LINES)
    assert [(g.lines, len(g.rows)) for g in al.groups] == [([0], 2), ([1], 1), ([2], 0)]
    assert al.missed_lines == 1
    assert len(al.unaligned) == 1


def test_row_spanning_two_lines_joins_them_into_one_group():
    al = metrics.align_rows([row(3.0, 7.0), row(9.5, 11.0)], LINES)
    assert [(g.lines, len(g.rows)) for g in al.groups] == [([0, 1], 1), ([2], 1)]
    assert al.missed_lines == 0


def test_row_from_log_prefers_timeline_and_falls_back_to_audio_ts():
    r = metrics.Row.from_log({"timeline_s": 3.25, "duration_s": 2.0, "ja": "あ", "en": "a"})
    assert (r.start_s, r.end_s) == (3.25, 5.25)
    r = metrics.Row.from_log(
        {"audio_ts": "01:05", "duration_s": 1.0, "speaker": {"id": "speaker-2"}, "merged_ids": [4]}
    )
    assert (r.start_s, r.speaker, r.merged) == (65.0, "speaker-2", True)


def test_speaker_accuracy_uses_best_label_permutation():
    truth = ["A", "B", "A", "B"]
    assert metrics.speaker_accuracy(truth, ["x", "y", "x", "y"]) == 1.0
    assert metrics.speaker_accuracy(truth, ["y", "x", "y", "x"]) == 1.0  # swapped names
    assert metrics.speaker_accuracy(truth, ["x", "x", "x", "x"]) == 0.5  # one cluster
    assert metrics.speaker_accuracy(truth, [None] * 4) == 0.0
    assert metrics.speaker_accuracy(truth, ["x", "y", "z", "y"]) == 0.75  # extra label


def log(start, dur, ja, en, spk=None, **kw):
    r = {
        "timeline_s": start,
        "duration_s": dur,
        "ja": ja,
        "en": en,
        "asr_latency_s": 0.5,
        "translate_latency_s": 0.25,
        "vad_wait_s": 0.5,
        "queue_wait_s": 0.0,
        "speaker": {"id": spk, "label": spk} if spk else None,
    }
    r.update(kw)
    return r


def test_score_meeting_alignment_speakers_and_revisions():
    rows = [
        log(1.0, 3.0, "あ", "a", "s1"),
        log(5.0, 1.5, "い", "b", "s2", merged_ids=[9]),
        log(6.5, 1.5, "ん", "", "s2"),
    ]  # fragment lands on line B too
    m = scoring.score_meeting({"lines": LINES}, rows)
    assert m["missed_lines"] == 1  # line C has no rows
    assert m["refs_ja"] == ["あ", "い", "う"]
    assert m["hyps_ja"] == ["あ", "いん", ""]
    assert m["speaker_accuracy"] == pytest.approx(2 / 3)
    assert m["revisions"] == 1
    agg = scoring.aggregate_meetings([m])
    assert agg["lines"] == 3 and agg["missed_lines"] == 1
    assert agg["latency"]["perceived_s"]["p50"] == pytest.approx(1.25)


def test_merged_row_is_scored_against_both_lines_and_speaker_per_line():
    m = scoring.score_meeting({"lines": LINES[:2]}, [log(1.0, 7.0, "あい", "a b", "s1")])
    assert m["refs_ja"] == ["あい"] and m["hyps_ja"] == ["あい"]
    assert m["refs_en"] == ["a b"] and m["missed_lines"] == 0
    assert m["speaker_accuracy"] == 0.5  # one label cannot cover two speakers


def test_score_meeting_without_speaker_labels_reports_none():
    m = scoring.score_meeting({"lines": LINES}, [log(1.0, 3.0, "あ", "a")])
    assert m["speaker_accuracy"] is None


def test_score_clips_joins_rows_and_counts_empty():
    items = [
        (
            {"sid": "1", "wav": "a.wav", "ja_norm": "あい", "en": "ab"},
            [log(0, 1, "あ", "a"), log(1, 1, "い", "b")],
        ),
        ({"sid": "2", "wav": "b.wav", "ja_norm": "う", "en": "c"}, []),
    ]
    res = scoring.score_clips(items)
    assert res["empty"] == 1
    assert res["per_clip"][0]["ja_hyp"] == "あい"
    assert res["per_clip"][0]["en_hyp"] == "a b"
    assert res["cer"] == pytest.approx(1 / 3)
    assert "| clips | 2 |" in scoring.markdown_table(res, None)


def _wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(np.full(int(seconds * SAMPLE_RATE), 1000, np.int16).tobytes())


def _clips(tmp_path, n_per_gender=4, seconds=5.0):
    clips = []
    for g in ("MALE", "FEMALE"):
        for i in range(n_per_gender):
            p = tmp_path / f"{g}{i}.wav"
            _wav(p, seconds)
            clips.append(
                Clip(f"{g[0]}{i}", p, f"ja{i}", f"ja{i}", f"en{i}", g, int(seconds * SAMPLE_RATE))
            )
    return clips


def test_meeting_builder_alternates_speakers_with_gaps_and_truth_matches_audio(tmp_path):
    clips = _clips(tmp_path)
    rng = random.Random(1)
    plan = plan_meeting(clips, rng, target_s=(20.0, 20.0))
    assert [c.gender for c in plan] == ["MALE", "FEMALE"] * (len(plan) // 2) + (
        ["MALE"] if len(plan) % 2 else []
    )
    assert len({c.sid for c in plan}) == len(plan)
    pcm, truth = render_meeting(plan, rng)
    lines = truth["lines"]
    assert [ln["speaker"] for ln in lines][:2] == ["A", "B"]
    for a, b in zip(lines, lines[1:]):
        assert GAP_RANGE_S[0] - 0.02 <= b["start_s"] - a["end_s"] <= GAP_RANGE_S[1] + 0.02
    first = lines[0]  # the truth span is exactly the clip's audio
    seg = pcm[int(first["start_s"] * SAMPLE_RATE) : int(first["end_s"] * SAMPLE_RATE)]
    assert (seg != 0).mean() > 0.99
    sidecar = write_meeting(tmp_path / "m" / "meeting-1.wav", pcm, truth)
    assert sidecar.name == "meeting-1.json" and sidecar.read_text().count('"speaker"') == len(lines)


def test_plan_prefers_least_used_clips_across_meetings(tmp_path):
    clips = _clips(tmp_path)
    counts: dict = {}
    rng = random.Random(0)
    plan_meeting(clips, rng, counts, target_s=(10.0, 10.0))
    used = set(counts)
    second = plan_meeting(clips, rng, counts, target_s=(10.0, 10.0))
    assert all(c.wav not in used for c in second)


def test_parse_and_load_clips_pair_by_sentence_id(tmp_path):
    def tsv(lang, rows):
        d = tmp_path / lang
        d.mkdir(parents=True)
        (d / "dev.tsv").write_text("\n".join("\t".join(r) for r in rows), encoding="utf-8")

    tsv(
        "ja_jp",
        [
            ["7", "a.wav", "こん「に」ち", "こん に ち", "ph", "32000", "MALE"],
            ["8", "b.wav", "x", "x", "ph", "16000", "FEMALE"],
        ],
    )
    tsv("en_us", [["7", "q.wav", 'Hello "there"', "hello there", "ph", "1", "MALE"]])
    (tmp_path / "ja_jp" / "dev").mkdir()
    _wav(tmp_path / "ja_jp" / "dev" / "a.wav", 1)
    assert parse_tsv(tmp_path / "ja_jp" / "dev.tsv")[0]["raw"] == "こん「に」ち"
    clips = load_clips(tmp_path)
    assert [(c.sid, c.en, c.duration_s) for c in clips] == [("7", 'Hello "there"', 2.0)]


def test_normalize_rms_levels_quiet_audio_without_clipping():
    quiet = (np.sin(np.linspace(0, 200, 16000)) * 30).astype(np.int16)
    out = normalize_rms(quiet, -23.0)
    rms_db = 20 * np.log10(np.sqrt((out.astype(float) ** 2).mean()) / 32768)
    assert rms_db == pytest.approx(-23.0, abs=0.3)
    spiky = np.zeros(1000, np.int16)
    spiky[0] = 20000
    spiky[1:] = 10
    assert np.abs(normalize_rms(spiky, -3.0).astype(int)).max() <= 32767
    assert (normalize_rms(np.zeros(10, np.int16)) == 0).all()


def test_render_meeting_levels_clips_and_raw_levels_keep_them(tmp_path):
    clips = _clips(tmp_path, n_per_gender=1, seconds=1.0)  # constant 1000-amplitude clips
    plan = clips[:2]
    leveled, truth = render_meeting(plan, random.Random(0))
    raw, _ = render_meeting(plan, random.Random(0), None)
    first = truth["lines"][0]
    mid = (int(first["start_s"] * SAMPLE_RATE) + int(first["end_s"] * SAMPLE_RATE)) // 2
    assert raw[mid] == 1000 and leveled[mid] != 1000


def test_cer_fold_case_only_when_asked():
    assert metrics.cer(["hello world"], ["Hello World"]) > 0
    assert metrics.cer(["hello world"], ["Hello World"], fold_case=True) == 0.0


def test_fetch_audio_limited_stops_after_limit(tmp_path, monkeypatch):
    import importlib.util
    import io
    import tarfile

    spec = importlib.util.spec_from_file_location(
        "fetch_fleurs", Path(__file__).parent.parent / "scripts" / "eval" / "fetch_fleurs.py"
    )
    ff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff)

    cache = tmp_path / "cache"
    (cache / "ko_kr").mkdir(parents=True)
    rows = [f"{i}\tw{i}.wav\traw\tnorm\tph\t16000\tFEMALE" for i in range(5)]
    (cache / "ko_kr" / "dev.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ["../evil.wav", "dev/w3.wav", "dev/w1.wav", "dev/w0.wav", "dev/other.wav"]:
            data = b"RIFF" + b"\0" * 8
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    monkeypatch.setattr(ff.urllib.request, "urlopen", lambda url: buf)
    monkeypatch.setattr(ff, "ensure_pcm16", lambda p: None)
    ff.fetch_audio_limited(cache, "ko_kr", 2)
    got = sorted(p.name for p in (cache / "ko_kr" / "dev").iterdir())
    assert got == ["w1.wav", "w3.wav"]
    assert not (tmp_path / "evil.wav").exists()
