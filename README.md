<p align="center">
  <img src="docs/images/mimivox-logo-uwu.png" alt="MimiVox" width="480">
</p>

<p align="center">
  <b>Live meeting captions and translation, fully on your Mac.</b><br>
  Apple Silicon · MLX · no audio or text leaves the machine<br><br>
  <a href="https://alok-19.github.io/mimivox/"><b>alok-19.github.io/mimivox</b></a>
</p>

---

MimiVox listens to a meeting (Zoom, Teams, Meet, your microphone, or a recording), transcribes
it, and shows translated captions as people speak. Everything runs locally on Apple Silicon
once the models are downloaded.

| You hear | Captions in | Translation model |
|---|---|---|
| Japanese | English | CAT-Translate 1.4B (the main, most-tuned path) |
| English | Japanese | Qwen3-4B |
| Chinese | English | Qwen3-4B |
| Korean | English | Qwen3-4B |

Beyond captions you get floating captions, meeting notes and Q&A over the transcript,
glossaries, speaker labels, a searchable meeting library and a Japanese learning mode. See
[docs/features.md](docs/features.md).

## Get the app

Requires an Apple Silicon Mac on macOS 14+, [uv](https://docs.astral.sh/uv/) and
**Xcode 26+**. The Command Line Tools alone can't compile the SwiftUI app.

```bash
brew install uv ffmpeg
git clone https://github.com/alok-19/mimivox && cd mimivox
make app        # builds dist/MimiVox.app and opens it
```

On first launch, open **Home** and download the caption models (about 1.7 GB). To try it without
a meeting, run `make fixture` and pick the **Replay** source.

<details>
<summary>macOS says the app can't be opened</summary>

A copy you build yourself runs as is. The bundle is not notarized, so a downloaded copy is
blocked. Run this once, or use **Open Anyway** in System Settings → Privacy & Security:

```bash
xattr -dr com.apple.quarantine /Applications/MimiVox.app
```
</details>

## Develop

```bash
make setup      # install deps into .venv (uv)
make app-dev    # run the macOS app with its local helper
make check      # lint + tests, run before a PR
```

For API development, set `MIMIVOX_API_TOKEN` and run `make server`. The standalone
server listens on loopback. Send the token in the `Authorization: Bearer` header.

Run `make` to list every command:

| Command | What it does |
|---|---|
| `make setup` | Install Python deps and dev tools into `.venv` |
| `make server` | Run the authenticated local API |
| `make app-dev` | Run the SwiftUI app and local helper from source |
| `make app` | Build and open `dist/MimiVox.app` |
| `make test` / `make lint` / `make fmt` | pytest, ruff check, ruff fix |
| `make swift-check` | Build the Swift app and run its checks |
| `make fixture` | Generate the sample recording for the Replay source |
| `make eval` | Score the real pipeline on public speech (~20 min, [docs/eval.md](docs/eval.md)) |

The CLI works without the app:

```bash
uv run mimivox data/fixtures/meeting-recording.wav     # a recording
uv run mimivox --live                                  # the microphone
uv run mimivox meeting.wav --source-lang zh            # Chinese → English
uv run mimivox meeting.wav --glossary terms.csv        # with a glossary
```

### How it fits together

```
audio ─▶ Silero VAD ─▶ mlx-whisper ─▶ CAT-Translate / Qwen ─▶ captions
                                                   │
                       FastAPI server(src/mimivox) ┴─▶ macOS app · terminal
```

The SwiftUI app starts its own Python helper on a free local port. It checks the helper's
TLS certificate and sends a token with each request. Details are in [docs/architecture.md](docs/architecture.md) and
[macos/README.md](macos/README.md).

```
src/mimivox/      Python package
  pipeline.py       async orchestration (workers, queues, sessions)
  translate.py      CAT-Translate, context, post-processing
  api_adapter.py    /api/* used by the macOS app
  server.py         authenticated local API
macos/            SwiftUI app
scripts/          app build, fixtures, eval, diagnostics
tests/            pytest suite
docs/             architecture and feature notes
```

### Contributing

1. Branch from `main`.
2. Make the change with a test in `tests/` that shows the behavior.
3. Run `make check`. If you touched `macos/`, run `make swift-check` too.
4. If the change affects caption quality or latency, run `make eval` before and after and
   put both numbers in the PR.
5. Open a PR against `main` that says what changed and why.

Data from a source run lives in `data/` (git-ignored). Set `MIMIVOX_DATA_DIR` to put it
elsewhere. Anything derived from a session should register
`session_archive.register_delete_hook` so deletes and retention reach it
([docs/privacy.md](docs/privacy.md)).

## Docs

- [Features](docs/features.md): captions, glossaries, notes, library, learning mode, APIs
- [Privacy](docs/privacy.md): offline lock, retention, what is stored
- [Language pairs](docs/language-pairs.md)
- [Architecture](docs/architecture.md)
- [Eval harness](docs/eval.md)
- [Model mirror for enterprise builds](docs/model-mirror.md)
- [macOS app: build details and shortcuts](macos/README.md)

## Models

| Component | Model |
|---|---|
| Speech recognition | `LibraxisAI/whisper-large-v3-turbo-mlx-q8` |
| JA→EN translation | `hotchpotch/CAT-Translate-1.4b-mlx-q4` |
| Other pairs, notes, Ask (optional download) | Qwen3-4B-Instruct-2507, 4-bit MLX |
| Voice activity | Silero VAD |
| Speaker labels (optional) | WeSpeaker ResNet34-LM ONNX ([notices](THIRD_PARTY_NOTICES.md)) |
| Learning mode | SudachiPy and a JMdict subset |

## License

[MIT](LICENSE)
