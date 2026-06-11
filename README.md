# NovaSkin Export — `render_uv_mask.py`

A Blender add-on/script that exports, per player, the data layers of a
**Thomas_Rig_Legacy** rig for downstream compositing:

- **UV** per part (R=U, G=V, B=normalized depth, A=coverage), front and back, raw 8-bit
  PNG (or float EXR), named with Minecraft labels (`head`, `hat`, `body`, `jacket`, …).
- **Character mask** (Object Index), occluded by scenery **and** by other players,
  in both arm variants (`classic` = Steve arms, `slim` = Alex arms).
- **Per-layer light** (base parts lit from a base-only render, overlays from the full one)
  + **cast shadow** per player/variant, and `base_layer_*` composites.
- **Background** (scene without players/layers), **optional layers** (marked scenery
  objects exported independently), and a `manifest.json` with bboxes + draw order.

Output: `<.blend dir>/novaskin/` (one subfolder per player — `player1`, `player2`, … —
plus `layers/` and scene-level files).

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

<p align="center">
  <img src="blender-panel.png" alt="NovaSkin Export panel in the 3D Viewport sidebar"
       width="300">
</p>

*The NovaSkin tab in the sidebar: render/launch buttons, output + UV format, the layer
toggles, render quality, **Optional Layers** (the “Mark Selected as Layer” button and the
list of marked objects), and the rig fix. The output field turns red until the `.blend`
is saved.*

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
2. **Symlink into the add-ons folder** *(best if you edit often)* — link the repo file
   into Blender's user add-ons dir, e.g. on macOS:
   ```
   ln -s /path/to/repo/render_uv_mask.py \
     "~/Library/Application Support/Blender/<ver>/scripts/addons/render_uv_mask.py"
   ```
   Blender loads it straight from the repo; after editing, **F3 → “Reload Scripts”**
   (or restart) reloads it.
3. **Scripting workspace (Option B)** — during development, this always runs the current
   version of the open file; no reinstall needed.

> Summary: for stable use, **Option A** (reinstall when it changes). For heavy iteration,
> the **symlink** or **Scripting** avoid reinstalling on every edit.

---

## Progress and cancellation

When launched from the panel/menu (modal), the progress shows in three places: a **progress
bar in the NovaSkin panel** (replacing the run button while it works), the **3D Viewport
header**, and the **status bar** (`NovaSkin 42% - UV front: player1 / head`). Cancel with
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
├── player1/                         # one per player (sorted by armature name)
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
├── background.png                   # scene without players and optional layers
├── layers/                          # optional layers (objects marked in the panel)
│   ├── <name>.png                   # the object alone, transparent bg, display-encoded
│   ├── <name>_shadow.jpg            # shadow it casts on the scenery (display multiply)
│   ├── <name>_UV.png                # generic UV (retexture); EXR mode: <name>_UVDL.exr
│   └── <name>_light.jpg             # its light map (PNG pipeline)
└── manifest.json                    # manifest_version, addon_version, resolution,
                                     #   draw_order (back→front), bboxes, depth ranges, etc.
```

### Optional layers

Select any scenery mesh and click **“Mark Selected as Layer”** in the panel (it toggles —
click again to unmark; marked objects are listed in the panel; player-rig meshes are
refused). Each marked object is **hidden from the background and from the whole player
pipeline** and exported as an independent toggleable layer in `layers/`: beauty
(transparent background, lit by the real scene), the shadow it casts on the scenery, and
its UV + light for generic retexturing (`EXPORT_LAYER_UV`; the light follows the Illum
toggle). The manifest lists each layer (files, bbox, `camera_depth`,
`depth_range_viewer`) and includes them in `draw_order_back_to_front`.

**Occlusion:** unlike players (always rendered unoccluded, in front), a layer **is
occluded by the scenery** — the scenery renders as a *holdout* (cuts the alpha where it
is in front while still lighting/shadowing the object) and the UV coverage is clipped
the same way. Players and the **other layers** are excluded from the render instead
(they can be toggled off in the wallpaper, so the layer must be whole with respect to
them). Trade-off: a layer's shadow never falls on a player (the player light is
independent of layer toggles).

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
| `EXPORT_ILLUM` / `EXPORT_SHADOW` | Per-player light (per-part + illum image) and cast shadow — independent toggles |
| `EXPORT_BACKGROUND` | Render the scenery without players/layers (`background.png`) |
| `LAYER_ID_PROP` / `EXPORT_LAYER_UV` | Optional layers: marker property name; `EXPORT_LAYER_UV` (default **on**) also exports each layer's UV + light |
| `EXPORT_PART_LIGHT` | Save the per-part light images (`<part>_light.jpg`, PNG pipeline; default on) |
| `SHADOW_DISPLAY_RATIO` | Shadow ratio in display space (view-transformed) so multiplying it onto the background matches the render (default on) |
| `ILLUM_HIDE_SCENERY_FROM_CAMERA` | Light renders: scenery camera-invisible so foreground objects don't bleed into the player light (default on) |
| `BACKGROUND_USE_SCENE_SETTINGS` | Background renders with the user's engine/samples/denoise (default on) |
| `PNG_COMPRESSION` | zlib level for data PNGs (default `90`; byte-exact) |
| `UV_PNG_FLOOR_TEXELS` / `UV_TEXEL_BINS` | PNG U/V quantized with `floor` into texel bins (byte = texel index @256) |
| `FIX_2LAYER_POSITION` / `HAT_SCALE_RATIO` | Snap the hat (`2_Layer_Extrusion`) onto `NoFace_Head` and scale it to the Minecraft hat size (`1.125`× the head) before export (persistent) |
| `ILLUM_SAMPLES` / `ILLUM_COLORSPACE` | Illum quality/encoding |
| `PNG_BIT_DEPTH` | Bit depth of UV/mask PNGs (default `8`; 256 levels, enough for MC skins) |
| `UV_FORMAT` / `EXR_HALF` / `EXR_CODEC` | UV + base_layer format: `PNG` (default), `WEBP` (8-bit **lossless** — quality 100; ~60% smaller than PNG, browser-decodable) or `OPEN_EXR` (half-float, `ZIP` ≈ PNG size, lossless). As EXR + illum, the RGB light is embedded as an extra `light.*` layer — see note |
| `COMPOSITE_BASE_LAYER` / `COMPOSITE_BASE_LABELS` | Composite base parts into `base_layer.png` (nearest pixel wins) |
| `LIGHTSHADOW_FORMAT` / `JPEG_QUALITY` | Illum + shadow file format: `JPEG` (default), `WEBP` (~30% smaller, browser-friendly) or `PNG`; quality `90` |
| `DRAFT_MODE` (panel: *Draft*) | Everything at 50% resolution with few samples — fast preview, recorded in the manifest |
| `RIG_ID_PROP` / `RIG_ID_VALUE` | How players are detected |
| `RENAME_UV_PARTS` / `MC_PART_MAP` | Rename UV files to Minecraft labels (head/hat, body/jacket, arm/sleeve, leg/pant, `_left`/`_right`, `_classic`/`_slim`) |

UV part filenames use Minecraft-style labels: `head`, `hat`, `body`, `jacket`,
`arm_{left,right}_{classic,slim}`, `sleeve_{left,right}_{classic,slim}`,
`leg_{left,right}`, `pant_{left,right}`. Front files are `<part>_UV.png`
(R=U, G=V, B=depth, A=coverage; `<part>_UVDL.exr` with a `light` layer in EXR mode);
`base_layer_{classic,slim}.png` composite the base parts (nearest depth wins).
Unmapped meshes fall back to a sanitized object name, and duplicate labels get a `_2`/`_3`
suffix. The mapping (label → object name) is recorded in `manifest.json` per player. To
disable and keep raw object names, set `RENAME_UV_PARTS = False`.

### Applying the light and shadow

- **Light** (`<part>_light.jpg` / EXR `light` layer): the scene lighting captured on a
  **0.5 gray** Lambertian body (sRGB). Relight a part with
  `lit = skin × (light / 0.5)` (in linear; ≈ multiply + 2× exposure in a 2D canvas). Base
  parts are lit from a base-only render, overlays from the full render — each layer is lit
  where it is front-most. Self-shadowing is already in the light; `shadow_*` is only the
  shadow cast on the scenery.
- **Shadow** (`shadow_*.jpg`, players and layers): a **display-space multiply** map
  (white = no shadow). Combine multiple shadows with *darken* (per-pixel min), then
  *multiply* the result onto `background.png` — this reproduces the Blender render.

### Reading the UV alpha (light): mind premultiplied alpha

**PNG** files are `<part>_UV.png` with RGBA = (U, V, depth, coverage) — alpha is `0`/`1`
(coverage), so they read fine anywhere, including an HTML 2D canvas. The light is the
separate `<part>_light.jpg` (and the full-body `illum_*.jpg`).

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
