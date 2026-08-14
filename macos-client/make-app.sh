#!/bin/bash
#
# make-app.sh — package the SwiftPM executable as WorkloadTracker.app.
#
# Plan §12 (Phase 9) of docs/plan-macos-app.md. This is not cosmetic: three
# things in this codebase need a real bundle to work at all — the custom drag
# `UTType`, `@SceneStorage`, and a stable `UserDefaults` domain. See
# macos-client/README.md.
#
# Usage:
#     ./make-app.sh                      # -> macos-client/dist/WorkloadTracker.app
#     ./make-app.sh -o /Applications     # -> /Applications/WorkloadTracker.app
#     ./make-app.sh --identity "Developer ID Application: …"
#     ./make-app.sh --no-hardened-runtime
#
# Two properties this script is written to hold:
#
#   * **Idempotent.** Re-running it rebuilds and replaces the bundle in place,
#     with the same result every time. Nothing is appended or accumulated.
#   * **Never a half-bundle.** Everything is assembled, linted and signed in a
#     private staging directory; the live bundle is only removed once the new one
#     has passed verification, and any failure aborts with the previous bundle
#     untouched. `set -euo pipefail` plus an EXIT trap on the staging dir.
#
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly APP_NAME="WorkloadTracker"
readonly PRODUCT="WorkloadClient"        # SwiftPM's executable target name
readonly LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"

OUT_DIR="$SCRIPT_DIR/dist"
IDENTITY="-"                             # ad-hoc; correct for local use
HARDENED=1

die() { printf '\nmake-app.sh: FAILED: %s\n' "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------- arguments

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)
            [[ $# -ge 2 ]] || die "$1 needs a directory"
            OUT_DIR="$2"; shift 2 ;;
        --identity)
            # Ad-hoc ("-") is the default and is what local use wants. A real
            # Developer ID is accepted but NOT notarized by this script:
            # notarization needs an Apple Developer account and a network round
            # trip, and this app is never distributed.
            [[ $# -ge 2 ]] || die "$1 needs a signing identity"
            IDENTITY="$2"; shift 2 ;;
        --no-hardened-runtime)
            HARDENED=0; shift ;;
        -h|--help)
            sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's|^# \{0,1\}||'; exit 0 ;;
        *)
            die "unknown argument: $1 (see --help)" ;;
    esac
done

# ------------------------------------------------------------ preflight

step "Preflight"
command -v swift    >/dev/null || die "swift not found on PATH"
command -v codesign >/dev/null || die "codesign not found on PATH"
command -v plutil   >/dev/null || die "plutil not found on PATH"

readonly SOURCE_PLIST="$SCRIPT_DIR/Info.plist"
[[ -f "$SOURCE_PLIST" ]] || die "missing $SOURCE_PLIST"

# Lint the template *before* spending a release build on it: a malformed plist
# produces a bundle that launches to a generic "damaged" dialog with no clue.
plutil -lint "$SOURCE_PLIST" >/dev/null || die "$SOURCE_PLIST is not a valid plist"

BUNDLE_ID="$(plutil -extract CFBundleIdentifier raw -o - "$SOURCE_PLIST")"
[[ -n "$BUNDLE_ID" ]] || die "Info.plist has no CFBundleIdentifier"

PLIST_EXEC="$(plutil -extract CFBundleExecutable raw -o - "$SOURCE_PLIST")"
[[ "$PLIST_EXEC" == "$APP_NAME" ]] \
    || die "CFBundleExecutable ($PLIST_EXEC) must match the binary this script installs ($APP_NAME)"

printf '    bundle id      : %s\n' "$BUNDLE_ID"
printf '    signing identity: %s%s\n' "$IDENTITY" \
    "$([[ $HARDENED -eq 1 ]] && echo '  (hardened runtime)' || echo '  (no hardened runtime)')"

# ------------------------------------------------------------ build

step "swift build -c release"
swift build -c release --package-path "$SCRIPT_DIR"

BIN_DIR="$(swift build -c release --package-path "$SCRIPT_DIR" --show-bin-path)"
readonly BINARY="$BIN_DIR/$PRODUCT"
[[ -x "$BINARY" ]] || die "release binary not found at $BINARY"
file "$BINARY" | grep -q 'Mach-O.*executable' \
    || die "$BINARY is not a Mach-O executable"

# ------------------------------------------------------------ assemble

# Staging lives beside the destination so the final move is a rename on the same
# filesystem rather than a copy that can fail halfway.
mkdir -p "$OUT_DIR" || die "cannot create $OUT_DIR"
STAGING="$(mktemp -d "$OUT_DIR/.make-app.XXXXXX")" || die "cannot create staging dir in $OUT_DIR"
trap 'rm -rf "$STAGING"' EXIT

readonly APP="$STAGING/$APP_NAME.app"
readonly CONTENTS="$APP/Contents"

step "Assembling $APP_NAME.app"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

# Copied under the bundle name, not the SwiftPM product name: CFBundleExecutable
# has to match, and it makes a bundled instance obvious in `ps` next to a debug
# `WorkloadClient` one.
cp "$BINARY" "$CONTENTS/MacOS/$APP_NAME"
chmod 755 "$CONTENTS/MacOS/$APP_NAME"

cp "$SOURCE_PLIST" "$CONTENTS/Info.plist"
plutil -lint "$CONTENTS/Info.plist" >/dev/null || die "copied Info.plist failed lint"

# Legacy, tiny, and still what a few Finder/LaunchServices paths look at first.
printf 'APPL????' > "$CONTENTS/PkgInfo"

# ------------------------------------------------------------------- icon
#
# `CFBundleIconFile` names the file *without* its extension, and the file must
# sit at the top level of Contents/Resources — not in a subdirectory.
#
# The rasterised `.icns` is what ships. The layered `AppIcon.icon` next to it is
# the Icon Composer source for the macOS 26 treatment Plan §11 wants; compiling
# it needs `actool`, which is currently broken on this machine (see the comment
# in Info.plist). Neither `AppIcon.icon` nor the master SVG is copied into the
# bundle — they are build inputs, not runtime resources.
#
# Missing icon is fatal rather than a warning: the plist names one, and a bundle
# whose CFBundleIconFile points at nothing renders the generic icon while
# looking, from the plist alone, like it should be fine.
readonly ICON_NAME="$(plutil -extract CFBundleIconFile raw -o - "$CONTENTS/Info.plist" 2>/dev/null || true)"
if [[ -n "$ICON_NAME" ]]; then
    readonly ICON_SRC="$SCRIPT_DIR/Resources/$ICON_NAME.icns"
    step "Adding the app icon"
    [[ -f "$ICON_SRC" ]] \
        || die "Info.plist sets CFBundleIconFile=$ICON_NAME but $ICON_SRC does not exist"
    cp "$ICON_SRC" "$CONTENTS/Resources/$ICON_NAME.icns"
    printf '    %s.icns (%s bytes)\n' "$ICON_NAME" "$(stat -f%z "$CONTENTS/Resources/$ICON_NAME.icns")"
fi

# ------------------------------------------------------------ localization
#
# Plan §11: "String catalog from day one even though only `en` ships."
#
# The catalog cannot be a SwiftPM resource. SwiftPM **copies** an `.xcstrings`
# verbatim (measured: the build log says "Copying Localizable.xcstrings") rather
# than compiling it, and it lands in `WorkloadClient_WorkloadClient.bundle` —
# whereas SwiftUI's `Text("…")` looks its key up in **`Bundle.main`**, which for
# a packaged app is `Contents/Resources`. So the catalog is compiled here, by
# the same tool Xcode uses, straight into the app bundle.
#
# `swift run` (unbundled) therefore has no catalog and falls back to the literal
# keys, which for `en` is byte-identical output. That is the point: the fallback
# is the source language, so a missing catalog can never change what is on
# screen — only an added *translation* could.
readonly CATALOG="$SCRIPT_DIR/Resources/Localizable.xcstrings"
readonly XCSTRINGSTOOL="$(xcrun --find xcstringstool 2>/dev/null || true)"
if [[ -f "$CATALOG" ]]; then
    if [[ -x "$XCSTRINGSTOOL" ]]; then
        step "Compiling the string catalog"
        "$XCSTRINGSTOOL" compile "$CATALOG" \
            --output-directory "$CONTENTS/Resources" \
            || die "xcstringstool failed on $CATALOG"
        # `en.lproj/Localizable.strings` is the only artefact this catalog can
        # produce; asserting it means a silently-empty compile fails here rather
        # than shipping a bundle whose strings table is missing.
        [[ -f "$CONTENTS/Resources/en.lproj/Localizable.strings" ]] \
            || die "the catalog compiled but produced no en.lproj/Localizable.strings"
        printf '    %s strings\n' \
            "$(plutil -convert json -o - "$CONTENTS/Resources/en.lproj/Localizable.strings" \
               | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo '?')"
    else
        printf '    warning: xcstringstool not found; shipping without a compiled catalog\n'
        printf '             (en falls back to the source strings, so nothing breaks)\n'
    fi
fi

# Assert the declaration that this whole phase exists to add, so a future edit
# that drops it fails here instead of silently re-breaking the board drag.
plutil -extract UTExportedTypeDeclarations.0.UTTypeIdentifier raw -o - "$CONTENTS/Info.plist" \
    >/dev/null || die "Info.plist declares no UTExportedTypeDeclarations — see Models/BoardDrop.swift"

# Same shape of assertion for the icon: the plist naming one and the bundle
# containing one are two different facts, and only the second is what Finder
# reads.
if [[ -n "$ICON_NAME" ]]; then
    [[ -f "$CONTENTS/Resources/$ICON_NAME.icns" ]] \
        || die "CFBundleIconFile=$ICON_NAME but Contents/Resources/$ICON_NAME.icns is missing"
fi

# ------------------------------------------------------------ sign

step "Signing"
sign_args=(--force --sign "$IDENTITY" --timestamp=none)
[[ $HARDENED -eq 1 ]] && sign_args+=(--options runtime)

if ! codesign "${sign_args[@]}" "$APP"; then
    if [[ $HARDENED -eq 1 ]]; then
        die "codesign failed with --options runtime. Re-run with --no-hardened-runtime to check whether the hardened runtime is the cause."
    fi
    die "codesign failed"
fi
codesign --verify --strict --verbose=2 "$APP" || die "signature failed verification"

# ------------------------------------------------------------ install

step "Installing to $OUT_DIR"
readonly FINAL="$OUT_DIR/$APP_NAME.app"
if [[ -e "$FINAL" ]]; then
    # Only now, with a verified bundle in hand.
    rm -rf "$FINAL" || die "cannot remove existing $FINAL"
fi
mv "$APP" "$FINAL" || die "cannot move bundle into $OUT_DIR"

# The step that makes UTExportedTypeDeclarations real: LaunchServices only knows
# about a declared type once the bundle has been registered. A fresh bundle in a
# path LaunchServices has never scanned is otherwise invisible until something
# else happens to trigger a scan.
if [[ -x "$LSREGISTER" ]]; then
    "$LSREGISTER" -f "$FINAL" || printf '    warning: lsregister -f failed; the custom UTType may not be registered\n'
else
    printf '    warning: lsregister not found at the expected path; skipping registration\n'
fi

# ------------------------------------------------------------ report

step "Done"
printf '    %s\n\n' "$FINAL"
codesign -dv "$FINAL" 2>&1 | sed 's/^/    /'
printf '\n    Launch:   open "%s"\n' "$FINAL"
printf '    Defaults: defaults read %s\n' "$BUNDLE_ID"
