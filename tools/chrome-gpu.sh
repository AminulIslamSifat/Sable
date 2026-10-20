#!/usr/bin/env bash
# Wrapper: force Chrome's WebGL onto the real GL driver even in headless mode.
# Playwright injects --headless itself; everything here just adds GPU flags.
exec /usr/bin/google-chrome-stable \
  --use-angle=gl \
  --enable-webgl \
  --ignore-gpu-blocklist \
  --enable-gpu-rasterization \
  --enable-zero-copy \
  --disable-software-rasterizer \
  "$@"
