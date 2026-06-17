# Static export v2 — mesh + UV light atlas (planning)

Status: **export pipeline implemented** (steps 1–4 done end-to-end via `_static_export(players)`;
step 5 = panel button + `app.js` WebGL integration remain). Converges the static wallpaper export
onto the same screen-space mesh format the animated export uses (one mesh key, K=1), replacing the
per-part UV images. Background and rationale: [animated-export-plan.md](animated-export-plan.md)
("Future discussion: the mesh stream as a STATIC format too").

**Build status**: ① atlas bake `_bake_player_light_atlas` ✅ · ② mesh/positions
`_static_write_geometry` ✅ · ③ images `_static_render_images` ✅ · ④ manifest
`_static_write_manifest` + orchestrator `_static_export`/`_static_export_steps` ✅ · ⑤a panel
button `RENDER_OT_novaskin_static` ✅ · ⑤b WebGL renderer (prototype) ✅.
Verified: `<OUT_DIR>/static/` self-contained — `mesh.bin`, `positions.bin`, `background.webp`,
`foreground.webp`, `<label>_atlas.jpg` ×N, `manifest.json`.

**Step ⑤a (panel button)**: `RENDER_OT_novaskin_static` (`render.novaskin_static`, "Export Static
(mesh v2, beta)") inherits the base operator's modal/progress/Esc-cancel plumbing and drains
`_static_export_steps` (one yield per atlas + geometry + images + manifest). Lives in the panel
tab **"Mesh"** (the old "Animated" tab, now hosting both mesh exports). Verified: registers clean,
generator yields 0.2/0.4/…, cancel (`gen.close()`) restores materials/engine/ray-visibility with
no leftover datablocks. **Atlas now filters parts by `hide_render`** too (was baking all 15
basic-look parts for player2 → the hidden slim arm contaminated the classic arm's shared UV
island; now bakes the 7 visible parts, matching the mesh).

The animated mesh pipeline is validated end-to-end, so static-v2 is mostly **"the animated
export with K=1 + a UV light atlas + still images instead of videos"**. The one genuinely
new component is the **UV light atlas bake**.

## Deliverables (`<OUT_DIR>/static/` or reuse the player folders)

```
mesh.bin                 NSKM v2 — welded UV, src (welded→unique), tris (DENSE mesh, see below)
positions.bin            one key of screen positions + z (NSKA v3 with K=1, absolute only)
<player>_atlas.<ext>     UV-space light atlas per player (sampled by uv like the skin)
background.<ext>         scene without players (shadows baked in); opaque
foreground.<ext>         scenery in front of the players — WebP with a real ALPHA channel
manifest.json            static_version, resolution, crop, per-player atlas + tri ranges
```

Same WebGL renderer as the animated mode, with K=1 (no interpolation) and the light sampled
in **UV space** instead of screen space (see the shader note).

**No matte for the static foreground.** The animated mode splits the foreground into RGB +
grayscale matte because no in-browser **video** codec carries alpha on Safari (no VP9-alpha) and
Blender's bundled ffmpeg can't encode it. A static foreground is a still **image**, and WebP
carries a real alpha channel that every target browser decodes — so static ships a single
`foreground.webp` with alpha (lossless or lossy), no separate matte file. (Matte stays
animated-only.)

## The UV light atlas (the new piece)

A per-player texture in the skin's UV layout storing the scene lighting on a 0.5-gray
Lambertian body. The browser relights any skin with `lit = skin(uv) · atlas(uv) · 2` — the
same values as today's `<part>_light.jpg`, but indexed by **uv**, so each Minecraft part's
own UV island carries its own light. A skin that reveals an occluded part (empty hat over a
painted jacket) samples the **jacket's** light, not the hat's — correct by construction, no
base/full screen-space ambiguity.

### Bake (Cycles `bpy.ops.object.bake`) — single pass, Option B ✅ implemented

> **Implemented**: `_bake_player_light_atlas(player, atlas_res=ATLAS_RES, samples=ATLAS_BAKE_SAMPLES)`
> (+ `_atlas_bake_material`, config block `ATLAS_RES`/`ATLAS_BAKE_SAMPLES`/`ATLAS_BAKE_MARGIN`/
> `ATLAS_OVERLAY_LABELS`). One COMBINED pass, Option B ray-visibility, display-encoded via
> `_to_display`, saved as `<player>/light_atlas.<LIGHTSHADOW_EXT>` through `_save_image`. Verified
> on Player 1 (512²/48spp → 48.7 KB JPEG, head lit, scene state restored clean). The caller must
> set the normal look first (`sess.restore_visibility()`); not yet wired into the export operator.

The base/overlay intersection problem: base and overlay parts are **concentric** (overlay
~0.5px outside the base), in **separate UV islands** but nearly coincident in 3D. In a naive
full-player bake the overlay shadows/occludes the base directly beneath it, recording a
"permanent shadow under the hat" into the **base** UV region — wrong when a skin leaves the
overlay empty and the base shows. The only region that ever needs fixing is the **base**, and
it must always be lit as if **its own overlay is absent** (if the overlay is present & opaque
the base is hidden behind it anyway; if absent the base shows and must be overlay-free). So
there are at most 2 concerns, never the 2^N combinations of part on/off.

**Option B — single pass via Cycles ray visibility** (chosen over the two-state A below):

Per player, into one atlas image (e.g. 512×512, sRGB — higher than the 64px skin for a
smooth gradient; the skin samples nearest, the atlas linear):

1. Cycles engine; the parts already have the skin UV map.
2. Swap every part to the **gray-0.5 diffuse** material (`_gray_diffuse_material`) with an
   **Image Texture node = the atlas**, selected/active (the bake target). `bake.margin` for
   edge bleed.
3. On the **overlay** objects only, turn **off** Cycles ray visibility for **Diffuse +
   Shadow** (`obj.visible_diffuse=False`, `obj.visible_shadow=False`). Rays leaving the base
   pass through the overlay as if absent → base lit overlay-free. The overlay is still a bake
   **target** (ray visibility governs how it affects *others*, not how it is itself baked), so
   its UV region bakes normally.
4. Select **all** parts, bake `COMBINED` (or `DIFFUSE` direct+indirect) into the shared atlas
   in **one** call — each part writes its own UV island. Save (PNG/WebP, sRGB; encoding follows
   the panel Quality "Illum/Shadow" option).

Trade-off: with Diffuse/Shadow off the overlays also stop shadowing **each other** — but they
are blocky, sit 0.5px apart and barely overlap, so inter-overlay shadow is negligible for a
light atlas. Keep Option A as a fallback if it ever matters.

**Option A — two scene states (fallback, more correct for inter-overlay shadow):** bake base
parts with overlays hidden (`use_clear=False` accumulates), then bake overlay parts with the
full player visible. Two setups; overlays correctly shadow each other in their own pass.

Risks/notes: bake setup is fiddly (active image node per material, margin, ray visibility);
self-shadow vs scenery comes for free (other parts/scenery cast in the bake); verify the
factor so `skin · atlas · 2` matches a reference Cycles render of the textured player.

### Prototype results (Player 1, 256² / 16spp COMBINED) ✅ validated

- **Separate UV islands confirmed**: head `u∈[0.004,0.496]`, hat `u∈[0.504,0.996]` — disjoint,
  so all base + overlay parts bake into ONE shared atlas with no island collision.
- **Single bake call works**: select all 11 parts → one gray-0.5 diffuse material with a
  shared Image Texture node (active) → `bpy.ops.object.bake(type='COMBINED', use_clear=True)`
  writes every island in one pass. Atlas reads as a clean lightmap in skin-UV layout.
- **Ray-visibility trick decisive**: mean luminance of the head UV region —
  naïve (overlays occluding) = **0.0038** (head sealed near-black under the hat) vs
  Option B (overlay `visible_diffuse=visible_shadow=False`) = **0.2038**. ~50× — the hat
  phantom-shadow is real and severe, and Option B removes it.
- Parts share `UVMap`; gray material assigned to all slots, originals + overlay ray-vis
  restored in a `finally`. Reuses `RM.discover_players()` / `_assign_part_labels`.

### Factor / fidelity validation ✅ confirmed (`×2` stands)

Validated the atlas against a reference Cycles render of the **lit gray** player (the relight
math `skin·light·2` is the same model the screen-space `_light` already ships, so the new risk
was bake fidelity + color space). Method, in one Blender pass (avoids cross-call state loss):
bake the atlas (Option B), render a **UV pass** (UVMap → Emission, `Raw` view transform → exact
per-pixel `(u,v)`) and a **lit-gray** reference, then in Python sample the atlas at each player
pixel's uv, push through the scene view transform (`RM._to_display`, AgX) and compare:

- atlas(display) vs lit-gray reference: **ratio 0.94**, **MAE 0.031** (~3% on 0–1), median
  abs err 0.021, P95 0.092; per-channel ratio R/G/B = 0.90 / 0.94 / 0.97.
- `atlas ≈ lit-gray = 0.5·L`, so `skin · atlas · 2 ≈ skin · L`. The **×2 factor stands**; the
  0.94 is a slight underestimate within bake/denoise noise, consistent with the existing
  `_light` model. Per-channel tilt (~7% R↔B) negligible.

Bake gotchas found (env-specific, do NOT affect the shipped add-on, which writes the atlas to
disk for the **browser** to sample — Blender never re-renders it): (a) `bpy.ops.object.bake`
into a generated image works, but **saving that image then re-using the datablock zeros its
in-memory buffer** — read `img.pixels` and finish in the **same** `execute_blender_code` call,
never round-trip; (b) Cycles renders an **Image Texture node pointing at a generated/just-loaded
image as black** in this MCP context, so validation samples `img.pixels` in NumPy instead of
re-rendering the atlas as emission.

Still open: whether static keeps the legacy `mask`/`bbox`/`base_layer` outputs, and the
positions.bin format (NSKA K=1 vs a dedicated blob) — both deferred to the build step.

### Per-pixel lighting lattice — fixed by flat-shading the bake

A sharp 512px bake showed a regular **grid** in the light, aligned with the skin pixels (visible
in-browser, less clean than the screen-space light). Diagnosed: NOT JPEG 8×8 blocking (a lossless
PNG bake had the same lattice) and NOT subsurf alone — the rig models **each skin pixel as its own
quad** (a body = 352 polys = one quad per face texel) and **SMOOTH-shades** it (an "Auto Smooth"
geometry-nodes modifier) over a pose-curved, subdivided surface, so the gray bake captures a
per-skin-pixel *pillowed* normal variation. The screen-space light hid it by under-resolving at the
player's on-screen size.

A masked low-pass blur removed the lattice but softened the real light/shadow boundaries (the user
wanted those crisp), so the fix is at the **source**: `_bake_player_light_atlas` **flat-shades the
bake** — disables the per-pixel Auto-Smooth + Subdivision modifiers (`ATLAS_BAKE_FLAT=True`,
snapshot/restore `show_render`) so each face is lit per its own orientation: uniform within, sharply
separated at face edges. `ATLAS_BLUR_RADIUS` now defaults to **0** (the blur helpers stay as an
optional knob for strongly pose-curved parts). The geometry stream still uses the dense mesh; only
the light bake is flattened, and the atlas is UV-sampled, so the slight base-vs-dense surface
mismatch is negligible for low-frequency light. The atlas part list is also `hide_render`-filtered
so hidden slim-arm/overlay duplicates don't contaminate shared UV islands. Verified in-browser
(3.2× zoom): clean per-face lighting, crisp light/shadow separation, lattice gone, skin pixels still
crisp (NEAREST), no blur.

## Mesh + positions ✅ implemented

> **Implemented**: `_static_write_geometry(players, out_dir=None)` (+ `_static_collect`,
> `_static_write_mesh`). Writes `static/mesh.bin` + `static/positions.bin` for the current frame,
> reusing `_anim_frame_positions` / `_anim_write_anim`. Verified on the 2-player scene.

- **DENSE mesh**: `_static_write_geometry` sets `AntiLag=False` + `use_simplify=False` (full
  subdivision; K=1 has no per-frame delta budget). Each player's **arm style is left untouched**.
- **mesh.bin is NSKM v2 (`_static_write_mesh`)**: u32 `src` + u32 `tris` — the dense mesh exceeds
  the u16 the animated NSKM v1 uses (measured: welded ≈ 79 k > 65535). `uv` stays u16/65535.
  Header `<4sIIII>` magic, **ver=2**, welded, unique, ntris; payload zlib(uv u16x2, src u32,
  tris u32x3). Browser reads indices as Uint32 for v2, Uint16 for v1.
- **positions.bin is NSKA v3 with K=1** (one absolute key; the consumer degenerates to "use
  key 0"). `_anim_write_anim(path, [pos], ANIM_QUANT, keys_fps=0.0)` — same format as the
  animated stream, shared reader.
- **`_static_collect` includes BOTH base and overlays** (the animated mesh is base-only). Parts
  are selected by **current render visibility** (`not o.hide_render`), which auto-picks the
  player's actual arm variant (classic vs slim) and the overlays it uses, dropping the basic-look
  duplicates `discover_players` carries for the per-part UV export. Base = visible non-overlay;
  overlay = visible `ATLAS_OVERLAY_LABELS`. Per player it records `welded_range`, `tri_range` and
  `overlay_tri_start` (where the overlay shell begins) so the renderer draws base→overlay
  (depth-tested) and alpha-discards the overlay's transparent texels.
- **Gotcha fixed**: filtering by visibility also excised far parked **duplicate/stray meshes**
  (`.001`/`.003`, unused slim arms) that sat at camera depth ~55 and blew the z-range to
  `[6.85, 55.78]`; the cleaned mesh is `[6.85, 8.77]`, giving the GPU depth test ~0.0005 u/level.
- Measured (2 players, 1920×1080): welded 79 475, unique 47 268, **94 464 tris**; mesh.bin
  ≈ 574 KB, positions.bin ≈ 219 KB.
- Crop, foreground, background: same as the animated mode but single still images (step 3). The
  static foreground is one **WebP with alpha** (no matte — see Deliverables).

> **Step 3 implemented**: `_static_render_images(players, out_dir=None)` (+ config
> `STATIC_BG_FORMAT`/`STATIC_BG_QUALITY`/`STATIC_FG_LOSSLESS`/`STATIC_FG_QUALITY`). Renders, at the
> current frame in the DENSE state: `background.<ext>` (players camera-invisible, shadows baked,
> opaque) and `foreground.webp` (scenery-with-players-holdout ∩ player silhouette, real WebP alpha,
> RGB dilated under the edge). **No crop** — a full-frame WebP whose ~99% transparent area
> compresses to almost nothing. Verified: background 1920×1080 opaque ≈ 377 KB; foreground ≈ 23 KB
> (lossless). The player silhouette uses the full visible char parts (base + overlays).
>
> **Foreground alpha — anti-aliased edge fringe, fixed.** The silhouette AA edge produced a 1–2px
> rim of scenery color around the player in the composite. Root cause: a premult-style composite
> (`out = fg.rgb + behind*(1-fg.a)`) over **straight** data. Fix = STRAIGHT alpha + multiply by
> alpha at sample time: **`out = fg.rgb*fg.a + behind*(1-fg.a)`** (manifest `foreground_alpha:
> "straight"` + shader_note). Verified by simulating the composite over a gray player: edge
> overshoot mean/max **0** (vs 0.074 / 0.478 for the buggy form), transparent areas land exactly on
> `behind`. NOTE: do **not** pre-premultiply into the WebP — the encoder discards the rgb of fully
> transparent texels (fills white, `stored_empty_rgb_max=1`), which a premult composite would blow
> out; multiplying by alpha at runtime makes that garbage harmless (×0). Lossless WebP keeps the
> alpha edge crisp.

## Shader (unified renderer)

```glsl
// per player, depth-tested, painter's order:
color = skin(uv) * light(uv_or_screen) * 2.0;   // static: atlas(uv); animated: lightVideo(screenUv)
```
A manifest flag (`light_space: "uv" | "screen"`) tells the renderer which sampler to use.
Background → players (depth test) → foreground (over). Static composites the foreground with its
own WebP alpha (animated uses the separate matte). Otherwise identical to the animated path with
K=1.

## Panel & migration strategy ✅ decided

**Do NOT switch the panel to the new format only — yet.** The new format needs the web tool
to render WebGL; the *current* static wallpaper tool uses a 2D canvas with the per-part UV
PNGs and would break the moment the export changes. Phased instead:

1. **Add** the static-mesh export alongside the legacy per-part UV export (a panel choice,
   or export both during the transition). Legacy stays the default and keeps working.
2. **Integrate** the new static format in `app.js` (WebGL renderer: vertices + UV atlas) —
   this is the WebGL renderer the animated mode also needs, so it is shared work.
3. Once the web tool consumes the new format, **flip the default** to mesh and deprecate the
   per-part UV path.
4. **Later**, add the animated (video) mode on top of the same renderer (the format is
   already K=N-ready; static is K=1).

So the immediate build order matches the user's call: **static mesh + UV atlas first, app
integration, kept video-ready** — animated analysis continues in parallel.

> **Step ⑤b (WebGL renderer)** — prototype `prototype/static.html` + `prototype/static.js`
> (`prototype/static` → the export dir), mirroring `play.js`. Reads `manifest.json`, `mesh.bin`
> (**NSKM v2 u32** indices, `gl.UNSIGNED_INT`), `positions.bin` (NSKA K=1, key 0 absolute, no
> lerp), `background.webp`/`foreground.webp` + per-player `*_atlas.jpg`. Pipeline: blit background
> → draw each player's `tri_range` depth-tested with `frag = skin(uv) * atlas(uv) * 2` (skin
> NEAREST, atlas LINEAR, `s.a<0.5 discard` reveals base under empty overlays) → foreground over
> with standard `SRC_ALPHA, ONE_MINUS_SRC_ALPHA` (the straight composite; transparent white fill
> ×0 is harmless). Verified in-browser (port 8077): 47 268 verts / 94 464 tris / 2 players load
> clean, players relit by the atlas (toggling light off → flat bright skin confirms the multiply),
> foreground composites the waterline over the players with **no edge fringe**. This is the shared
> renderer the animated mode will reuse (K=N positions + light video instead of K=1 + atlas).
> Next: port into the real `app.js` web tool.

## Decided

- **Atlas resolution: 512** (up to 1024 if needed). Higher than the 64px skin for a smooth
  light gradient.
- **Encoding: follows the panel Quality "Illum/Shadow" option** (JPEG / WebP / PNG +
  quality) — it is only lighting, so it does NOT need to be lossless, same model as the
  current `_light` images.
- **One atlas per player.**

## Open questions

- ~~Exact bake pass/factor so `skin · atlas · 2` matches a reference render.~~ ✅ resolved —
  `×2` confirmed (ratio 0.94, MAE 3%); see "Factor / fidelity validation" above.
- Whether static keeps the legacy `mask`/`bbox`/`base_layer` outputs for tools that still
  read them, or drops them once the web tool is ported.
- ~~positions.bin format: reuse NSKA (K=1) or a dedicated blob.~~ ✅ resolved — reused NSKA v3
  with K=1 (shared reader). mesh.bin needed a new **NSKM v2** (u32 indices) for the dense mesh.
- Overlay inclusion policy: the static mesh currently matches the **rendered** geometry (a
  player's 2nd-layer toggled off in Blender is absent). If the web tool should toggle overlays
  the player doesn't currently wear, force "Second layer" on at collect time (one line) like the
  animated screen-space passes — deferred until the app side needs it.
