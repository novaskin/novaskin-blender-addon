# NovaSkin Export — `render_uv_mask.py`

A Blender add-on/script that exports, per player, the data layers of a
**Thomas_Rig_Legacy** rig for downstream compositing:

- **UV** per part (R=U, G=V, B=normalized depth), front and back, 16-bit raw.
- **Character mask** (Object Index), occluded by scenery **and** by other players,
  in both arm variants (`classic` = Steve arms, `slim` = Alex arms).
- **Illum + shadow** per player/variant (resolves occlusion between players).
- **Background without players** + `manifest.json` with the rigs' depth order.

Output: `<.blend dir>/novaskin/` (one subfolder per player + scene-level files).

---

## Requirements

- **Blender with a GUI** (5.1 recommended). Does **not** run in `--background` — the
  script reads the result through the *Viewer node*.
- A scene with the **Thomas_Rig_Legacy** rig (1+ players, identified by the `Rig_ID`
  custom property), an **active camera**, and lights/scenery.
- The **`.blend` must be saved** (output uses `//novaskin/`, relative to the file).

If anything is missing, the script aborts at startup with a clear message (preflight)
instead of failing midway.

---

## How to use

### Option A — Install as an add-on ✅ *(recommended)*

Two ways:

- **Extension zip (Blender 4.2+):** build it with `python3 build_addon.py` (creates
  `dist/novaskin_export-<version>.zip`), then **drag the `.zip` onto the Blender window**, or
  `Edit > Preferences > Get Extensions > ⌄ > Install from Disk…`.
- **Classic add-on:** `Edit > Preferences > Add-ons > Install…` → select
  `render_uv_mask.py` (the single file keeps its `bl_info`).

Then enable **“NovaSkin Export (Thomas_Rig_Legacy)”** and run it from either:
   - **3D Viewport sidebar** (press <kbd>N</kbd>) → **“NovaSkin”** tab — options + a
     **Render for NovaSkin** button, or
   - **top bar → Render → “Render for NovaSkin”**.

The panel exposes the common options (output folder, UV format, layers, samples, etc.);
its values override the `CONFIG` constants in the script at run time.

### Option B — Run from the *Scripting* workspace *(quick test / development)*

1. **Scripting** tab → **Open** → open `render_uv_mask.py` → **Run Script** (▶)
2. This **registers** the operator (does not render) and adds the menu entry
3. Launch it via **Render → “Render for NovaSkin”**

### Option C — Console / another script

```python
bpy.ops.render.novaskin()
```

(Runs **synchronously** — blocks until done. Useful for automation.)

---

## Updating the script

**Important:** *Install from Disk* **copies** the file into Blender's add-ons folder;
it is **not linked** to the original file. Editing the original does **not** update the
installed add-on.

Ways to update:

1. **Reinstall** — *Install from Disk* again pointing at the new file (overwrites the
   copy). Simple, but manual on every change.
2. **Link via a Script Directory** *(best if you edit often)* —
   `Edit > Preferences > File Paths > Script Directories` → **Add** pointing at a folder
   that contains an `addons/` subfolder with `render_uv_mask.py` (e.g. this repo with
   `addons/render_uv_mask.py`). Blender loads it straight from disk; after editing, use
   **F3 → “Reload Scripts”** (or restart) to reload.
3. **Scripting workspace (Option B)** — during development, this always runs the current
   version of the open file; no reinstall needed.

> Summary: for stable use, **Option A** (reinstall when it changes). For heavy iteration,
> **Script Directory** or **Scripting** avoid reinstalling on every edit.

---

## Progress and cancellation

When launched from the panel/menu (modal), the progress shows in three places: a **progress
bar in the NovaSkin panel** (replacing the run button while it works), the **3D Viewport
header**, and the **status bar** (`NovaSkin 42% - UV front: Thomas_rig / head`). Cancel with
the **Cancel button** in the panel, or **Esc** (mouse in the viewport) — either way the
scene is restored to its original state (it takes effect at the next step boundary).

> The UI is responsive **between** steps, but each individual render still blocks briefly
> while it runs. `bpy` is single-threaded and not thread-safe, so there is no way to render
> without blocking the main thread; the modal approach splits the work into many short steps
> with feedback and allows cancelling.

The scene is always restored at the end (compositor, visibility, samples, drivers,
materials), even on cancel or error.

---

## Output layout

```
<.blend dir>/novaskin/
├── <armature>/                      # one per player (e.g. Thomas_rig, Thomas_rig.001, ...)
│   ├── <part>_UV.png                # R=U, G=V, B=depth, A=coverage(0 outside); MC label; 8-bit
│   │                                #   (EXR "<part>_UVDL.exr" packs the light in a layer)
│   ├── <part>_light.jpg             # per-part light (base parts: base render; overlays: full)
│   ├── <part>_UV_back.png           # back faces (R=U, G=V, B=depth)
│   ├── base_layer_classic.png       # base parts composited per variant (nearest wins)
│   ├── base_layer_slim.png
│   ├── ...
│   ├── mask_classic.png    # 8-bit PNG
│   ├── mask_slim.png
│   ├── illum_classic.jpg / illum_slim.jpg     # JPEG (light)
│   └── shadow_classic.jpg / shadow_slim.jpg   # JPEG (multiply)
├── illum_classic.jpg / illum_slim.jpg  # (only if EXPORT_ILLUM_BACKGROUND)
├── background_no_players.png
└── manifest.json                    # resolution, engine, draw_order (back→front), etc.
```

---

## Configuration

Parameters live in the **CONFIG** block at the top of `render_uv_mask.py`. Key ones:

| Key | Purpose |
|---|---|
| `OUT_DIR` | Output folder (default `//novaskin/`) |
| `PLAYER_FOLDER_SCHEME` | Per-player subfolder name: `index` (`player1`, `player2`, …) or `armature` (object name). The manifest records both, a player `visible_bbox`, and `part_bboxes` (per-part bbox) — all top-left px + normalized |
| `MASK_ARM_VARIANTS` | Arm variants exported (`classic`/`slim`) |
| `UV_DEPTH_IN_BLUE` | Write normalized depth into the UV's B channel |
| `EXPORT_BACKFACE_UV` | Also export the back-face UVs |
| `EXPORT_PLAYER_ILLUM_SHADOW` | Illum + shadow per player |
| `EXPORT_BACKGROUND_NO_PLAYERS` | Render the scenery without players |
| `FIX_2LAYER_POSITION` / `HAT_SCALE_RATIO` | Snap the hat (`2_Layer_Extrusion`) onto `NoFace_Head` and scale it to the Minecraft hat size (`1.125`× the head) before export (persistent) |
| `ILLUM_SAMPLES` / `ILLUM_COLORSPACE` | Illum quality/encoding |
| `PNG_BIT_DEPTH` | Bit depth of UV/mask PNGs (default `8`; 256 levels, enough for MC skins) |
| `UV_FORMAT` / `EXR_HALF` / `EXR_CODEC` | UV + base_layer format: `PNG` (default) or `OPEN_EXR` (half-float, `ZIP` ≈ PNG size, lossless). As EXR + illum, the RGB light is embedded as an extra `light.*` layer — see note |
| `COMPOSITE_BASE_LAYER` / `COMPOSITE_BASE_LABELS` | Composite base parts into `base_layer.png` (nearest pixel wins) |
| `LIGHTSHADOW_FORMAT` / `JPEG_QUALITY` | Illum + shadow file format (default `JPEG`, quality `90`) |
| `RIG_ID_PROP` / `RIG_ID_VALUE` | How players are detected |
| `RENAME_UV_PARTS` / `MC_PART_MAP` | Rename UV files to Minecraft labels (head/hat, body/jacket, arm/sleeve, leg/pant, `_l`/`_r`, `_classic`/`_slim`) |

UV part filenames use Minecraft-style labels: `head`, `hat`, `body`, `jacket`,
`arm_{left,right}_{classic,slim}`, `sleeve_{left,right}_{classic,slim}`,
`leg_{left,right}`, `pant_{left,right}`. Front files are `<part>_UVDL.png`
(R=U, G=V, B=depth, A=light); `base_layer_{classic,slim}.png` composite the base parts.
Unmapped meshes fall back to a sanitized object name, and duplicate labels get a `_2`/`_3`
suffix. The mapping (label → object name) is recorded in `manifest.json` per player. To
disable and keep raw object names, set `RENAME_UV_PARTS = False`.

### Reading the UV alpha (light): mind premultiplied alpha

**PNG** files are `<part>_UV.png` with RGBA = (U, V, depth, coverage) — alpha is `0`/`1`
(coverage), so they read fine anywhere, including an HTML 2D canvas. The light is the
separate `illum_*.jpg`.

**EXR** files (`UV_FORMAT='OPEN_EXR'`) keep the same RGBA = (U, V, depth, coverage) and,
when the illum pass is on, embed the **RGB light** as an extra `light.*` channel layer
(`light.R/G/B`, sRGB) → `<part>_UVDL.exr`. Read with a float EXR loader (e.g. Three.js
`EXRLoader`) — the default RGBA gives U/V/depth/coverage exactly like the PNG; read the
`light` layer for the color light. With `EXR_HALF=True` + `EXR_CODEC='ZIP'` it is lossless
and ≈ PNG file size (~110 KB at 1080p for the 7 channels). Browsers can't decode EXR via a
2D canvas, so this also sidesteps the canvas premultiply gotcha entirely.

> Light is never packed into a PNG alpha: a variable alpha gets corrupted by the 2D-canvas
> premultiply round-trip (it un-premultiplies on read, mangling U/V where the light is dark).

> This script is specific to the **Thomas_Rig_Legacy** rig. Other rigs would require
> adjusting `RIG_ID_VALUE`, `SLIM_CONTROL`, `SELECTION_FORCE_OFF`, `MESH_COLLECTION_PREFIX`,
> and `MASK_ARM_VARIANTS`.
