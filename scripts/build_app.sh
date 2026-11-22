#!/usr/bin/env bash
# Build dist/MimiVox.app: the Swift client plus a self-contained, relocatable
# CPython with mimivox installed. Rerunnable; each run rebuilds the bundle.
#
#   scripts/build_app.sh            # -> dist/MimiVox.app
#   PYTHON_VERSION=3.12 scripts/build_app.sh
#
# Needs: Xcode 26+ (not just the Command Line Tools) and uv. Not notarized (issue #37).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$ROOT/macos/MeetingTranslator"
APP="$ROOT/dist/MimiVox.app"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

command -v uv >/dev/null || { echo "uv not found (https://docs.astral.sh/uv/)" >&2; exit 1; }

# SwiftUI's macros (@State, @Entry, ...) compile through a plugin that ships only
# inside Xcode.app; the Command Line Tools alone fail deep into the build.
DEV_DIR="$(xcode-select -p 2>/dev/null || true)"
if [[ ! -d "$DEV_DIR/Platforms/MacOSX.platform" ]]; then
  echo "MimiVox needs Xcode 26 or later, not just the Command Line Tools (developer dir: ${DEV_DIR:-none})." >&2
  echo "Install Xcode, then: sudo xcode-select -s /Applications/Xcode.app" >&2
  exit 1
fi
SDK_MAJOR="$(xcrun --sdk macosx --show-sdk-version | cut -d. -f1)"
if (( SDK_MAJOR < 26 )); then
  echo "MimiVox needs the macOS 26 SDK or later (Xcode 26+); found SDK $SDK_MAJOR." >&2
  exit 1
fi

# SwiftPM stamps the binary with the deployment target as its SDK version
# (sdk 14.0). AppKit then draws the app in the pre-macOS 26 compatibility look,
# without Liquid Glass. Stamp the SDK actually used instead.
SDK_VERSION="$(xcrun --sdk macosx --show-sdk-version)"
MIN_MACOS="14.0"
SWIFT_FLAGS=(-c release --package-path "$PKG"
  -Xlinker -platform_version -Xlinker macos -Xlinker "$MIN_MACOS" -Xlinker "$SDK_VERSION")

echo "==> swift build -c release (macOS $MIN_MACOS+, SDK $SDK_VERSION)"
swift build "${SWIFT_FLAGS[@]}"
BUILD_DIR="$(swift build "${SWIFT_FLAGS[@]}" --show-bin-path)"
BIN="$BUILD_DIR/MeetingTranslator"

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/MeetingTranslator"
cp "$PKG/BundleResources/Info.plist" "$APP/Contents/Info.plist"
cp "$PKG/BundleResources/MeetingTranslator.icns" "$APP/Contents/Resources/"
cp -R "$BUILD_DIR/MeetingTranslator_MeetingTranslatorCore.bundle" "$APP/Contents/Resources/"
# In-app help and changelog (Help menu, About window).
cp "$PKG"/Sources/MeetingTranslatorApp/Resources/*.md "$APP/Contents/Resources/"
PB=/usr/libexec/PlistBuddy
"$PB" -c "Set :CFBundleName MimiVox" "$APP/Contents/Info.plist"
"$PB" -c "Set :CFBundleDisplayName MimiVox" "$APP/Contents/Info.plist"
"$PB" -c "Set :CFBundleIdentifier local.mimivox.app" "$APP/Contents/Info.plist"
# Enterprise builds: MIMIVOX_MODEL_ENDPOINT=https://mirror.example.com bakes a
# Hugging Face mirror in as the default download source (see docs/model-mirror.md).
if [[ -n "${MIMIVOX_MODEL_ENDPOINT:-}" ]]; then
  [[ "$MIMIVOX_MODEL_ENDPOINT" =~ ^https?://[^/?#]+ ]] || { echo "MIMIVOX_MODEL_ENDPOINT must be an http(s) URL" >&2; exit 1; }
  "$PB" -c "Add :MIMIVOXModelEndpoint string ${MIMIVOX_MODEL_ENDPOINT%/}" "$APP/Contents/Info.plist"
  echo "==> model downloads default to $MIMIVOX_MODEL_ENDPOINT"
fi

echo "==> bundling python $PYTHON_VERSION (uv-managed python-build-standalone)"
uv python install --managed-python "$PYTHON_VERSION"
# Resolve from a neutral cwd so uv does not pick an ancestor .venv.
PY_SRC="$(cd / && uv python find --managed-python --no-project "$PYTHON_VERSION")"
PY_SRC="$(cd "$(dirname "$PY_SRC")/.." && pwd -P)"   # install root (bin/ + lib/)
PY_DST="$APP/Contents/Resources/python"
cp -R "$PY_SRC" "$PY_DST"
mkdir -p "$ROOT/build"   # setup.cfg points egg_base at build/
# Runtime deps pinned by uv.lock (no dev extras), then mimivox itself,
# non-editable so nothing points back at the repo.
REQS="$(mktemp)"
uv export --project "$ROOT" --frozen --no-dev --no-emit-project --no-hashes -o "$REQS" >/dev/null
uv pip install --python "$PY_DST/bin/python3" --break-system-packages --no-cache -r "$REQS"
uv pip install --python "$PY_DST/bin/python3" --break-system-packages --no-cache --no-deps "$ROOT"
rm -f "$REQS"
rm -rf "$ROOT/build" "$ROOT"/src/*.egg-info

echo "==> trimming what the app never imports"
# mlx-whisper requires torch but only its checkpoint converter imports it;
# sympy, networkx and mpmath are there only for torch. pip and setuptools
# install packages, which a built bundle never does.
uv pip uninstall --python "$PY_DST/bin/python3" --break-system-packages \
  torch sympy networkx mpmath pip setuptools
SITE="$PY_DST/lib/python$PYTHON_VERSION/site-packages"
find "${SITE:?}" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} +
rm -rf "${SITE:?}/mlx/include" "${SITE:?}/mlx/lib/cmake" "${SITE:?}/build" \
  "${PY_DST:?}/include" "${PY_DST:?}/lib/pkgconfig" \
  "${PY_DST:?}"/lib/{tcl,tk,itcl,thread}[0-9]* "${PY_DST:?}"/lib/libtcl* \
  "${PY_DST:?}/lib/python$PYTHON_VERSION"/{idlelib,ensurepip,tkinter,lib2to3,pydoc_data,turtledemo} \
  "${PY_DST:?}/lib/python$PYTHON_VERSION"/lib-dynload/_tkinter*
# Fail the build here, not on a user's Mac, if the trim removed something used.
APP_IMPORTS="import mimivox.pipeline, mimivox.server, mlx_whisper, mlx_lm, onnxruntime, sudachipy; mimivox.vad.SileroOnnx(); import mimivox.api_adapter as a"
"$PY_DST/bin/python3" -I -c "$APP_IMPORTS"

echo "==> precompiling bytecode"
# The app starts Python with PYTHONDONTWRITEBYTECODE=1 so nothing is written
# into the signed bundle; compile everything now so startup still uses .pyc.
# unchecked-hash: the signature already pins the sources, so a copy that resets
# file times (plain cp -R) must not make every .pyc stale.
"$PY_DST/bin/python3" -I -m compileall -q -f -j 0 --invalidation-mode unchecked-hash "$PY_DST/lib"

echo "==> ad-hoc codesign"
# Sign every nested Mach-O first (--deep does not reach arbitrary Resources dirs).
find "$PY_DST" -type f -print0 \
  | xargs -0 -P 8 -n 50 sh -c 'for f; do if file -b "$f" | grep -q "Mach-O"; then codesign --force -s - "$f" 2>/dev/null; fi; done' _
codesign --force --deep -s - "$APP"
codesign --verify --deep "$APP"
# What the app does at launch must leave the seal intact.
(cd / && PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 "$PY_DST/bin/python3" -c "$APP_IMPORTS")
codesign --verify --deep "$APP" || { echo "importing the app wrote into the signed bundle" >&2; exit 1; }

echo "==> done: $APP ($(du -sh "$APP" | cut -f1))"
