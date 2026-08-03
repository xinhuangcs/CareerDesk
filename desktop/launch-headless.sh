#!/bin/zsh
# Headless macOS launcher invoked by the app bundle.
cd "${0:A:h}/.." || exit 1
# GUI sessions often omit common uv installation directories from PATH.
command -v uv >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec uv run --project backend python run.py
