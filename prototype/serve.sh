#!/bin/sh
# Serve the prototype viewers (play.html / static.html / index.html) from this directory.
#   ./prototype/serve.sh [port]     (default port: 8088)
# play.html auto-loads ./animated/ when present (symlink an export there), or use its
# folder picker to load any export containing a manifest.json.
PORT="${1:-8088}"
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Serving $DIR"
echo "  animated player:  http://localhost:$PORT/play.html"
echo "  static viewer:    http://localhost:$PORT/static.html"
exec python3 -m http.server "$PORT" --directory "$DIR"
