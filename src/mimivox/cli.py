"""mimivox.cli — Command-line entry point for the JA→EN pipeline."""

from __future__ import annotations

import argparse
import asyncio

from mimivox.pipeline import load_all_models, run


def main() -> None:
    parser = argparse.ArgumentParser(description="Parallel JA→EN meeting translation")
    parser.add_argument("input", nargs="?", help="WAV file path")
    parser.add_argument("--live", action="store_true", help="Live mic capture")
    parser.add_argument("--asr-only", action="store_true", help="Transcribe only")
    parser.add_argument("--device", default=None, help="Audio input device name or index")
    parser.add_argument(
        "--list-devices", action="store_true", help="List audio input devices and exit"
    )
    parser.add_argument(
        "--glossary-profile", default=None, help="Glossary profile name from settings"
    )
    parser.add_argument(
        "--glossary",
        default=None,
        help="Glossary file (.csv/.tsv/.json); overrides settings glossary_path",
    )
    parser.add_argument(
        "--source-lang", default=None, help="Spoken language: ja (default), zh, ko, en"
    )
    parser.add_argument(
        "--target-lang",
        default=None,
        help="Caption language: en (default), ja (with --source-lang en)",
    )
    args = parser.parse_args()

    if args.list_devices:
        try:
            import sounddevice as sd
        except ImportError:
            print("❌  pip install sounddevice")
            raise SystemExit(1) from None
        print("Available input devices:\n")
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                print(f"  {i}: {d['name']}")
        print()
        raise SystemExit(0)

    if not args.live and not args.input:
        parser.print_help()
        raise SystemExit(1)

    if not args.asr_only:
        print("🌐 Translator: cat (local)")
    from mimivox.settings_store import apply_settings_for_session, load_settings

    settings = load_settings()
    from mimivox.languages import UnsupportedPair, resolve_pair
    from mimivox.pair_translate import unready_reason

    try:
        pair = resolve_pair(
            settings, {"source_lang": args.source_lang, "target_lang": args.target_lang}
        )
    except UnsupportedPair as exc:
        raise SystemExit(f"❌  {exc}") from None
    if blocked := unready_reason(pair):
        raise SystemExit(f"❌  {blocked}")
    settings = {**settings, "source_lang": pair.source, "target_lang": pair.target}
    if args.glossary:
        settings["glossary_path"] = args.glossary
    apply_settings_for_session(settings, args.glossary_profile)
    load_all_models()
    try:
        asyncio.run(
            run(
                wav_path=args.input,
                live=args.live,
                asr_only=args.asr_only,
                audio_device=args.device,
                pair=pair,
            )
        )
    except KeyboardInterrupt:
        print("\n⏹  Stopped.")
