#!/usr/bin/env bash
# Run the render_uv_mask.py unit tests with a Python that has numpy.
#
# The add-on's pure helpers need numpy; the system python3 usually doesn't have it, so we prefer
# Blender's bundled Python (which matches the runtime). Override with:  BLENDER_PYTHON=/path ./run_tests.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_blender_python() {
  if [[ -n "${BLENDER_PYTHON:-}" && -x "${BLENDER_PYTHON}" ]]; then
    echo "${BLENDER_PYTHON}"; return 0
  fi
  # numpy in the system python? use it.
  if python3 -c "import numpy" >/dev/null 2>&1; then
    command -v python3; return 0
  fi
  # search the usual Blender install spots (macOS) for a bundled python with numpy
  local p
  while IFS= read -r p; do
    if "$p" -c "import numpy" >/dev/null 2>&1; then echo "$p"; return 0; fi
  done < <(
    find \
      "$HOME/Library/Application Support/Steam/steamapps/common/Blender" \
      "/Applications" "$HOME/Applications" "$HOME/Desktop" \
      -maxdepth 8 -path "*Blender*/python/bin/python3.*" -type f 2>/dev/null
  )
  return 1
}

PY="$(find_blender_python || true)"
if [[ -z "${PY:-}" ]]; then
  echo "No Python with numpy found. Set BLENDER_PYTHON=/path/to/blender/python3.x" >&2
  exit 1
fi

echo "Using: $PY"
"$PY" -m unittest discover -s "$HERE" -p "test_*.py" -v
