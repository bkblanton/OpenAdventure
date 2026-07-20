#!/bin/sh

cd "$(dirname "$0")" || exit 1

# Finder-launched Terminal windows may not inherit the PATH configured by the
# uv installer or Homebrew.
PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

if ! command -v uv >/dev/null 2>&1; then
    echo "OpenAdventure needs uv, but it was not found on your PATH."
    echo "Install it from https://docs.astral.sh/uv/getting-started/installation/"
    echo "Then close this window and run launch-web.command again."
    echo
    printf "Press Return to close..."
    read -r _openadventure_reply
    exit 1
fi

echo "Starting OpenAdventure..."
uv run openadventure web "$@"
openadventure_exit_code=$?

if [ "$openadventure_exit_code" -ne 0 ]; then
    echo
    echo "OpenAdventure stopped with exit code $openadventure_exit_code."
    printf "Press Return to close..."
    read -r _openadventure_reply
fi

exit "$openadventure_exit_code"
