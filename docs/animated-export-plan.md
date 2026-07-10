# Animated Export — Planning Doc

Status: **SHIPPED — format v5** (this doc is the original plan plus phase notes; the
sections below describe versions 1-2 and are kept as history. Summary of what actually
ships as of 2026-07):

- **Streams per export**: `background.webm` (players camera-invisible, shadows baked),
  `occlusion.webm` (per-pixel player-vs-scenery matte, VP9 lossless — see below),
  `light.webm` (below), `view_lut.png` (the scene's view transform as a 64³ 3D LUT),
  `mesh.bin` (NSKM, u16/u32 auto) + `anim.bin` (NSKA v3, 12-bit z) and `manifest.json`
  (`animated_version: 5`).
- **Occlusion** (player vs scenery): the exporter computes it in FLOAT and ships the
  RESULT, not depth. The bg render leaves the SCENERY camera depth in `zz.exr`; a dedicated
  occlusion render — the FULL player (base + overlays), scenery camera-invisible, ONE
  sample (Z + coverage are geometry-exact, no lighting) — leaves the PLAYER front depth in
  the same EXR, and its alpha marks where a player is. `matte = (alpha > 0.01) AND (z_scene
  - ANIM_OCC_DELTA < z_player)` → a per-pixel 0/1 mask cropped like the light. The small
  delta (normalized band units) widens occlusion slightly at the contact boundary, so the
  player fragments that would poke through where it meets scenery (the black fringe) get
  culled — the float heir of the old depth guard. It is a SEPARATE render from
  the light (which is base-only, so its base light stays overlay-free): the overlay shells
  extend past the base (~+54% coverage measured) and must occlude against scenery too. The
  viewer discards player fragments where the matte is white. This REPLACED `scene_depth.webm`
  (scenery Z quantized to 8-bit and seeded into the depth buffer): quantized depth left a
  hard black fringe where a shadowed player part (e.g. a leg behind grass) failed the
  imprecise per-fragment test — the matte cuts at the render's exact silhouette because the
  compare is done in float offline.
  Player-vs-player occlusion stays a real GPU depth test on the per-vertex mesh z (drawn
  back-to-front). Translucids (water tint over a submerged player) remain binary occlusion.
- **Light**: ALWAYS screen-space (drafts and full quality) — the visible gray players
  rendered at frame resolution, stored sRGB-encoded (`0.5·L`), sampled by screen position;
  the viewer decodes to linear, relights `skin_lin × L × 2`, and applies the scene's real
  view transform via `view_lut.png` (AgX in GLSL would only approximate; the LUT matches
  OCIO to ~2/255). Two per-frame atlas modes were tried and DROPPED (git history has
  both): REPROJECT (atlas from the light render's UV pass — sampling density
  unsatisfactory) and BAKE (per-player Cycles bake per frame — per-face atlas resolution
  loses shadow detail the screen render keeps, ~3× less on a near player, plus the
  AgX-encoded tiles could not be decoded exactly in the shader, plus ~20 s/player/frame).
  The static export still bakes its atlas: one bake amortized over the wallpaper,
  lossless WebP, no per-face-vs-screen resolution race.
- **Players**: base + classic 2nd layer (per-part `parts[]` tri ranges in the manifest,
  toggleable in the viewer; labels come from UV-rect classification, not object names).
  BOTH arm styles ship (same design as the static export): the inactive variant's parts are
  emitted after the overlays with a `variant` field (`classic`/`slim`), and their per-frame
  key positions are captured with the rig style flipped (the parked variant poses at the
  arm). `default_variant` records the style the atlas/light was rendered for — the other
  borrows its rects (the two UV nets differ ~1px). The rig's inset per-pixel UVs are
  expanded for the export.
- **Viewer** (`prototype/play.js`): CURRENT format only (prototype, no back-compat — old
  atlas/fg/matte exports are rejected with a "re-export" message). texAA + premultiplied
  two-pass alpha, matte-discard occlusion, screen-light relight in linear + view-transform LUT,
  per-part toggles + per-player classic/slim arm toggle, folder picker, debug views
  (wireframe / depth / light / grid), one-loop composite recording (⏺ rec → shareable
  MP4/WebM; the tab must stay visible while it records).
- **Costs** (reference scene, 2 players): draft ≈ 5 s/frame; full quality ≈ 14 s/frame
  (bg + light at full samples + a 1-sample occlusion render, no bakes). `ffmpeg` is required at
  render time (the depth pass is read back through it).

---

Original planning document follows (historical):

## Goal

Export an *animated* wallpaper (N frames) that the web customizer can retexture with the
user's skin **in the browser**, without downloading per-frame images or huge files.

The static pipeline exports per-pixel UV maps (PNG/WebP). That does not translate to
video: **browsers ship no lossless video codec** (FFV1 etc. don't decode in browsers), and
4:4:4 chroma (required — U/V live in R/G) is software-only where it exists at all, with
weak Safari/AV1 coverage. A per-pixel "UV video" is therefore a dead end. See research
notes at the bottom.

## Key idea ✅

Split the data by **loss tolerance**:

| Data | Tolerates lossy video? | Transport |
|---|---|---|
| Background (scenery + baked shadows) | yes — natural image | lossy video |
| Foreground (scenery in front of players) | yes | lossy video + alpha |
| Light (combined, all players) | yes — smooth gradients | lossy video |
| Character geometry / UV | **no** — must be exact | **screen-space mesh stream** (not video) |

Instead of per-pixel UVs, export the player meshes **already projected to screen
coordinates**, per frame, each vertex carrying its skin `(u, v)`. The browser renders the
triangles with WebGL/Three.js, sampling the user's skin by UV and the light video by
screen position:

```glsl
color = texture(skin, vUv) * texture(light, vScreenUv) / 0.5;
```

- Deformation is **exact** (real evaluated vertices: lattices, bends — whatever survives
  the simplification level, see *Mesh density*).
- The only precision-critical data (vertex positions) travels in a compact binary, not in
  a video.

## Simplified mode — scope decisions ✅

This mode trades flexibility for size; it is NOT the static pipeline:

- **Base layer only** (no hat/jacket/sleeve/pant overlays) — at least initially.
- **No per-player toggles**: all players always drawn. Therefore:
  - **No `shadow.webm`** ✅ — cast shadows are *baked* into `background.webm` (render the
    scenery with players camera-invisible but still casting, the same trick the static
    shadow pass uses) and self-shadowing is already in the light maps.
  - **One combined `light.webm`** for all players (all players gray at once, scenery
    camera-invisible). Where players overlap, the front player's light wins — which is
    also the fragment that survives the draw order, so it is consistent.
- **No per-player depth maps**: inter-player ordering is painter's order per frame
  (tiny data). `player_id` lives in the mesh data (no blue-channel hacks needed).

## Deliverable layout

```
animated/
├── background.webm     lossy video — scenery BEHIND the players, shadows baked in
├── foreground.webm     lossy video + alpha — scenery IN FRONT (occludes the players)
├── light.webm          lossy video — combined player light (multiply, gray-0.5 basis)
├── mesh.json           static: triangle indices, per-vertex skin UV, per-player vertex
│                       ranges, part labels, counts
├── anim.bin            per frame: vertex screen positions (+ optional depth),
│                       per-player draw order
└── manifest.json       fps, frame count, resolution, file list, format versions
```

### `mesh.json` (static, written once)

```jsonc
{
  "players": [
    {
      "label": "player1",
      "vertex_range": [0, 1520],        // into the shared vertex buffer
      "triangles": [/* index triples into vertices */],
      "uv": [/* u,v per vertex — skin space, floor-texel convention as static */],
      "parts": {"head": [0, 240], "body": [240, 600], ...}   // optional, debugging
    }
  ],
  "uv_convention": "same as static export (floor texel, 64x64 base)"
}
```

### `anim.bin` (per frame)

- Header: magic, version, vertex count V, frame count F, fps, quantization scale.
- Per frame: `V × (x, y[, z])` as **int16**, quantized to 1/8 px at export resolution;
  stored as **delta vs previous frame** (motion is smooth → tiny deltas), then the whole
  file is served gzip/brotli (HTTP does this for free). `z` only present if occlusion
  Option B is used.
- Per frame: player draw order (back→front), one byte per player.
- Budget estimate: 1 500 verts × 300 frames × 2 × 2 B = 1.8 MB raw → delta+gzip typically
  **< 500 KB**. Scales linearly with vertex count — see *Mesh density*.

### Videos

- Codec: VP9 or AV1 in WebM (alpha supported for `foreground.webm`); H.264+matte fallback
  if Safari coverage demands it. Standard 4:2:0 is fine — these are natural images.
- The light video is *display-referred the same way the static light is* (sRGB encode of
  the gray render) so the shader stays `skin * light / 0.5`.
- Resolution: export res (1080p), bitrate tuned per layer; `light.webm` can typically be
  half resolution (light is low-frequency) — decide in the prototype.

### Why the light is NOT merged into `foreground.webm` (considered ✅)

WebM alpha is a full 8-bit plane, so a semi-transparent overlay (e.g. 50 % black over the
player) *is* expressible — and "over" compositing with black does reduce to a scalar
multiply (`result = player × (1−a)`). But the player light needs more than darkening:
**colored light** (per-channel multiply — one alpha can't do R≠G≠B) and **gain above 1×**
(`light/0.5` brightens up to 2×; "over" can only lerp toward the overlay color — a white
overlay fogs, it doesn't multiply). That is exactly why the player shader samples the
light as a texture (`skin × light/0.5`).

A colored semi-transparent overlay (e.g. yellow at 50 %) *does* tint the player — but it
is the wrong kind of tint: "over" is `offset + uniform scale`, so it **lifts blacks**
(black skin pixels acquire the overlay color and glow) and washes contrast, where real
light is a per-channel multiply that keeps blacks black. The error is also skin-dependent
(the skin is unknown at export time — users swap skins), while multiply is correct for any
skin. And keeping the light separate costs ~nothing: the player is already a custom draw
call, so sampling one more texture there is free (even a pure 2D-canvas pipeline has a
`multiply` composite op for a masked light layer, as the static tool already uses).

Packing note for a possible v2: the regions are complementary (light only matters where
the player is visible = where the foreground is transparent), so the light could ride in
the foreground's unused RGB — as data for the player shader to sample, NOT as an "over"
layer — and save one video decode. Rejected for v1: AA edges + lossy alpha bleed scenery
RGB into the light exactly at occlusion contact pixels, and `light.webm` at half
resolution is already the cheapest of the three streams.

## Occlusion by the scenery

The mesh must be occluded by scenery, like everything else. Two options:

**Option A — foreground layer (recommended ✅ for the simplified mode)**
Blender splits the scenery per frame into *behind* / *in front of* the players (it knows
exact occlusion). `foreground.webm` carries the in-front part with alpha; the browser
draws background → players (mesh) → foreground. Exact, no depth math in the browser, the
same holdout technique the static optional-layers already use. Limitation: assumes the
players occupy a contiguous depth band — fine for wallpapers; objects *interpenetrating*
a player would split imperfectly.

**Option B — scenery depth video (fallback, more general)**
Export a scenery depth video (depth is smooth → tolerates lossy, unlike UV) + per-vertex
`z` in `anim.bin`; the fragment shader discards player fragments behind the scenery.
Handles interpenetration; costs one more video + z stream + shader work. Only adopt if A
proves insufficient on real scenes.

## Mesh density (size lever) ✅

The Thomas rig has built-in reducers (Misc panel): **AntiLag** (big reduction) and
**Simplify** (with a level, milder). Both trade bend smoothness for vertex count. Plan:

1. Measure evaluated vertex counts of the base layer at: full / Simplify (each level) /
   AntiLag, in the live scene (depsgraph-evaluated, post-modifiers).
2. Pick the default that keeps silhouettes acceptable at wallpaper size; expose it as an
   export option later. An additional Decimate-on-export can be applied if needed.
3. The 226-verts-per-sleeve figure observed earlier suggests full-res is thousands of
   verts per player — the reducers are likely mandatory, not optional.

## Browser pipeline (per displayed frame)

1. Seek/decode the three videos to frame t (HTMLVideoElement in lockstep, or WebCodecs
   for exact frame control — prototype decides; lockstep `requestVideoFrameCallback` is
   the simple path).
2. Draw `background` to the canvas.
3. For each player in draw order: draw its triangles (Three.js `BufferGeometry`, one
   position update per frame from `anim.bin`) with the
   `skin × light/0.5` shader.
4. Draw `foreground` on top.

Retexturing = swap the skin texture; nothing else changes. Static skin × animated scene.

## Phases

- **Phase 0 — measurements (Blender, cheap):** evaluated vert counts at each
  simplification level; verify projected-vertex export (`world_to_camera_view` on the
  evaluated depsgraph mesh); confirm the foreground/background split renders.

### Phase 0 results (2026-06-12, wallpaper1.blend, 2 players, 15 s / 360 f camera sway) ✅

- **AntiLag is mandatory.** The rig's AntiLag drives `Subdivision.show_viewport`
  (viewport-only; render keeps level 3 — irrelevant, the mesh comes from the viewport
  depsgraph). Base-layer (classic) totals for 2 players:
  - default (subsurf 1): **34 328 verts** → 49 MB raw — inviable.
  - AntiLag (subsurf off): **3 416 verts / 6 784 tris** → 4.9 MB raw.
- **Projection pipeline verified**: evaluated-depsgraph meshes, camera
  `calc_matrix_camera` per frame (handles the Track To constraint), topology constant
  across frames, 97.8 % of verts in-frame, ~**2 ms/frame** extraction (free next to
  renders).
- **`anim.bin` budget validated** (real consecutive-frame deltas, int16 + zlib):
  - 1/8 px quant: **0.92 MB** for 360 f; 1/4 px: **0.74 MB**.
  - With mesh keys at 12 fps + browser interpolation: **0.37–0.46 MB** ✅ (target ~0.5 MB).
- **With CHARACTER animation** (vigorous swim stroke keyed on Player 1 — arm steps up to
  34 px/frame — on top of the camera sway; Player 2 static):
  - plain delta: 1.49 MB / 360 f; **delta-of-delta (linear motion predictor): 1.26 MB**,
    or **0.63 MB at 12 fps keys** — the predictor recovers ~15 %, smooth motion is
    second-order-small. Both players animated would land roughly 2× the animated share
    (a static player's deltas compress to almost nothing), still ≈ 1–1.3 MB @ 12 fps —
    comparable to one of the three videos. Budget holds; 12 fps mesh keys + browser
    lerp is the recommended default.
- **Foreground split verified — and needed**: at frame 180, **19.7 % of the player
  silhouette** (14 260 px) is occluded by scenery. Two cheap data renders produce it:
  1. scenery with players as **holdout** (alpha hole where a player is in front);
  2. player **unoccluded silhouette** (scenery hidden);
  `foreground = render(1) ∩ silhouette(2)` — per pixel.

  Design note: the split is **per-pixel, not per-object** (the ground plane spans the
  whole depth range, so an object-level front/behind split is impossible). Restricting
  the foreground to the silhouette also protects the player's AA edges from being
  overwritten by redundant scenery pixels.
- **Phase 1 — one-frame web prototype (the go/no-go gate):** export ONE frame's mesh +
  light + background/foreground as stills; render it in Three.js with a real skin;
  compare against the Blender render. If the look matches on one frame, time is just
  repetition.

### Phase 1 results (2026-06-12, frame 180) — **GO** ✅

`prototype/` (WebGL2, no deps; `python3 -m http.server` in that dir): background quad →
players (skin×light×2 shader, painter's order) → foreground quad. Data exported from
Blender at frame 180 (AntiLag, classic, base-only, render simplify=0 so rendered geometry
matches the viewport mesh).

- **Pixel-diff vs the Blender reference render: MAE 0.69/255, median 0, p95 < 1; only
  2.1 % of pixels differ by > 10** (mesh AA edges + render-to-render noise between two
  independent 16-sample renders). Visually indistinguishable at 1×.
- The scene's water occluding the swimming players is reproduced exactly by the
  per-pixel foreground layer.
- **Welding discovery:** per-(vertex, UV) welding for the GPU buffers gives 13 234 verts
  (3.9× the 3 416 raw — box UV seams split heavily). Positions of the duplicates are
  identical, so `anim.bin` must store positions for **unique vertices only** (3 416) and
  `mesh.json` carries a static `src: welded → unique` index map. The 0.37–0.92 MB budget
  stands.
- Light sampled by `gl_FragCoord/resolution`; one FLIP_Y convention for every texture
  (v=0 = bottom) keeps Blender UV space, screen space and GL consistent.
- **Phase 2 — exporter MVP:** frame loop in the add-on (reuse `_Session`/steps/progress);
  write `mesh.json`/`anim.bin`; encode videos (Blender renders PNG sequences; ffmpeg or
  Blender's own encoder for WebM).

### Phase 2 results (2026-06-12) — MVP shipped ✅

`render.novaskin_animated` (panel **Animated (beta)**, + Draft button). Per frame:
background (players camera-invisible, shadows baked), per-pixel foreground
(scenery-holdout ∩ silhouette), combined light; mesh keys every `ANIM_KEYS_STEP` frames.
Outputs `<OUT_DIR>/animated/`:

- `mesh.bin` — `NSKM` header + zlib(uv u16×2, src u16 welded→unique, tris u16×3)
- `anim.bin` — `NSKA` header + zlib(int16 1/8 px: abs, delta, then delta-of-delta)
- `manifest.json`, `background/foreground/light.webm` (ffmpeg VP9; fg `yuva420p` alpha;
  falls back to `encode.sh`, and searches homebrew paths — GUI Blender has no shell PATH)

Verified end-to-end on an 8-frame draft (480×270): binaries parse byte-exact in the
browser (`DecompressionStream('deflate')`), videos encode (bg 78 KB, fg 84 KB, light
2.7 KB), and `prototype/play.html` composites correctly — water occluding the swimmers,
per-player skin textures, keys lerped, no console errors.

**Known gaps / Phase 3+ list:**
- Video sync: 3 `<video>` elements kept aligned by a 500 ms re-sync nudge; consider one
  WebCodecs clock or muxing streams later. Loop-seam behavior unverified on long clips.
- ~~Draw order fixed at frame_start~~ → solved: NSKA v2 carries per-vertex camera depth
  (shared scale) and the player uses a GPU depth test — correct self-occlusion (legs
  behind no longer draw over) AND per-pixel inter-player ordering. Cost: anim.bin ~2×
  (z quantized to 16 bits is noisy; a 12-bit z is a pending size tweak).
- Esc during a batch now asks for confirmation (second Esc within 3 s cancels).
- Slim variant unsupported (classic forced); overlays/hat excluded by design (base only).
- ~~Safari: no VP9-alpha~~ → solved: the foreground is split into an RGB video + a grayscale MATTE video (its alpha). No alpha channel anywhere, all plain yuv420p, so it decodes on Safari. The web player combines rgb + matte (falls back to a single RGBA fg for older exports). Also lets Blender's bundled ffmpeg encode it later (no alpha needed).
- Panel doesn't expose keys_step / CRFs / keep-sequences yet (CONFIG constants only).
- Light could be encoded at half resolution (cheap win).

### Encoder & cross-platform (2026-06-12)

- ffmpeg search now covers per-OS install locations (homebrew/usr on Unix, ProgramFiles on
  Windows) and falls back to writing `encode.sh` (Unix) or `encode.bat` (Windows). The PNG
  sequences are the resume point if a batch is interrupted (keep them, skip existing).
- Blender ships its OWN ffmpeg (file_format='FFMPEG', media_type='VIDEO', WEBM + VP9/AV1
  confirmed in 5.1) -- encoding via the VSE would drop the system dependency AND is
  cross-platform. PARKED: routing our pre-baked display frames through the sequencer
  re-applies a color transform (needs a browser-validated config); WebM-alpha is unreliable
  (the matte removes that blocker). Color config is the remaining work before switching;
  until then, system ffmpeg + the script fallback.

### Phase 3 — crop optimization (2026-06-12) ✅ validated

The foreground was nearly as big as the background (8.5 MB / 360f) despite being ~97%
transparent: it carried the scenery RGB outside the silhouette, and the full frame was
encoded though content is localized to the players. Two fixes (ANIM_CROP_PAD, default on):
- zero the foreground RGB where alpha=0 (flat -> compresses to nothing);
- crop foreground + light to the union of the players' screen bboxes (+ pad), computed in
  a key pre-pass; even dims for yuv420p; background stays full-frame.
manifest.crop (top-left px) drives the web side: the foreground quad is positioned at the
rect and the light is sampled at (fragScreen - cropOrigin)/cropSize.
Verified e2e (12-frame draft): crop 380x180 (~13% of frame); foreground 42 KB vs 369 KB
background (was comparable); composite, occlusion and light alignment all correct in the
player; also speeds the fg/light render passes (fewer px).
- **Phase 3 — size pass:** delta+quantization tuning, light at half-res, bitrate ladder.
- **Phase 4 — customizer integration** (out of this repo).

## Risks / open questions

- **Alpha video support** for `foreground.webm` across browsers (VP9/AV1 alpha in WebM is
  Chrome/Firefox-good; Safari may need an H.264 + separate matte fallback). Prototype
  must test Safari early.
- **Video seek sync**: three `<video>` elements staying frame-locked; `requestVideoFrameCallback`
  vs WebCodecs. Decide in Phase 1.
- **Light bleed at player overlaps** (combined light): expected to be invisible since the
  winning fragment matches the winning light; verify in Phase 1 with two overlapping rigs.
- **Anti-aliasing at mesh edges** vs the video background: MSAA on the WebGL canvas
  should match well enough; verify.
- How many frames / fps for a wallpaper loop (size scales linearly with F).

## Future discussion: the mesh stream as a STATIC format too (v2 of the static export)

> Detailed plan: [static-mesh-plan.md](static-mesh-plan.md).

The screen-space mesh could replace the per-part UV images in the static pipeline as
well — and it is arguably a better fit there: one frame of positions is ~14 KB (vs
hundreds of KB of WebP UVs per player), resolution-independent (crisp at 4K from the
same export), 16-bit UV precision, GPU-AA edges, per-pixel depth via the v2 z, part/
variant toggles become triangle ranges, back faces come free (disable culling + depth
test), and masks/bboxes/base_layer composites become unnecessary. For the light, two screen-space images (base/full) are NOT enough (noted by the user):
screen-space light bakes the front-most surface per pixel assuming a fixed opacity, but
the SKIN decides transparency at runtime — when a skin reveals an occluded part (empty
hat over a painted jacket), that part samples the front part's light. The correct model
is a **UV-space light atlas** baked into the skin's UV layout (one per player), sampled
by uv like the skin: `lit = skin(uv) * lightUV(uv) * 2`. Each Minecraft part has its own
UV island, so each carries its own light and reveals work by construction — no base/full
split needed. Bake it higher-res than the skin (e.g. 256/512, linear) for a smooth light
gradient. (The current static export's per-part `<part>_light` images already get this
right; the 2-image idea was an over-simplification.) The static variant also keeps a
foreground image for scenery occlusion and background/shadows as today.

Note: the UV-light atlas is a STATIC elegance — for animation the light changes per frame
(it would be a small UV-space light video). The current base-layer animated mode dodges
the problem: no overlays, and the base layer is rarely transparent, so there is almost
nothing to reveal. A single frame can afford a DENSE
mesh (subsurf 1, ~34k verts ≈ 136 KB) — render-matching silhouettes, no AntiLag
compromise. Cost: the web tool must render WebGL instead of 2D canvas — which the
animated integration already requires, so the right order is: integrate animated in
app.js first, then converge the static export onto the same renderer as K=1.

## Research notes (June 2026)

- No lossless or near-lossless codec is generally available in browsers
  ([MDN codec guide](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Formats/Video_codecs)).
- WebCodecs decodes H.264/H.265/VP8/VP9/AV1 — all lossy profiles in practice; lossless
  encoder config is an open issue ([w3c/webcodecs#258](https://github.com/w3c/webcodecs/issues/258)).
- 4:4:4 (VP9 profile 1, AV1 high profile) is software-only decode where present;
  AV1 decode on macOS/iOS Safari ≈ 24 % / 33 % of sessions
  ([WebCodecs Fundamentals dataset](https://webcodecsfundamentals.org/datasets/codec-analysis-2026/)).
- **Transparent video on Safari** (July 2026; see
  [jakearchibald.com/2024/video-with-transparency](https://jakearchibald.com/2024/video-with-transparency/)):
  Safari does NOT composite VP9-alpha WebM, but it DOES support Apple's "HEVC Video with
  Alpha" since Safari 13 — alpha as an auxiliary layer in the HEVC track, MP4/MOV, codec
  tag **`hvc1`** (`hev1` fails). Caveats: encoding only through Apple VideoToolbox
  (`ffmpeg -c:v hevc_videotoolbox -alpha_quality 0.1 -tag:v hvc1` + REQUIRED
  `-vf "premultiply=inplace=1"`; macOS-only — x265 cannot encode alpha), Chrome/Firefox
  ignore HEVC alpha (dual-source needed: hvc1 for Safari, VP9-alpha WebM elsewhere,
  itself buggy on Android), files are HEAVY (Jake's sample: 3.4 MB native HEVC vs
  1.1 MB VP9-alpha), and `canPlayType` cannot confirm the ALPHA capability — detect by
  drawing to a canvas and sampling a known-transparent pixel.
  **Jake's conclusion = this project's design**: skip native alpha, composite yourself.
  His refinement worth stealing if a fg/overlay stream ever returns: STACK color (top) +
  grayscale alpha (bottom) in ONE video (`vstack`) instead of our old two-video
  rgb+matte — perfect sync by construction (no frame-lock needed), one decode, plain
  yuv420p everywhere, and stacked AV1 hit 460 kB vs 1.1 MB VP9-alpha (hardware AV1:
  iPhone 15 Pro+/M3+; stacked HEVC fallback 1.14 MB). He ships the `stacked-alpha-video`
  npm web component. Gotcha: limited-range YUV banding — use 10-bit (`yuv420p10le`) to
  mitigate.
- VP9/AV1 WebM alpha channel: supported for transparency video in Chromium/Firefox.
