# NovaSkin Export

A Blender add-on that exports a scene built with the **Thomas Rig Legacy** Minecraft rig
so the [NovaSkin wallpaper tool](https://minecraft.novaskin.me/wallpapers/tools/blender/)
can re-texture the players with **any skin, directly in the browser** — without
re-rendering. One click renders everything the web tool needs: the background, each
player's data layers (UV/mask/light/shadow), optional toggleable scenery layers, and an
experimental animated mode.

<p align="center">
  <img src="blender-panel.png" alt="NovaSkin Export panel in the 3D Viewport sidebar"
       width="300">
</p>

---

## What you need

- **Blender 4.2+ with a GUI** (5.1 recommended). It does **not** run with `--background`.
- The **[Thomas Rig Legacy](https://extensions.blender.org/add-ons/thomas-rig-legacy/)**
  rig in your scene (1 or more players). Install/append it separately — the panel shows
  how many players it detects, and links to the rig if none is found.
- An **active camera** and some lights/scenery.
- A **saved `.blend`** (the output goes to a `novaskin/` folder next to it).

If something is missing, the export stops right away with a clear message instead of
failing midway.

## Install

- **Extension zip (recommended):** download `novaskin_export-<version>.zip` from the
  [releases](https://github.com/novaskin/novaskin-blender-addon/releases) and **drag it
  onto the Blender window** (or `Edit > Preferences > Get Extensions > ⌄ > Install from
  Disk…`).
- **Classic add-on:** `Edit > Preferences > Add-ons > Install…` → pick
  `render_uv_mask.py`.

Then enable **“NovaSkin Export”**. Reinstall the same way to update (Install from Disk
copies the file — editing the original does not update it).

## How to use

Open the **3D Viewport sidebar** (<kbd>N</kbd>) → **NovaSkin** tab:

1. Check the **Rig** section — it lists the players detected in the scene.
2. Hit **Render Draft (fast preview)** first: a quick 50 %-resolution pass to check
   framing and your marked layers.
3. Hit **Render for NovaSkin** for the real export.
4. Use **Open Output Folder** to grab the results, and the **Open Wallpaper Tool**
   button to jump to the web tool.

Everything also lives in the top bar under **Render → Render for NovaSkin**, and
scripts can call `bpy.ops.render.novaskin()`.

While it runs you get a progress bar in the panel (plus the viewport header and status
bar). Cancel any time with the **Cancel** button or <kbd>Esc</kbd> — the scene is always
restored to exactly how it was, even on cancel or error. The UI stays responsive between
steps, but each render step still blocks briefly (a Blender limitation).

### Panel options

- **Output** — destination folder and the UV file format (`PNG` default; `WebP` is
  lossless and ~60 % smaller; `EXR` for float pipelines).
- **Layers** — which data layers to export: back-face UVs, per-player light (*Illum*),
  cast *Shadow*, the *Base Layer Composite*, and the *Background* image.
- **Quality** — light/shadow render samples, and the light/shadow file format
  (JPEG/WebP/PNG + quality).
- **Rig** — *Fix Hat Position and Scale* snaps the rig's hat onto the head at the
  Minecraft proportions before exporting. The rig's own "Second layer" toggle does
  **not** matter: overlays (jacket/sleeves/pants) are always exported.

### Optional layers

Mark scenery you want as an **independent toggle in the wallpaper** (a mob, a tree, a
build) with the **“Mark as Optional Layer”** button:

- with objects **selected** — a selected armature (or any mesh of a rig) marks the
  **whole rig** as one layer; a plain mesh marks just itself;
- with **nothing selected** — marks the **active collection** (all its meshes become one
  layer). The panel shows which collection is active.

Marked layers are listed in the panel with an **✕** to remove them. Player rigs are
refused. Each layer is rendered by itself (correctly occluded by the scenery) plus the
shadow it casts, so the web tool can toggle it on/off.

### Animated export (beta)

The **Animated (beta)** section exports the scene's frame range as an animated wallpaper:
three WebM videos plus the players' animated geometry (~1 MB for 15 s), re-textured live
in the browser. Base layer only, classic arms; optional-layer marks are ignored (those
objects render as part of the scenery). Encoding needs `ffmpeg` installed — without it
the PNG sequences and an `encode.sh` script are left for you to run. Start with **Export
Animation Draft** to preview. ⚠️ A full export renders 3 passes per frame — expect it to
take a while.

## What you get

A `novaskin/` folder next to your `.blend`:

- `background.png` — the scene without players;
- one folder per player (`player1/`, `player2/`, …) with its UV maps, masks, light and
  shadow images in both arm variants (classic/slim);
- `layers/` — your optional layers;
- `manifest.json` — everything the web tool needs to put it together;
- `animated/` — the animated export, when used.

Load that folder in the wallpaper tool and swap skins freely.

> **Integrating the output yourself?** All file formats, channel layouts and the
> compositing math are documented in [docs/output-format.md](docs/output-format.md).
> The animated pipeline design lives in
> [docs/animated-export-plan.md](docs/animated-export-plan.md), with a reference web
> player in [prototype/](prototype/).

## For developers

- The whole add-on is a single file, [render_uv_mask.py](render_uv_mask.py); defaults
  live in its `CONFIG` block (the panel overrides the common ones).
- For heavy iteration, symlink the repo file into Blender's add-ons folder and use
  **F3 → Reload Scripts** after editing; or open it in the *Scripting* workspace and run
  it directly.
- `python3 build_addon.py` builds the extension zip; tags `v*` publish a release via CI.
