#!/bin/bash
set -e
cd "$(dirname "$0")"

OUT_DIR="../bin"
mkdir -p "$OUT_DIR"

echo "→ Building Linux amd64..."
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags="-s -w" -o "$OUT_DIR/sable-beacon-linux-amd64" .

echo "→ Building Windows amd64..."
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -ldflags="-s -w" -o "$OUT_DIR/sable-beacon-windows-amd64.exe" .

echo "→ Building Linux arm64..."
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags="-s -w" -o "$OUT_DIR/sable-beacon-linux-arm64" .

echo "✓ Done. Binaries in $OUT_DIR/"
ls -lh "$OUT_DIR"/sable-beacon-*
