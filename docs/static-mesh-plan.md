# Static export v2 — mesh + UV light atlas (planning)

Status: **planning**. Converge the static wallpaper export onto the same screen-space mesh
format the animated export uses (one mesh key, K=1), replacing the per-part UV images.
Background and rationale: [animated-export-plan.md](animated-export-plan.md) ("Future
discussion: the mesh stream as a STATIC format too").

The animated mesh pipeline is validated end-to-end, so static-v2 is mostly **"the animated
export with K=1 + a UV light atlas + still images instead of videos"**. The one genuinely
new component is the **UV light atlas bake**.

## Deliverables (`<OUT_DIR>/static/` or reuse the player folders)

```
mesh.bin                 NSKM — welded UV, src (welded→unique), tris (DENSE mesh, see below)
positions.bin            one key of screen positions + z (NSKA with K=1, absolute only)
<player>_light.png       UV-space light atlas per player (sampled by uv like the skin)
background.png           scene without players (shadows baked in)
foreground.png + _matte  scenery in front of the players (RGB + grayscale matte)
manifest.json            static_version, resolution, crop, per-player atlas + tri ranges
```

Same WebGL renderer as the animated mode, with K=1 (no interpolation) and the light sampled
in **UV space** instead of screen space (see the shader note).

## The UV light atlas (the new piece)

A per-player texture in the skin's UV layout storing the scene lighting on a 0.5-gray
Lambertian body. The browser relights any skin with `lit = skin(uv) · atlas(uv) · 2` — the
same values as today's `<part>_light.jpg`, but indexed by **uv**, so each Minecraft part's
own UV island carries its own light. A skin that reveals an occluded part (empty hat over a
painted jacket) samples the **jacket's** light, not the hat's — correct by construction, no
base/full screen-space ambiguity.

### Bake (Cycles `bpy.ops.object.bake`)

Per player, into one atlas image (e.g. 512×512, sRGB — higher than the 64px skin for a
smooth gradient; the skin samples nearest, the atlas linear):

1. Cycles engine; the parts already have the skin UV map.
2. Swap every part to the **gray-0.5 diffuse** material (`_gray_diffuse_material`) with an
   **Image Texture node = the atlas**, selected/active (the bake target). `use_clear=False`
   so parts accumulate into their UV regions; set a `bake.margin` for edge bleed.
3. **Base parts** (head/body/arms/legs): bake with the **overlays hidden** (base-only), so
   the visible base (when a skin leaves an overlay empty) has no phantom overlay shadow.
4. **Overlay parts** (hat/jacket/sleeves/pants): bake with the **full** player visible.
5. Bake `COMBINED` (or `DIFFUSE` direct+indirect) — the lit gray, the same quantity as the
   screen-space light. Save the atlas (PNG/WebP, sRGB).

The base/full split survives here, but only as **which scene state to bake each region
with** (the self-shadow concern) — NOT the screen-space reveal ambiguity, which UV space
removes entirely.

Risks/notes: bake setup is fiddly (active image node per material, margin, clear=False);
self-shadow detail comes for free (other parts/scenery cast in the bake); verify the
factor so `skin · atlas · 2` matches a reference Cycles render of the textured player.

## Mesh + positions

- Reuse `_anim_collect_static` (mesh.bin) and `_anim_frame_positions` at the **current
  frame only** (K=1). positions.bin = one absolute key (NSKA reconstruction degenerates to
  "use key 0"); or a tiny dedicated blob.
- Use the **DENSE** mesh (Subdivision viewport on, ~34k verts ≈ 136 KB once) — render-
  matching silhouettes. The AntiLag reduction was only to fit the per-frame anim.bin delta
  budget, which K=1 doesn't have.
- Crop, foreground/matte, background: same as the animated mode but single still images.

## Shader (unified renderer)

```glsl
// per player, depth-tested, painter's order:
color = skin(uv) * light(uv_or_screen) * 2.0;   // static: atlas(uv); animated: lightVideo(screenUv)
```
A manifest flag (`light_space: "uv" | "screen"`) tells the renderer which sampler to use.
Background → players (depth test) → foreground (over, via matte). Identical to the animated
path with K=1.

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

## Open questions

- Atlas resolution (256 vs 512) and encoding (sRGB PNG vs lossless WebP) — tune vs size.
- Exact bake pass/factor so `skin · atlas · 2` matches a reference render.
- One atlas per player vs a shared atlas with per-player regions (per-player is simpler).
- Whether static keeps the legacy `mask`/`bbox`/`base_layer` outputs for tools that still
  read them, or drops them once the web tool is ported.
- positions.bin format: reuse NSKA (K=1) for one consumer, or a dedicated static blob.
