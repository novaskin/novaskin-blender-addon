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

1. `Edit > Preferences > Add-ons`
2. **Install...** (*Install from Disk*) → select `render_uv_mask.py`
3. Tick the checkbox to enable **“NovaSkin Export (Thomas_Rig_Legacy)”**
4. Run it from either:
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

When launched from the menu (modal), a **progress bar** and the current step appear in the
status bar (`NovaSkin 42% - UV front: Thomas_rig / L.Leg`). Press **Esc** to cancel — the
scene is restored to its original state.

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
│   ├── <part>_UV.png                # R=U, G=V, B=depth, A=1; <part> = MC label; 8-bit
│   │                                #   (A=light, "<part>_UVDL.png", if UV_ALPHA_LIGHT=True)
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
| `MASK_ARM_VARIANTS` | Arm variants exported (`classic`/`slim`) |
| `UV_DEPTH_IN_BLUE` | Write normalized depth into the UV's B channel |
| `EXPORT_BACKFACE_UV` | Also export the back-face UVs |
| `EXPORT_PLAYER_ILLUM_SHADOW` | Illum + shadow per player |
| `EXPORT_BACKGROUND_NO_PLAYERS` | Render the scenery without players |
| `FIX_2LAYER_POSITION` | Snap the head's outer layer (`2_Layer_Extrusion`), parked above the head by default, back onto `NoFace_Head` before export (position only; persistent) |
| `ILLUM_SAMPLES` / `ILLUM_COLORSPACE` | Illum quality/encoding |
| `PNG_BIT_DEPTH` | Bit depth of UV/mask PNGs (default `8`; 256 levels, enough for MC skins) |
| `UV_ALPHA_LIGHT` | Pack illum lightness into the UV's alpha (`_UVDL.png`). **Default `False`** — see note below |
| `UV_FORMAT` / `EXR_HALF` / `EXR_CODEC` | UV + base_layer format: `PNG` (default) or `OPEN_EXR` (half-float, `ZIP` ≈ PNG size, lossless; see note) |
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

By **default `UV_ALPHA_LIGHT=False`**, so the UV alpha is a constant `1` and the file is
`<part>_UV.png` — safe to read anywhere, including an HTML 2D canvas. Take the light from
the separate `illum_*.jpg`.

If you turn `UV_ALPHA_LIGHT=True` (light packed in alpha, `<part>_UVDL.png`), **do not read
it through a 2D canvas** (`drawImage` + `getImageData`): the canvas stores premultiplied
alpha and un-premultiplies on read, which **corrupts U/V wherever the light is dark**. Then
either:
- upload the `ImageBitmap` straight to the GPU unpremultiplied (WebGL
  `UNPACK_PREMULTIPLY_ALPHA_WEBGL=false`; Three.js texture from the bitmap, no `getImageData`), or
- set `UV_FORMAT='OPEN_EXR'` — float, straight alpha, no quantization. With `EXR_HALF=True`
  + `EXR_CODEC='ZIP'` it is lossless and ≈ PNG file size (~90 KB at 1080p), but needs a float
  EXR loader (e.g. Three.js `EXRLoader`) since browsers can't decode EXR via canvas.

> This script is specific to the **Thomas_Rig_Legacy** rig. Other rigs would require
> adjusting `RIG_ID_VALUE`, `SLIM_CONTROL`, `SELECTION_FORCE_OFF`, `MESH_COLLECTION_PREFIX`,
> and `MASK_ARM_VARIANTS`.
