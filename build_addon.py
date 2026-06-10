#!/usr/bin/env python3
"""Package render_uv_mask.py into a Blender extension zip (blender_manifest.toml + code).

The zip can be installed via  Edit > Preferences > Get Extensions > Install from Disk,
or by dragging the .zip onto the Blender window (Blender 4.2+).

Usage:  python3 build_addon.py
Output: dist/<id>-<version>.zip   (id/version read from blender_manifest.toml)

The single render_uv_mask.py keeps its bl_info so it still works as a classic add-on
(Install from Disk of the raw .py). For the extension zip the manifest replaces bl_info,
so this script strips bl_info when copying the code to __init__.py.
"""
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "render_uv_mask.py")
MANIFEST = os.path.join(HERE, "blender_manifest.toml")
DIST = os.path.join(HERE, "dist")


def _field(text, key):
    m = re.search(r'^%s\s*=\s*"([^"]+)"' % re.escape(key), text, re.M)
    return m.group(1) if m else None


def main():
    if not os.path.exists(MANIFEST):
        sys.exit("blender_manifest.toml not found")
    if not os.path.exists(SRC):
        sys.exit("render_uv_mask.py not found")
    manifest = open(MANIFEST, encoding="utf-8").read()
    code = open(SRC, encoding="utf-8").read()

    ext_id = _field(manifest, "id") or "novaskin_export"
    version = _field(manifest, "version") or "0.0.0"

    # bl_info is ignored for extensions (the manifest replaces it); strip it for a clean zip.
    code = re.sub(r"\nbl_info\s*=\s*\{.*?\n\}\n", "\n", code, count=1, flags=re.S)

    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "%s-%s.zip" % (ext_id, version))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("blender_manifest.toml", manifest)
        z.writestr("__init__.py", code)

    print("Built %s (%d bytes)" % (out, os.path.getsize(out)))
    print("Install: Blender > Edit > Preferences > Get Extensions > (v) Install from Disk")
    print("    or:  drag the .zip onto the Blender window")


if __name__ == "__main__":
    main()
