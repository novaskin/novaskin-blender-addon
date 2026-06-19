# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**NovaSkin Export** — a single-file Blender add-on (`render_uv_mask.py`, "NovaSkin Export",
add-on id `novaskin`) that exports Minecraft-style player characters (the *Thomas Legacy Rig*)
plus their scene into web wallpaper assets. A browser renderer (in `prototype/`) loads those
assets and **relights swappable skins** in real time, so a user can drop in a new skin PNG and
see it lit by the original scene.

The add-on requires a **GUI Blender** (it reads the compositor Viewer node; it does NOT run in
`--background`). Target: Blender 4.2+ extensions; the file keeps `bl_info` so it also installs as
a classic add-on. Current version: **1.2.2**.

## Repo layout

The git repo is rooted at `thomas-legacy-rig/` (the parent `blender_novaskin/` is NOT a repo — it
holds scratch `.blend` files, test renders, and the `wallpaper-1` / `wallpaper-2` scene folders).

```
thomas-legacy-rig/
  render_uv_mask.py        THE deliverable — the whole add-on in one file
  blender_manifest.toml    extension manifest (version lives here too)
  build_addon.py           packages render_uv_mask.py -> dist/<id>-<version>.zip
  bl_info / ADDON_VERSION  in render_uv_mask.py
  docs/
    static-mesh-plan.md    design of the "static mesh v2" pipeline
    animated-export-plan.md
    output-format.md       the per-part UV/mask output format
  prototype/               browser renderers (WebGL2)
    static.html static.js  static mesh v2 viewer (the current focus)
    play.html play.js      per-part relighting viewer (legacy output)
    animated/              animated wallpaper viewer
    static -> ../../wallpaper-2/novaskin/static   (gitignored SYMLINK, see below)
  textures/  novaskin/  thomas-rig-legacy.blend
```

`origin` = https://github.com/novaskin/novaskin-blender-addon.git (branch `main`).

## Two export pipelines

`render_uv_mask.py` contains two related exporters:

1. **Per-part legacy export** (`bpy.ops.render.novaskin`) — one PNG/EXR per body part
   (`<part>_UV.png`: R=U G=V B=depth A=coverage) + masks + background + optional layers. The
   browser composites parts. See the module docstring and `docs/output-format.md`.

2. **Static mesh v2** (`bpy.ops.render.novaskin_static`, `_static_export_steps`) — the current
   focus. Welds the whole scene into one mesh and a per-player UV light atlas:
   - `mesh.bin` — NSKM v2, u32, welded vertices.
   - `positions.bin` — NSKA v3, K=1, int16 xyz; z = camera depth normalized to `[zmin,zmax]`.
   - per-player `<label>_atlas.webp` — gray-0.5 Lambertian light bake in UV space; the browser
     relights as `skin(uv) × atlas(uv) × 2`.
   - `background`/`foreground` WebP, per-entity `<name>_shadow.webp` multiply ratios.
   - optional **layers** (scenery props/mobs marked via the panel): a *mesh-type* layer (one
     shared texture, retexturable) exports like a player; a *sprite-type* layer exports as a
     tight silhouette beauty + a separate shadow ratio.
   - `manifest.json` ties it together.

The panel (3D Viewport sidebar → "NovaSkin" tab) has Export / Layers / Animation tabs and an
"Export Mesh (v2)" toggle.

### Key invariants of static v2 (don't regress these)

- **DENSE render** for the geometry/silhouette passes: AntiLag OFF + no simplify, so the rendered
  silhouettes match the exported mesh.
- **Atlas independence**: during each atlas bake the OTHER players and ALL layers are hidden
  (`_hide_others_for_bake`) so no other entity's shadow bakes into this atlas (a toggle could then
  never remove it). Scenery stays visible.
- **One sample count for the whole export** — the background, per-entity shadow, and sprite
  renders all use the panel "Samples"; differing counts cause a composite quality seam.
- **Sprites = tight silhouette + separate shadow**, NOT a beauty-overlay of the full scene.
  Folding the whole scene-with-mob into the sprite double-images the water against the background.
  The sprite is the mob alone (scenery as a holdout); its darkening is a separate multiply ratio
  composited before the players. Sprites order by `camera_depth` (painter order), no per-pixel depth.
- **Overlay parts** (hat / jacket / sleeves / pants 2nd layer) must ALWAYS export, even if the
  rig's "Second layer" toggle is OFF. `_static_export_steps` forces it ON via
  `_force_selection_props_on` (restored at the end), like the legacy/animated renders. The browser
  toggles each overlay part per player. Note: `leggings` may be pulled out as a separate optional
  *layer* (e.g. "Leg_Armor") rather than a player overlay — that's a scene choice.
- **Pixel-UV expansion**: the rig insets per-pixel UVs ~50%; `_expand_pixel_uvs` snaps each
  per-pixel quad to fill its skin-pixel cell (no gap-grid, HD skins work). Degenerate (h=0) faces
  are reconstructed from geometry + the mesh UV handedness sign so they don't flip on HD textures.

## Browser prototype

WebGL2, ES modules, top-level `await`. `static.js` loads `manifest.json` and renders. The HUD has
global toggles (background / players / light / shadows / foreground), a per-player toggle
(`ck_player_i`) + skin file input, per-layer toggles (`ck_layer_i`) + retex input for mesh layers,
per-overlay-part toggles per player (`ck_ov_i_<label>`), and per-player arm-variant radios
(`armv_i`, classic/slim).

Serve `prototype/` over http (an MCP **preview server** is used in this workflow:
`preview_start`, then `preview_eval`/`preview_screenshot`/`preview_console_logs`). `static.html`
cache-busts the import (`?v=Date.now()`), so a browser reload picks up `static.js` edits; the
manifest/assets are also `_cb`-busted.

### The `static` symlink

`prototype/static` is a **gitignored symlink** to `../../wallpaper-2/novaskin/static` (the live
export output). Edits to `static.js`/`static.html` are tracked; the exported binary assets under
the symlink target are not.

## Running an export (IMPORTANT — workflow + hazards)

The add-on runs only in a GUI Blender connected over the **Blender MCP** (`execute_blender_code`).

- **The user reloads the add-on themselves** (F3 → "Reload Scripts", or restart). The repo `.py`
  is symlinked into Blender's `addons/`, so a reload picks up edits live. **Do NOT re-register the
  operators via MCP** — it corrupts the live session.
- **Long renders time out the MCP call but Blender keeps running.** Pattern: have the Blender code
  write a marker/result file, then poll it from Bash with an until-loop (the `execute_blender_code`
  call itself will report a timeout — that's expected; ignore it and poll the file).
- **Do NOT drive the export repeatedly via `exec` on the live rig.** `_hide_others_for_bake` mutes
  `hide_render` drivers during the atlas bake; repeated/interrupted/queued exec runs leave drivers
  muted and player meshes stuck hidden, so a later collect yields **0 parts** (empty geometry).
  Recovery: un-mute every `hide_render` driver and `view_layer.update()`. The saved `.blend` is
  fine — only runtime state corrupts. **Prefer letting the user run a single clean export via the
  panel button** after reloading.
- A clean **single** run is reliable; the corruption is an accumulation artifact of programmatic
  re-runs, not a code bug.

To inspect the export without a full render, you can `exec` the file in a throwaway namespace
(`__name__` ≠ `"__main__"` so `register()` doesn't fire) and call `discover_players()` +
`_static_write_geometry(...)` (fast, no render) to check collected parts.

## Tests

`tests/` holds unit tests for the **pure** (no live-Blender) helpers — the binary-format writers
(`_anim_write_anim` round-trip), colour math (`_lin_to_srgb`), label mapping (`_mc_part_label` /
`_assign_part_labels`, incl. the duplicate-mesh collision case), filename sanitation, and the light
dilation. They use `unittest` (stdlib, no pytest) and import `render_uv_mask.py` via
`tests/_loader.py`, which installs a **minimal fake `bpy`** so the module imports outside Blender
(the bpy-touching functions just aren't called).

They need **numpy**, which the system `python3` usually lacks — run with Blender's bundled Python:

```
tests/run_tests.sh            # finds Blender's python (or a numpy-capable python3) and runs them
BLENDER_PYTHON=/path tests/run_tests.sh   # override the interpreter
```

Anything that genuinely needs `bpy` (geometry collection, rendering, the atlas bake) is NOT unit
tested — validate those by running an export and checking the manifest / browser, per the workflow
above. When adding a pure helper, prefer testing it here.

## Conventions

- **Code, comments, docs, and commit messages: English. Chat with the user: Portuguese.**
- **Git**: push/tag ONLY when explicitly asked. Never delete GitHub repos. Commit messages end with:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- **Release tag** ("create release tag"): bump the version in THREE places before tagging —
  `blender_manifest.toml` `version`, `bl_info["version"]` (keep it a literal — Blender parses it
  via `ast`), and `ADDON_VERSION`. `v1.0.0` stays.
- Keep comments in the surrounding density/idiom. The file is large and dense; match it.
- `python3 -m py_compile render_uv_mask.py` is the cheap sanity check after edits.

## Map of key functions (`render_uv_mask.py`)

- `discover_players()` / `_select_uv_parts()` — find player armatures (Rig_ID) and their part meshes
  by visibility (robust to Blender's non-uniform `.NNN` dup suffixes).
- `discover_layers()` / `_static_split_layers()` — optional scenery layers; mesh-type vs sprite-type.
- `_static_export_steps()` — the static-v2 generator (atlas bakes → geometry → images → sprites →
  manifest). `_static_export()` drains it synchronously.
- `_static_collect()` / `_static_write_geometry()` — weld geometry, capture per-part positions,
  both arm variants (`_set_arm_style`), `parts` meta (label / tri_range / overlay / variant).
- `_static_render_images()` — background, foreground, screen light, per-entity shadow ratios.
- `_static_render_layers()` — tight sprite beauty per sprite-type layer.
- `_bake_player_light_atlas()` / `_hide_others_for_bake()` / `_force_bake_visible()` — atlas baking.
- `_expand_pixel_uvs()` / `_mesh_uv_handedness()` — pixel-UV cell expansion + degenerate-face fix.
- `_force_selection_props_on()` / `_restore_selection_props()` — force "Second layer" ON for export.
- `_fix_2layer_positions()` — snap/scale the head's outer "hat" layer onto the head (idempotent).
