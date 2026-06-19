#!/usr/bin/env bash
# Run the in-Blender geometry/UV tests headless, in a SEPARATE Blender process (does NOT touch any
# running GUI session). Override the binary with:  BLENDER=/path/to/Blender ./run_blender_tests.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/blender/test_geometry_blender.py"

find_blender() {
  if [[ -n "${BLENDER:-}" && -x "${BLENDER}" ]]; then echo "${BLENDER}"; return 0; fi
  if command -v blender >/dev/null 2>&1; then command -v blender; return 0; fi
  local b
  b="$(find \
        "$HOME/Library/Application Support/Steam/steamapps/common/Blender" \
        "/Applications" "$HOME/Applications" "$HOME/Desktop" \
        -maxdepth 4 -path "*Blender.app/Contents/MacOS/Blender" -type f 2>/dev/null | head -1)"
  [[ -n "$b" ]] && { echo "$b"; return 0; }
  return 1
}

BL="$(find_blender || true)"
if [[ -z "${BL:-}" ]]; then
  echo "Blender binary not found. Set BLENDER=/path/to/Blender" >&2
  exit 1
fi

STATUS="$(mktemp)"
trap 'rm -f "$STATUS"' EXIT
echo "Using: $BL"
# --factory-startup: ignore the user's addons/prefs (fast, isolated). The script writes PASS/FAIL
# to NSK_TEST_STATUS because Blender swallows the script's exit code in --background.
NSK_TEST_STATUS="$STATUS" "$BL" --background --factory-startup \
  --python "$SCRIPT" --python-exit-code 1 2>&1 | grep -E '^(ok|FAIL) |failure\(s\)' || true

result="$(cat "$STATUS" 2>/dev/null || true)"
echo "----"
if [[ "$result" == PASS ]]; then
  echo "Blender geometry tests: PASS"; exit 0
else
  echo "Blender geometry tests: ${result:-NO RESULT (Blender failed to run the script)}"; exit 1
fi
