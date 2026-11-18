#!/bin/sh
# Double-click to install Forge on a Mac (first time: right-click → Open, since it's a downloaded script).
curl -fsSL https://forgecli.pages.dev/install.sh | sh
BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
if [ -x "$BIN/forge" ]; then
  cd "$HOME"
  exec "$BIN/forge"
fi
printf 'Press Enter to close '
read _
