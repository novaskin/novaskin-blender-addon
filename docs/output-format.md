# Output format reference

Technical reference for consuming the exporter's output (web tool / pipeline developers).
For installing and using the add-on, see the [README](../README.md). For the animated
export internals, see [animated-export-plan.md](animated-export-plan.md).

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
│   ├── mask_classic.png             # 8-bit PNG
│   ├── mask_slim.png
│   ├── illum_classic.jpg / illum_slim.jpg     # full-body light
│   └── shadow_classic.jpg / shadow_slim.jpg   # cast shadow (multiply)
├── background.png                   # scene without players and optional layers
├── layers/                          # optional layers (groups marked in the panel)
│   ├── <name>.png                   # the group alone, transparent bg, display-encoded
│   ├── <name>_shadow.jpg            # shadow it casts on the scenery (display multiply)
│   ├── <name>_UV.png                # single-mesh groups only; EXR mode: <name>_UVDL.exr
│   └── <name>_light.jpg             # its light map (PNG pipeline)
└── manifest.json                    # manifest_version, addon_version, resolution,
                                     #   draw_order (back→front), bboxes, depth ranges, etc.
```

## Part labels

UV part filenames use Minecraft-style labels: `head`, `hat`, `body`, `jacket`,
`arm_{left,right}_{classic,slim}`, `sleeve_{left,right}_{classic,slim}`,
`leg_{left,right}`, `pant_{left,right}` (`classic` = Steve arms, `slim` = Alex).
Unmapped meshes fall back to a sanitized object name; duplicate labels get a `_2`/`_3`
suffix (a warning is printed — usually a stray mesh in the rig). The label → object-name
mapping is in `manifest.json` per player. `base_layer_{classic,slim}` composite the base
parts, nearest depth winning.

## UV files

- RGBA = (U, V, normalized depth, coverage). Coverage alpha is 0/1, so the files read
  fine anywhere, including an HTML 2D canvas (no premultiply damage — see below).
- **U/V quantization (PNG/WebP, 8-bit):** byte = `floor(u * 256)`; texel =
  `floor(byte * texW / 256)` — equal to the byte for a 64/128/256-wide skin. The floor
  convention keeps a value inside texel *i* in texel *i* (round would push the upper half
  into the next texel). Recorded in the manifest as `uv_decode_note`.
- **Depth (B):** normalized per player/layer. Absolute depth =
  `zmin + B × (zmax − zmin)` with `[zmin, zmax]` = that entry's `depth_range_viewer` in
  the manifest. Players and layers share the same scale → **comparable for per-pixel
  depth-checked compositing** (smaller = nearer).
- **WebP UV** (`UV_FORMAT='WEBP'`) is lossless (quality 100), ~60 % smaller than PNG;
  byte-exact inside the coverage (alpha-0 RGB is premultiply-zeroed on save — never read).
- **EXR UV** (`UV_FORMAT='OPEN_EXR'`): same RGBA as floats, plus the RGB light embedded
  as a `light.R/G/B` channel layer → `<part>_UVDL.exr`. Read with a float EXR loader
  (e.g. Three.js `EXRLoader`). Half-float + ZIP ≈ PNG file size, lossless.
- Light is never packed into a PNG alpha: a variable alpha gets corrupted by the
  2D-canvas premultiply round-trip (it un-premultiplies on read, mangling U/V where the
  light is dark).

## Applying the light and shadow

- **Light** (`<part>_light.jpg` / `illum_*.jpg` / EXR `light` layer): the scene lighting
  captured on a **0.5 gray** Lambertian body, sRGB-encoded. Relight with
  `lit = skin × (light / 0.5)` (≈ multiply + 2× exposure in a 2D canvas). Base parts are
  lit from a base-only render, overlays from the full render — each layer is lit where it
  is front-most. Self-shadowing is already in the light; `shadow_*` is only the shadow
  cast on the scenery.
- **Shadow** (`shadow_*.jpg`, players and layers): a **display-space multiply** map
  (white = no shadow), view-transformed like `background.png` so a straight multiply in
  an sRGB canvas reproduces the render. Combine multiple shadows with *darken*
  (per-pixel min), then *multiply* onto the background.

## Optional layers — semantics

A layer is a *group* of meshes (a marked collection, a marked armature's rig, or a single
mesh). The group renders **together** (self-occluding) with players and other layers
excluded — they can be toggled off in the wallpaper, so the layer must be whole with
respect to them. Unlike players (always unoccluded, drawn in front), a layer **is
occluded by the scenery**: the scenery renders as a holdout (cuts the alpha where it is
in front, still lighting/shadowing the group) and the UV coverage is clipped the same
way. Multi-mesh groups have no UV/light files (their meshes don't share one texture
space). Trade-off: a layer's shadow never falls on a player. Manifest entries carry
`kind`, `meshes`, bbox, `camera_depth`, `depth_range_viewer` and join
`draw_order_back_to_front`.

## Animated export (`animated/`)

A separate output for the animated wallpaper, consumed by a WebGL player (reference:
[prototype/play.html](../prototype/play.html)). Design rationale:
[animated-export-plan.md](animated-export-plan.md). Layout:

```
animated/
├── background.webm        full-frame scenery (player shadows baked in); lossy VP9
├── foreground.webm        scenery IN FRONT of the players; RGB only, cropped (no alpha)
├── foreground_matte.webm  the foreground's alpha as grayscale; cropped (Safari-safe)
├── light.webm             combined player light (gray-0.5 basis); cropped
├── mesh.bin               static geometry (NSKM)
├── anim.bin               per-frame screen positions + depth (NSKA)
└── manifest.json          fps, frames, resolution, crop, file list, format versions
```

The character is NOT video — it is a screen-space triangle mesh re-textured live with the
user's skin. Only base-layer parts; players always drawn.

### Compositing recipe (per displayed frame `t`)

1. Draw `background.webm` full-frame.
2. Draw each player's mesh (back-to-front), with a **depth test** on the vertex z and
   `color = skin(uv) · light(screenUv) · 2` (display space). Vertex positions are the mesh
   keys interpolated at `t` (see anim.bin). `light` is sampled in screen space, offset by
   the crop: `screenUv = (fragCoord − cropOrigin) / cropSize`.
3. Draw the foreground over the players: `rgba = (foreground.rgb, foreground_matte.r)` with
   straight-alpha "over", positioned at the crop rect (not full-frame).

`light`/`foreground`/`foreground_matte` are cropped to `manifest.crop`; `background` is
full-frame. Keep the three secondary videos time-locked to the background's clock.

### `mesh.bin` — NSKM (static, little-endian)

`< 4s I I I >` header: magic `NSKM`, version (1), `welded`, `unique`, `tris`, then a
**zlib** payload of:
- `uv`: `welded × u16×2` — skin UV per welded vertex, `/65535` → 0..1.
- `src`: `welded × u16` — maps each welded vertex to its **unique** position index (box UV
  seams split positions; anim.bin only stores the `unique` positions).
- `tris`: `tris × u16×3` — triangle indices into the welded vertices.

`manifest.mesh.players[]` gives each player's `tri_range` (draw its own skin) and
`vert_range`, back-to-front.

### `anim.bin` — NSKA v3 (per-frame, little-endian)

`< 4s I I I f f f f f >` header: magic `NSKA`, version (3), `V` (unique verts), `K` (mesh
keys), `quant` (x/y px scale, 8), `keys_fps`, `zmin`, `zmax`, `zq`, then a **zlib** payload
of `K` keys of `V × i16×3` (x, y, z):
- key 0 absolute, key 1 = delta vs key 0, keys 2+ = **delta-of-delta** (linear motion
  predictor). Reconstruct by accumulating a running delta.
- `x, y = int / quant` (screen pixels, y up).
- `z = int / zq` → 0..1, the normalized camera depth (`= (camDepth − zmin)/(zmax − zmin)`);
  one scale across all players. Feed straight into the depth test (smaller = nearer).
- Display position at time `t`: `key = t · keys_fps`; lerp `keys[floor(key)]` →
  `keys[floor(key)+1]`. Older v2 has no `zq` (z `/32767`); v1 has no z.

## Manifest essentials

- `manifest_version` / `addon_version` — format and exporter versions.
- `render.resolution` — the **effective** export size every image matches
  (`base_resolution` is the scene setting; `draft: true` + `res_pct` on draft runs).
- `players[]` — folder, armature, `visible_bbox`, `part_bboxes`, `uv_parts`
  (label → object), `camera_depth`, `depth_range_viewer`.
- `layers[]`, `draw_order_back_to_front` — see above.

## CONFIG reference

Constants at the top of `novaskin_export.py` (the panel overrides the common ones at run
time):

| Key | Purpose |
|---|---|
| `OUT_DIR` | Output folder (default `//novaskin/`) |
| `PLAYER_FOLDER_SCHEME` | Per-player subfolder name: `index` (`player1`, …) or `armature` |
| `MASK_ARM_VARIANTS` | Arm variants exported (`classic`/`slim`) |
| `UV_DEPTH_IN_BLUE` | Write normalized depth into the UV's B channel |
| `EXPORT_BACKFACE_UV` | Also export the back-face UVs |
| `EXPORT_ILLUM` / `EXPORT_SHADOW` | Per-player light and cast shadow — independent toggles |
| `EXPORT_BACKGROUND` | Render the scenery without players/layers (`background.png`) |
| `LAYER_ID_PROP` / `EXPORT_LAYER_UV` | Optional layers: marker property; UV+light per single-mesh layer (default on) |
| `EXPORT_PART_LIGHT` | Save the per-part light images (default on) |
| `SHADOW_DISPLAY_RATIO` | Shadow ratio in display space (default on) |
| `ILLUM_HIDE_SCENERY_FROM_CAMERA` | Light renders: scenery camera-invisible (default on) |
| `BACKGROUND_USE_SCENE_SETTINGS` | Background with the user's engine/samples/denoise (default on) |
| `PNG_COMPRESSION` | zlib level for data PNGs (default 90; byte-exact) |
| `UV_PNG_FLOOR_TEXELS` / `UV_TEXEL_BINS` | Floor-texel U/V quantization (see above) |
| `FIX_2LAYER_POSITION` / `HAT_SCALE_RATIO` | Snap/scale the hat onto the head before export |
| `SELECTION_FORCE_ON` | Rig toggles forced ON during export (default: `Second layer`, so overlays always export); restored after |
| `ILLUM_SAMPLES` / `ILLUM_COLORSPACE` | Illum quality/encoding |
| `PNG_BIT_DEPTH` | UV/mask PNG bit depth (default 8) |
| `UV_FORMAT` / `EXR_HALF` / `EXR_CODEC` | UV format: `PNG`, `WEBP` (lossless) or `OPEN_EXR` |
| `COMPOSITE_BASE_LAYER` / `COMPOSITE_BASE_LABELS` | base_layer composites |
| `LIGHTSHADOW_FORMAT` / `JPEG_QUALITY` | Light/shadow format: `JPEG`, `WEBP` or `PNG`; quality |
| `DRAFT_RES_PCT` / `DRAFT_SAMPLES` | What the Draft buttons use (50 % / 8) |
| `RIG_ID_PROP` / `RIG_ID_VALUE` | How players are detected |
| `RENAME_UV_PARTS` / `MC_PART_MAP` | Minecraft part-label renaming |
| `ANIM_*` | Animated export: keys step, quantization, CRFs, encode toggle — see [animated-export-plan.md](animated-export-plan.md) |

> The exporter is specific to the **Thomas_Rig_Legacy** rig. Other rigs would require
> adjusting `RIG_ID_VALUE`, `SLIM_CONTROL`, `SELECTION_FORCE_OFF`,
> `MESH_COLLECTION_PREFIX` and `MASK_ARM_VARIANTS`.
