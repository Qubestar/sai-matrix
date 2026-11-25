#!/bin/bash
# Build SaiMatrix-1.0.0-universal.dmg: a universal2 macOS app with a bundled
# universal2 Python runtime, signed with a Developer ID Application identity and
# notarized/stapled.
#
# Usage:
#   scripts/build-dmg.sh               # full build, sign + notarize + staple + SHA256
#   scripts/build-dmg.sh --skip-sign   # local dry run (same layout, no signing)
#   SKIP_SIGN=1 scripts/build-dmg.sh
#
# Environment:
#   SAI_MATRIX_IDENTITY     Developer ID identity (default: auto-detected)
#   NOTARY_PROFILE          notarytool keychain profile (default: zenborg-notary)
#   SAI_MATRIX_PYTHON_ROOT  prebuilt universal2 runtime root (contains bin/python3)
#   SAI_MATRIX_PYTHON_CACHE download cache dir (default: build/.python-cache)
#
# Packaging runs from committed HEAD (`git archive`), so commit source changes
# before building a release DMG. No credentials live in this script.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="1.0.0"
APP_NAME="Sai Matrix"
APP="$ROOT/build/$APP_NAME.app"
DIST="$ROOT/dist"
BUILD="$ROOT/build"
DMG="$DIST/SaiMatrix-${VERSION}-universal.dmg"
IDENTITY="${SAI_MATRIX_IDENTITY:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-zenborg-notary}"
SKIP_SIGN="${SKIP_SIGN:-0}"

# Pinned python-build-standalone (universal2 built by lipo'ing the two arch builds).
PBS_RELEASE="${SAI_MATRIX_PBS_RELEASE:-20261124}"
PY_VERSION="${SAI_MATRIX_PY_VERSION:-3.11.18}"
SHA_ARM64="e9840948169972436f80241773458cdb95923a639f652c01bdc3f1066e45f113"
SHA_X86_64="3dd4e8c5fb66e8fa9bfcf5cb79d1c199d95aa04e93f9eedcc341e9622812165f"

for arg in "$@"; do
    case "$arg" in
        --skip-sign) SKIP_SIGN=1 ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done
[ "${SKIP_SIGN}" = "1" ] && SKIP_SIGN=1

say() { printf '\n==> %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

mkdir -p "$BUILD" "$DIST"

# ---------- 1. universal2 Python runtime ----------
prepare_python() {
    local pyroot="$1" cache="$2"
    mkdir -p "$cache"
    local arch arm_tgz x86_tgz
    for arch in aarch64 x86_64; do
        local want sha
        if [ "$arch" = "aarch64" ]; then want="$SHA_ARM64"; else want="$SHA_X86_64"; fi
        local tgz="$cache/cpython-${PY_VERSION}-${arch}.tar.gz"
        if [ ! -f "$tgz" ]; then
            local url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PY_VERSION}%2B${PBS_RELEASE}-${arch}-apple-darwin-install_only_stripped.tar.gz"
            say "Downloading Python $PY_VERSION ($arch)"
            curl -fsSL --retry 3 -o "$tgz.part" "$url" || die "download failed: $url"
            mv "$tgz.part" "$tgz"
        fi
        sha="$(shasum -a 256 "$tgz" | awk '{print $1}')"
        [ "$sha" = "$want" ] || die "checksum mismatch for $tgz (got $sha)"
        local dir="$cache/$arch"
        if [ ! -d "$dir/python" ]; then
            mkdir -p "$dir"
            tar xzf "$tgz" -C "$dir"
        fi
    done
    rm -rf "$pyroot"
    cp -R "$cache/aarch64/python" "$pyroot"
    python3 - "$pyroot" "$cache/x86_64/python" <<'PY'
import os, pathlib, subprocess, sys
root, other = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
merged = 0
for dirpath, _dirs, files in os.walk(root):
    for name in files:
        a = pathlib.Path(dirpath) / name
        if a.is_symlink():
            continue
        b = other / a.relative_to(root)
        if not b.is_file() or b.is_symlink():
            continue
        if "Mach-O" in subprocess.run(["file", "-b", str(a)], capture_output=True, text=True).stdout \
           and "Mach-O" in subprocess.run(["file", "-b", str(b)], capture_output=True, text=True).stdout:
            subprocess.run(["lipo", "-create", str(a), str(b), "-output", str(a)], check=True)
            merged += 1
print(f"lipo: merged {merged} Mach-O files into universal2")
PY
    strip_runtime "$pyroot"
    "$pyroot/bin/python3" -I -c 'import sys, sqlite3, ssl, tomllib, http.server, hashlib, json; print("runtime ok:", sys.version.split()[0], sys.prefix)'
    local archs
    archs="$(lipo -archs "$pyroot/bin/python3")"
    case "$archs" in *arm64*) ;; *) die "bundled python is missing arm64";; esac
    case "$archs" in *x86_64*) ;; *) die "bundled python is missing x86_64";; esac
}

strip_runtime() {
    local root="$1"
    python3 - "$root" <<'PY'
import pathlib, shutil, sys
root = pathlib.Path(sys.argv[1])
for rel in ["include", "share", "lib/python3.11/test", "lib/python3.11/site-packages",
            "lib/python3.11/idlelib", "lib/python3.11/tkinter", "lib/python3.11/turtledemo",
            "lib/python3.11/ensurepip", "lib/python3.11/lib2to3", "lib/python3.11/pydoc_data",
            "lib/tcl9", "lib/tcl9.0", "lib/tk9.0", "lib/itcl4.3.8", "lib/thread3.0.6"]:
    p = root / rel
    if p.exists():
        shutil.rmtree(p)
for rel in ["lib/python3.11/turtle.py", "lib/python3.11/lib-dynload/_tkinter.cpython-311-darwin.so",
            "lib/python3.11/lib-dynload/_curses.cpython-311-darwin.so",
            "lib/libtcl9.0.dylib", "lib/libtk9.0.dylib", "lib/libtcl9tk9.0.dylib"]:
    p = root / rel
    if p.exists():
        p.unlink()
for p in root.rglob("__pycache__"):
    shutil.rmtree(p, ignore_errors=True)
for pat in ("idle3*", "2to3*", "pip*", "pydoc3*"):
    for p in (root / "bin").glob(pat):
        p.unlink()
PY
}

# ---------- 2. staging from clean git archive ----------
stage_payload() {
    say "Staging app payload from git archive HEAD"
    rm -rf "$BUILD/payload"
    mkdir -p "$BUILD/payload"
    git -C "$ROOT" archive --format=tar HEAD | tar -x -C "$BUILD/payload"
    rm -rf "$APP"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"
    cp "$ROOT/packaging/macos/Info.plist" "$APP/Contents/Info.plist"
    printf 'APPL????' > "$APP/Contents/PkgInfo"
    for f in server.py index.html token_trim.py token_trim_gemini.py token_trim_hermes.py LICENSE README.md; do
        cp "$BUILD/payload/$f" "$APP/Contents/Resources/app/$f"
    done
}

stage_python() {
    local src="$1"
    say "Bundling universal2 Python runtime"
    rm -rf "$APP/Contents/Resources/python"
    cp -R "$src" "$APP/Contents/Resources/python"
    strip_runtime "$APP/Contents/Resources/python"
}

build_launcher() {
    say "Compiling universal2 launcher"
    swiftc -O -target arm64-apple-macos13.0 -o "$BUILD/launcher-arm64" "$ROOT/packaging/macos/Launcher.swift"
    swiftc -O -target x86_64-apple-macos13.0 -o "$BUILD/launcher-x86_64" "$ROOT/packaging/macos/Launcher.swift"
    lipo -create -output "$APP/Contents/MacOS/SaiMatrix" "$BUILD/launcher-arm64" "$BUILD/launcher-x86_64"
    chmod +x "$APP/Contents/MacOS/SaiMatrix"
}

# ---------- 3. hygiene ----------
verify_hygiene() {
    say "Checking bundle hygiene"
    local bad
    bad="$(find "$APP" \( -name '.data' -o -name 'config.json' -o -name '__pycache__' \
        -o -name '.DS_Store' -o -name '.git' \) -print)"
    [ -z "$bad" ] || die "unwanted files in bundle:\n$bad"
    if find "$APP" -type f -name '*.py' -print0 | xargs -0 grep -l -E '/Users/[^/]+|/Volumes/My Lexar' >/dev/null 2>&1; then
        die "developer home paths found in bundled Python sources"
    fi
    find "$APP/Contents/Resources/python" -name '*.pyc' -print -quit | grep -q . && die "pyc files in bundle"
    lipo -archs "$APP/Contents/MacOS/SaiMatrix" | grep -q arm64 || die "launcher missing arm64"
    lipo -archs "$APP/Contents/MacOS/SaiMatrix" | grep -q x86_64 || die "launcher missing x86_64"
}

# ---------- 4. signing / notarization ----------
find_identity() {
    if [ -n "$IDENTITY" ]; then return; fi
    IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
        | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)"
    [ -n "$IDENTITY" ] || die "no Developer ID Application identity found; set SAI_MATRIX_IDENTITY or use --skip-sign"
}

sign_macho() {
    say "Signing nested Mach-O binaries (inside-out)"
    find "$APP" -type f ! -type l -print0 | while IFS= read -r -d '' f; do
        if file -b "$f" | grep -q 'Mach-O'; then
            codesign --force --options runtime --timestamp --sign "$IDENTITY" "$f"
        fi
    done
    codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP"
}

verify_app() {
    say "Verifying app signature and notarization ticket"
    codesign --verify --deep --strict "$APP"
    xcrun stapler validate "$APP"
    spctl -a -vvv -t install "$APP"
}

notarize_app() {
    say "Notarizing app (this can take a few minutes)"
    local zip="$BUILD/SaiMatrix-app.zip"
    rm -f "$zip"
    ditto -c -k --keepParent "$APP" "$zip"
    xcrun notarytool submit "$zip" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP"
    rm -f "$zip"
}

# ---------- 5. DMG ----------
make_dmg() {
    say "Building $DMG"
    local stage="$BUILD/dmg"
    rm -rf "$stage"
    mkdir -p "$stage"
    ditto "$APP" "$stage/$APP_NAME.app"
    ln -s /Applications "$stage/Applications"
    rm -f "$DMG"
    hdiutil create -volname "$APP_NAME" -srcfolder "$stage" -ov -format UDZO "$DMG" >/dev/null
    rm -rf "$stage"
}

main() {
    local pyroot=""
    if [ -n "${SAI_MATRIX_PYTHON_ROOT:-}" ]; then
        pyroot="$SAI_MATRIX_PYTHON_ROOT"
        [ -x "$pyroot/bin/python3" ] || die "SAI_MATRIX_PYTHON_ROOT has no bin/python3"
    else
        pyroot="$BUILD/python-runtime"
        prepare_python "$pyroot" "${SAI_MATRIX_PYTHON_CACHE:-$BUILD/.python-cache}"
    fi
    stage_payload
    stage_python "$pyroot"
    build_launcher
    verify_hygiene

    if [ "$SKIP_SIGN" = "1" ]; then
        say "SKIP_SIGN: unsigned local build (do not distribute)"
    else
        find_identity
        sign_macho
        codesign --verify --deep --strict "$APP"
        notarize_app
        verify_app
    fi

    make_dmg
    if [ "$SKIP_SIGN" != "1" ]; then
        say "Signing, notarizing and stapling the DMG"
        codesign --force --timestamp --sign "$IDENTITY" "$DMG"
        xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
        xcrun stapler staple "$DMG"
        xcrun stapler validate "$DMG"
        spctl -a -vvv -t install "$DMG"
    fi
    (cd "$DIST" && shasum -a 256 "$(basename "$DMG")") | tee "$DMG.sha256"
    say "Done: $DMG"
}

main
