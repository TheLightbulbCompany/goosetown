#!/bin/bash
source "$(dirname "$0")/../env.sh"

ACTION="${1:?Usage: town_act <action> [args...]}"
shift

# Build JSON payload safely using Python to handle special characters
case "$ACTION" in
    move)
        PAYLOAD=$(python3 -c "import json; print(json.dumps({'action':'move','destination':'$1'}))")
        ;;
    chat)
        TARGET="$1"; shift
        MESSAGE="$*"
        PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'action':'chat','target':sys.argv[1],'message':sys.argv[2]}))" "$TARGET" "$MESSAGE")
        ;;
    say)
        CONV_ID="$1"; shift
        MESSAGE="$*"
        PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'action':'say','conv_id':sys.argv[1],'message':sys.argv[2]}))" "$CONV_ID" "$MESSAGE")
        ;;
    idle)
        ACTIVITY="${1:-idle}"
        PAYLOAD=$(python3 -c "import json; print(json.dumps({'action':'idle','activity':'$ACTIVITY'}))")
        ;;
    end)
        CONV_ID="$1"
        PAYLOAD=$(python3 -c "import json; print(json.dumps({'action':'end_conversation','conv_id':'$CONV_ID'}))")
        ;;
    *)
        echo "{\"error\":\"Unknown action: $ACTION. Use: move, chat, say, idle, end\"}"
        exit 1
        ;;
esac

# Send to daemon via Unix socket
if [ ! -S "$STATE_DIR/daemon.sock" ]; then
    echo '{"error": "Not connected to GooseTown. Run town_connect first."}'
    exit 1
fi

echo "{\"action\":\"act\",\"payload\":$PAYLOAD}" | socat - UNIX-CONNECT:"$STATE_DIR/daemon.sock" 2>/dev/null
if [ $? -ne 0 ]; then
    echo '{"error": "Failed to communicate with daemon. Try town_connect to reconnect."}'
    exit 1
fi
