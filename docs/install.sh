#!/bin/sh
# Install Forge on macOS or Linux:
#   curl -fsSL https://forgecli.pages.dev/install.sh | sh
# Uses uv (https://docs.astral.sh/uv/) to get a private Python and install `forgecommandline`
# into its own environment. Nothing needs sudo. Run it again any time to update.
set -eu

say() { printf '  %s\n' "$*"; }
printf '\n\033[1;35m  ◆ Installing Forge\033[0m\n\n'

if ! command -v uv >/dev/null 2>&1; then
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$d/uv" ] && PATH="$d:$PATH" && export PATH
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  say "Getting uv (a fast Python installer) …"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  else
    wget -qO- https://astral.sh/uv/install.sh | sh >/dev/null
  fi
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH
fi

say "Installing Forge …"
uv tool install --upgrade --python 3.12 forgecommandline >/dev/null 2>&1 \
  || uv tool install --upgrade --python 3.12 forgecommandline
uv tool update-shell >/dev/null 2>&1 || true

BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
printf '\n\033[1;32m  ✓ Forge is installed\033[0m  (%s)\n\n' "$("$BIN/forge" --version 2>/dev/null || echo forge)"
if command -v forge >/dev/null 2>&1; then
  say "Type  forge  to start."
else
  say "Open a new terminal and type  forge  to start."
fi
printf '\n'
