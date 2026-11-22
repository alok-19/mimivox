# Eval harness

Measures the real pipeline on public human speech (Google FLEURS ja_jp dev,
CC-BY-4.0) so a feature can be judged by numbers. Rerun it before and after a
change and compare the two `report.json` files.

```bash
export PYTHONPATH=src
PY=.venv/bin/python
$PY scripts/eval/fetch_fleurs.py                  # once; --langs ja_jp zh_cn ko_kr for more
$PY scripts/eval/build_meetings.py -n 5           # data/eval/meetings/meeting-N.{wav,json}
$PY scripts/eval/run_eval.py --clips 100          # loads Whisper + CAT (~20 min)
```

Audio is cached in `~/Library/Caches/mimivox/eval/fleurs/` (download is
SHA-256 checked, a second fetch downloads nothing). Meetings and reports live
under `data/eval/` (git-ignored).

## What it does

- **Clips**: samples N clips (one recording per sentence id, `--seed`), runs each
  as its own replay-file session through `pipeline.run`, and scores the
  concatenated caption rows.
- **Meetings**: concatenates clips with 0.6-1.5 s gaps, alternating a male (A)
  and a female (B) clip, so turns are known. The sidecar JSON has the same
  shape as `make_meeting_fixture.py` (`lines[]` of speaker, ja, start_s,
  end_s) plus the English reference. Meetings are fed at 1x real time so
  perceived latency is honest (`--fast-meetings` skips pacing; perceived then
  includes queue backlog).
- The translation cache is reset before each session, and `default_settings()`
  is used instead of your settings file, so runs are repeatable.

## Metrics

| metric | definition |
|---|---|
| CER | Corpus edit distance over reference characters. Both sides are NFKC-normalized and stripped of punctuation, symbols and whitespace, so punctuation is never counted. Reference is FLEURS's normalized JA transcript. |
| chrF | sacrebleu corpus chrF (0-100) of the EN captions against the FLEURS en_us text for the same sentence id. |
| latency | p50/p95 over caption rows of ASR, CAT, and perceived (VAD wait + queue wait + ASR + CAT). |
| speaker acc | Fraction of meeting lines labelled correctly under the best mapping of true speakers to predicted labels. Only when `data/models/wespeaker/voxceleb_resnet34_LM.onnx` exists. |
| revisions | Fragments merged back into an earlier caption. |
| missed lines | Meeting lines with no caption row overlapping them in time. |

Meeting alignment assigns each caption row to the ground-truth line it overlaps
most in time (row start is the pipeline's `timeline_s`, end is start +
`duration_s`). A line's hypothesis is its assigned rows joined in order; a
missed line scores as an empty hypothesis. Rows overlapping no line are counted
as `unaligned_rows` in `report.json`.

## Flags

`--clips N` (0 skips), `--seed`, `--no-meetings`, `--fast-meetings`,
`--glossary file.csv` (same loader as the CLI), `--meetings-dir`, `--out`,
`--raw-levels` (feed clips at FLEURS's own levels, about -60 dBFS for the
female ja_jp clips, to test quiet speakers).

## Caveats

- FLEURS has no speaker id. "Speaker A/B" is the gender role, so two male clips
  can be different people. Speaker accuracy is a lower bound on diarization
  quality in that case.
- Whisper may write numerals as digits where FLEURS normalizes differently
  (and the reverse), which inflates CER a little.
- FLEURS sentences are read text, not conversation: expect better ASR than a
  real meeting, and no overlapping speech.
