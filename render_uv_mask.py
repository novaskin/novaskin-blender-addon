"""Export per-part UV + (occlusion) mask per player, for N players in the scene.

Outputs (one subfolder per player):
  - <OUT_DIR>/<armature>/<part>_UV.png        (PNG, RAW/Non-Color; 8-bit by default)
        R=U, G=V, B=depth(per character), A=coverage (1 inside the part, 0 outside).
        As EXR (UV_FORMAT='OPEN_EXR') with the illum pass on, it is "<part>_UVDL.exr" with
        the same RGBA plus a "light" layer (light.R/G/B = sRGB illum color).
        <part> is a Minecraft-style label (head/hat, body/jacket, arm/sleeve, leg/pant,
        with _left/_right and _classic/_slim where relevant). See MC_PART_MAP; the manifest
        records the label -> object-name mapping.
  - <OUT_DIR>/<armature>/<part>_light.jpg     (per-part light, PNG pipeline; base parts use
        the base-only render, overlays the full render. EXR packs it in the 'light' layer
        instead. See EXPORT_PART_LIGHT.)
  - <OUT_DIR>/<armature>/base_layer_<classic|slim>.png  (base parts composited per arm
        variant, nearest pixel wins; + base_layer_<v>_light.jpg in the PNG pipeline)
  - <OUT_DIR>/<armature>/mask_<classic|slim>.png
  - <OUT_DIR>/background.png                  (scene without players/optional layers)
  - <OUT_DIR>/layers/<name>.png (+ _shadow/_UV/_light)  (optional layers: scenery objects
        marked via the panel's "Mark Selected as Layer"; each exported independently)

Multi-player
------------
  - Each "Mesh[.NNN]" collection (child of "Rig [only append this][.NNN]") is a player.
    Discovery is automatic: adding a 3rd rig just works.
  - Blender's duplication suffix is NOT uniform (e.g. L.alex_arm -> L.alex_arm.003),
    so parts are selected by visibility logic (not by exact name), which is robust.
  - Per-player mask via Object Index: the player gets pass_index=1 and everything else
    (scenery + OTHER players) stays 0 -> occlusion by scenery and by other players is
    handled automatically.

How it works
------------
  - Everything is read via the VIEWER node (bpy.data.images['Viewer Node']) and saved by
    us. The existing File Output is NOT modified (only MUTED during renders).
    >>> Requires Blender with a GUI. Does not run in --background. <<<
  - UV: isolates the part by muting the hide_render drivers; samples=1 (no AA -> raw coords).
  - Mask: scene in its normal state (drivers active); Cycles (EEVEE does not fill the
    Object Index pass); samples=1; configurable resolution.
  - Before exporting, the head's outer "hat" layer (2_Layer_Extrusion), which the rig parks
    above the head (and sizes 1:1 with it), is snapped onto NoFace_Head and scaled to the
    Minecraft hat size (1.125x the head). Persistent + idempotent; see FIX_2LAYER_POSITION /
    HAT_SCALE_RATIO.

Requirements (checked at startup by _preflight, which aborts with a clear message):
  - Tailored to the Thomas_Rig_Legacy rig (see the CONFIG block below).
  - Run with a GUI (NOT --background) -- reads the Viewer node.
  - The .blend must be saved (OUT_DIR uses '//', relative to the .blend).
  - The scene must have an active camera, plus lights/scenery for the illum steps.
  - The player rig(s) must carry the Rig_ID custom property.

Usage
-----
  - As an add-on: Edit > Preferences > Add-ons > Install... -> pick this file -> enable.
  - 3D Viewport sidebar (press N) -> "NovaSkin" tab: options + a "Render for NovaSkin"
    button. The panel's options override the CONFIG constants below at run time.
  - Top bar: Render > Render for NovaSkin.
  - Python console / another script:  bpy.ops.render.novaskin()
Outputs go to <blend_dir>/novaskin/.
"""

bl_info = {
    "name": "NovaSkin Export (Thomas_Rig_Legacy)",
    "author": "saviski",
    "version": (1, 2, 2),
    "blender": (4, 2, 0),
    "location": "Top bar > Render > Render for NovaSkin",
    "description": "Export per-part UV + occlusion mask + illum/shadow per player.",
    "category": "Render",
}

import bpy
import os
import re
import json
import numpy as np
from bpy.props import (BoolProperty, IntProperty, EnumProperty,
                       StringProperty, PointerProperty)

# ----------------------------- CONFIG -----------------------------
# Add-on version stamped into manifest.json ("addon_version"). Keep in sync with bl_info
# and blender_manifest.toml -- bl_info must stay a literal (Blender parses it via ast) and
# is STRIPPED from the extension zip, so the manifest can't read it at run time.
ADDON_VERSION = "1.2.2"
# Version of the manifest.json FORMAT (bump when keys/semantics change, so the web tool
# can branch). Absent in manifests written before this key existed.
MANIFEST_VERSION = 1

OUT_DIR = "//novaskin/"

# Per-player output subfolder name: 'index' -> player1, player2, ... (PLAYER_FOLDER_PREFIX,
# in armature-name sort order); 'armature' -> the armature object name (Thomas_rig, ...).
# The manifest records both the folder label and the armature name.
PLAYER_FOLDER_SCHEME = 'index'      # 'index' or 'armature'
PLAYER_FOLDER_PREFIX = 'player'

UV_SAMPLES = 1
MASK_SAMPLES = 1
# The Object Index pass is NOT averaged over samples (it stays the integer index), so the static-v2
# occlusion mask is binary. The dense lattice-deformed meshes leave thin sub-pixel index-0 slivers
# inside the silhouette (visible as cracks when the player draws over the bg); a morphological CLOSE
# on the mask fills them. The browser folds the (linear-sampled) mask into alpha for a soft edge.
STATIC_MASK_SAMPLES = 1
MASK_RES_PCT = 100
PLAYER_INDEX = 1

# Mask: 2 versions per player depending on the arm.
# The real control is the master Main_Properties["Slim main"] (False=Steve, True=Alex/slim);
# the per-arm "Steve/Alex" prop is DRIVEN by it, so we set the master.
# (file_suffix, value of "Slim main") -- "classic" = Steve arms, "slim" = Alex arms.
MASK_ARM_VARIANTS = [("classic", False), ("slim", True)]
SLIM_CONTROL = ("Main_Properties", "Slim main")

# Fingers/3x3 properties to force OFF when selecting parts (we want the basic arms,
# not the fingers/3x3 variants -- which on alex have no "fingers" in the name, only a
# .001/.002 suffix). Forcing them off + reading visibility isolates the basic ones.
SELECTION_FORCE_OFF = [
    ("Main_Properties", "3x3"),
    ("Main_Properties", "Finger main"),
    ("Main_Properties", "Finger+ main"),
    ("R.Arm_Properties", "R.Arm_Fingers"),
    ("R.Arm_Properties", "R.Arm_Fingers+"),
    ("L.Arm_Properties", "L.Arm_Fingers"),
    ("L.Arm_Properties", "L.Arm_ Fingers+"),
]
# Pose-bone toggles to force ON (truthy) so the parts they reveal are ALWAYS exported, even
# when the artist left the toggle OFF in the rig UI. The "Second layer" toggle hides the
# overlay meshes (jacket/sleeves/pants 2nd layer) via hide_render DRIVERS; with it off those
# parts aren't selected and never exported (and reconciliation can't recover them -- they are
# driver-hidden, not manually). Forced during part selection AND the whole render, then the
# artist's original value is restored. Empty list = export exactly what is visible.
SELECTION_FORCE_ON = [
    ("Main_Properties", "Second layer"),
]
# Opaque material override during the mask (the skin has alpha -> it would punch holes
# in the mask). Object Index ignores color; it only needs to be opaque. Gray ~#808080.
MASK_OVERRIDE_RGBA = (0.5, 0.5, 0.5, 1.0)

# Also export the UV of the BACK FACES (a material that only renders back faces):
# produces <part>_UV_back.png in addition to <part>_UV.png (front faces).
EXPORT_BACKFACE_UV = True

# Channels of the exported UV: R=U, G=V always. If True, B = DEPTH normalized (0..1)
# by the CHARACTER's depth range (camera Z); otherwise B stays 1.0.
UV_DEPTH_IN_BLUE = True

# Per-PLAYER illum + shadow (renders 1 player in gray at a time, others hidden, per arm
# variant). ILLUM = the per-part light + the full-body illum image; SHADOW = the shadow the
# player casts on the scenery (ratio vs the clean scene). Independent toggles.
EXPORT_ILLUM = True
EXPORT_SHADOW = True
# Compute the shadow ratio in DISPLAY space (view-transformed, like background.png)
# instead of linear, so multiplying it onto the (display) background in a 2D/sRGB canvas
# reproduces the render -- a LINEAR ratio multiplied in display space over-darkens. True for
# any view transform (AgX/Standard/...). Set False if you composite in linear.
SHADOW_DISPLAY_RATIO = True
# Also save the per-part light as its own image next to the UV ("<part>_light.<ext>", the
# illum/shadow format). Needed for the PNG pipeline (the light can't go in the UV alpha --
# canvas premultiply). With EXR the light is embedded in the UV's 'light' layer instead.
EXPORT_PART_LIGHT = True
# For the LIGHT renders, make the scenery invisible to the camera (visible_camera=False) so
# objects IN FRONT of the player don't bleed their color into its light -- the player renders
# unoccluded while the scenery still shadows/bounces light onto it (lighting stays real). The
# shadow render keeps the scenery visible (the shadow falls on it).
ILLUM_HIDE_SCENERY_FROM_CAMERA = True
EXPORT_ILLUM_BACKGROUND = False   # global (all together) -- superseded by the per-player one
ILLUM_SAMPLES = 48
ILLUM_RES_PCT = 100               # must match MASK_RES_PCT to align with the mask
ILLUM_GRAY_RGBA = (0.5, 0.5, 0.5, 1.0)
# If True: visible_diffuse=False on the player -> 100% pure shadow, but the illum loses
# the self-bounce (less detail in the crevices). Default False: keeps the self-bounce
# (more detailed illum); the shadow stays fine because clip(ratio,0,1) already removes the
# player's bounce on the scenery (ratio>1 -> 1).
ILLUM_PURE_SHADOW = False
# Illum PNG encoding: 'sRGB' (display look, for compositing in sRGB) or 'Non-Color'
# (raw linear). The shadow is always Non-Color (it is a multiply factor).
ILLUM_COLORSPACE = 'sRGB'

# Render of the FULL scene WITHOUT the players and optional layers -> background.png.
# Caveat: it also hides the shadows they would cast (those come from the shadow maps).
EXPORT_BACKGROUND = True
# The background is the final beauty image (the real scenery). Use the engine/samples/denoise
# the USER set in Blender (from the _Session snapshot) instead of the illum's gray-render
# settings. False = use ILLUM_SAMPLES/CYCLES like the data passes. (Resolution stays at the
# export res so it aligns with the shadow maps.)
BACKGROUND_USE_SCENE_SETTINGS = True

# OPTIONAL LAYERS: scenery marked by the user (panel "Mark Selected as Layer" / "Mark Active
# Collection", which toggle the LAYER_ID_PROP custom property) are exported as independent
# toggleable layers in <OUT_DIR>/layers/. The marker can sit on a COLLECTION (all its meshes
# = one layer), an ARMATURE (the rig's meshes = one layer) or a standalone MESH -- a group's
# meshes render TOGETHER (self-occluding), so a rigged object is one whole layer. Each group
# exports: the meshes rendered alone over a transparent background ("<name>.png", real
# materials, display-encoded like background.png, OCCLUDED by the scenery), the shadow they
# cast ("<name>_shadow.<ext>", display multiply), and -- single-mesh groups only, if
# EXPORT_LAYER_UV -- its UV + light ("<name>_UV.png" + "<name>_light.<ext>") for retexturing.
# Marked meshes are HIDDEN from the background and the whole player pipeline, so the player
# light is independent of the layer toggles (trade-off: a layer's shadow never falls on a
# player).
LAYER_ID_PROP = "novaskin_layer"
EXPORT_LAYER_UV = True

# Player detection: armatures that have the Rig_ID custom property.
# RIG_ID_VALUE filters by value (None = accept any armature with Rig_ID).
RIG_ID_PROP = "Rig_ID"
RIG_ID_VALUE = "Thomas_Rig_Legacy"
# Inside the rig container, the parts collection starts with this prefix.
MESH_COLLECTION_PREFIX = "Mesh"                  # "Mesh", "Mesh.001", ...

# The rig parks the head's outer "hat" layer (2_Layer_Extrusion) ABOVE the head by default
# (so the artist can edit it apart), AND leaves it the same size as the head. Before export
# we snap it onto NoFace_Head and scale it to the Minecraft hat size: the hat is the head
# inflated 0.5px (an 8px cube -> 9px), i.e. 9/8 = 1.125x the head. Persistent + idempotent.
FIX_2LAYER_POSITION = True
HAT_SCALE_RATIO = 1.125            # hat size relative to the head; None = keep the rig's scale
LAYER2_NAME = "2_Layer_Extrusion"
LAYER2_HEAD_NAME = "NoFace_Head"

# Standardize the per-part UV filenames to Minecraft skin semantics
# (head/hat, body/jacket, arm/sleeve, leg/pant). Keys are the object's base name (the
# Blender ".NNN" duplicate suffix stripped) in lowercase. Parts not in the map fall back
# to a sanitized version of their object name. If two parts resolve to the same label
# within a player (e.g. duplicate meshes), the later ones get a "_2", "_3"... suffix so no
# file overwrites another. Set RENAME_UV_PARTS=False to keep the raw object names.
RENAME_UV_PARTS = True
MC_PART_MAP = {
    "noface_head":          "head",
    "2_layer_extrusion":    "hat",
    "body":                 "body",
    "body_secondlayer":     "jacket",
    "l.leg":                "leg_left",
    "r.leg":                "leg_right",
    "r.leg_2ndlayer":       "pant_right",
    "leggings":             "pant_left",
    "l.steve_arm":          "arm_left_classic",
    "r.steve_arm":          "arm_right_classic",
    "l.steve_arm_2ndlayer": "sleeve_left_classic",
    "r.steve_arm_2ndlayer": "sleeve_right_classic",
    "l.alex_arm":           "arm_left_slim",
    "r.alex_arm":           "arm_right_slim",
    "l.alex_arm_2ndlayer":  "sleeve_left_slim",
    "r.alex_arm_2ndlayer":  "sleeve_right_slim",
}

# Output formats / bit depth.
# Mask is saved as PNG. 8-bit (256 levels) is enough for Minecraft skins (64x64 and even
# HD 256x256); the depth packed in the UV's B channel only needs to RANK parts within a
# single player, so 256 levels are plenty. Set to 16 for full-precision PNGs.
PNG_BIT_DEPTH = 8                  # 8 or 16
# PNG zlib compression 0..100. Blender's img.save() default (~15) barely compresses, so a
# mostly-transparent frame still costs ~36 KB; 90 cuts data PNGs ~70% (byte-exact). Saved
# via save_render (img.save ignores this). Only applies to data (Non-Color) PNGs.
PNG_COMPRESSION = 90
# UV + base_layer format: 'PNG' (8/16-bit), 'WEBP' (8-bit LOSSLESS -- quality=100 switches
# libwebp to lossless mode; ~60% smaller than PNG, browser-decodable; verified byte-exact
# inside the coverage, the alpha-0 RGB is premultiply-zeroed on save but never read) or
# 'OPEN_EXR' (float). EXR avoids the 2D-canvas premultiply problem (when alpha carries the
# light) AND removes 8-bit quantization, but the files are bigger and need a float EXR
# loader to read (browsers can't decode EXR via canvas).
UV_FORMAT = 'PNG'                  # 'PNG', 'WEBP' or 'OPEN_EXR'
EXR_HALF = True                    # half-float (16-bit) EXR -> smaller; else 32-bit float
EXR_CODEC = 'ZIP'                  # lossless: ZIP/ZIPS/PIZ/PXR24/RLE/NONE; lossy: DWAA/DWAB/B44A
UV_EXT = {'OPEN_EXR': '.exr', 'WEBP': '.webp'}.get(UV_FORMAT, '.png')
# 8-bit PNG: quantize U/V with FLOOR into texel bins (byte = texel index) instead of the
# default round-to-nearest, so a value inside texel i stays in texel i (round pushes the
# upper half of a texel into the next one -- an off-by-one at HD 256px, where 8-bit = exactly
# one level per texel). UV_TEXEL_BINS = bin count; 256 covers 64/128/256 skins (they all
# divide 256). EXR keeps the raw float (the consumer floors the exact value).
UV_PNG_FLOOR_TEXELS = True
UV_TEXEL_BINS = 256
# Illum (light) + shadow do not need PNG; JPEG/WebP keep the files much smaller (WebP is
# ~30% smaller than JPEG at the same quality and every browser decodes it).
LIGHTSHADOW_FORMAT = 'JPEG'        # 'JPEG', 'WEBP' or 'PNG'
JPEG_QUALITY = 90                  # 0..100 (JPEG/WebP only)
LIGHTSHADOW_EXT = {'JPEG': '.jpg', 'WEBP': '.webp'}.get(LIGHTSHADOW_FORMAT, '.png')

# Draft mode (panel toggle): renders everything at 50% resolution with few samples, for
# fast iteration on framing/marking before the real export. The manifest records it.
DRAFT_MODE = False
DRAFT_RES_PCT = 50
DRAFT_SAMPLES = 8

# The default RGBA of every UV file is (R=U, G=V, B=depth, A=coverage). When exporting as
# EXR (UV_FORMAT='OPEN_EXR') and the illum pass is on, the RGB light is ALSO embedded in the
# same file as an extra "light" layer (light.R/G/B, sRGB) -> "<part>_UVDL.exr". PNG never
# carries the light (4 channels max + the 2D-canvas premultiply issue); its light is the
# separate illum_*.jpg.

# After a player's parts are exported, composite the BASE layers (no overlays, no back
# faces) into one UVDL image PER arm variant, nearest pixel (smallest depth) winning. All
# parts share the Minecraft skin UV space, so this maps each screen pixel -> skin UV +
# depth + light. "{variant}" is replaced by each MASK_ARM_VARIANTS name (classic/slim).
COMPOSITE_BASE_LAYER = True
COMPOSITE_OUTPUT_NAME = "base_layer_{variant}"   # extension added from UV_EXT
COMPOSITE_BASE_LABELS = ["head", "body",
                         "arm_left_{variant}", "arm_right_{variant}",
                         "leg_left", "leg_right"]

# ---- ANIMATED EXPORT (simplified mode -- see docs/animated-export-plan.md) ----
# Exports <OUT_DIR>/animated/: three lossy videos (background with baked player shadows,
# per-pixel foreground occlusion layer, combined player light) + the player base-layer
# geometry as a screen-space mesh stream (mesh.bin static + anim.bin per-frame positions).
# Base layer only, no per-player toggles, players always drawn; optional-layer marks are
# IGNORED here (marked objects render as plain scenery). Frame range = the scene's.
ANIM_OUT_SUBDIR = "animated"
ANIM_KEYS_STEP = 2          # store mesh keys every Nth video frame (24fps video -> 12fps keys)
ANIM_QUANT = 8.0            # vertex x/y quantization: 1/8 px (int16)
ANIM_Z_BITS = 12            # depth quantization: 12 bits = 4095 levels (plenty for the depth
                            # test; smaller than the x/y range -> better delta compression)
# The 'Export Animation Draft' button renders only the first few seconds (fast iteration on
# look/quality) instead of the whole frame range. The full export uses the scene's range.
ANIM_DRAFT_SECONDS = 3
ANIM_CRF = {"bg": 32, "fg": 32, "light": 38}   # VP9 quality per stream
ANIM_ENCODE = True          # run ffmpeg after rendering (else keep PNG sequences + script)
ANIM_KEEP_SEQUENCES = False  # keep seq/ PNGs after a successful encode
# Resume an interrupted export: skip frames whose PNGs already exist in seq/. A crash or
# cancel leaves the rendered frames (seq/ is only deleted after a SUCCESSFUL encode), so
# re-running the export continues from where it stopped. Delete seq/ to force a fresh run.
ANIM_RESUME = True
ANIM_BASE_LABELS = ["head", "body", "arm_left_classic", "arm_right_classic",
                    "leg_left", "leg_right"]
# Dilate the light outward this many pixels beyond the player silhouettes before encoding.
# The light is ONLY sampled by the player fragments (it is never composited over the
# background), but mesh edges can sample 1-3 px outside the rendered silhouette (12fps key
# lerp vs 24fps video + VP9 edge blur) -- padding prevents black fringes. 0 disables.
ANIM_LIGHT_PAD = 8
# Crop the foreground + light videos to the players' screen region (union over all frames +
# this padding). Both only have content where the players are, so the rest of the frame is
# wasted bytes (foreground is mostly transparent yet nearly as big as the background). The
# background is NOT cropped (it is the whole wallpaper). The manifest records the crop rect;
# the web player positions the foreground quad and offsets the light sampling. 0 disables.
ANIM_CROP_PAD = 24

# ---- Static export v2 (mesh K=1 + UV light atlas) -------------------------------------
# A per-player light atlas in the skin's UV layout: each texel stores the scene lighting on a
# 0.5-gray Lambertian body, so the browser relights any skin per-pixel with
# `lit = skin(uv) * atlas(uv) * 2`. Replaces the screen-space per-part `_light` images with one
# UV-space texture per player. Baked in a single Cycles pass (Option B): overlay parts are made
# non-occluding via ray visibility so the base regions are lit overlay-free (a skin that leaves
# an overlay empty samples the base's own light, not a phantom shadow under it). See
# docs/static-mesh-plan.md. The atlas stores display-encoded light (`_to_display`), saved with
# the panel Quality "Illum/Shadow" format/quality -- same model as the screen-space `_light`.
# The grid in the bake was the rig's UV map: each skin-pixel face is INSET to ~50% of its 1/64
# cell (an anti-bleed trick), so the gaps between faces baked as a grid (and HD skins lost detail).
# STATIC_EXPAND_PIXEL_UVS scales each face's UVs out to fill its cell -> no gaps -> the atlas can run
# at high resolution again WITHOUT a grid, capturing sub-pixel cast-shadow detail, and 512px skins
# work. (With the expand off, keep ATLAS_RES low (~64) so the gaps average away.)
STATIC_EXPAND_PIXEL_UVS = True   # expand the rig's inset per-pixel UVs to fill their skin-pixel cells
STATIC_SKIN_PX = 64              # Minecraft skin resolution -> UV cell pitch = 1/64
# Atlas resolution (panel: 64/128/256/512/1024). With the UV-expand on, higher = sharper baked light
# and shadows on the characters, with NO grid. Default 512 (a good balance). _apply_settings overrides
# it from the panel.
ATLAS_RES = 512
ATLAS_BAKE_SAMPLES = None   # Cycles samples for the bake (denoised). None = follow the panel
                            # "Illum samples" (ILLUM_SAMPLES, live); set a fixed int to override.
ATLAS_BAKE_MARGIN = 4      # UV island edge bleed (px)
ATLAS_OVERLAY_LABELS = ("hat", "jacket", "sleeve", "pant")   # 2nd-layer parts -> non-occluding
# The atlas is baked SMOOTH-shaded -- exactly like the viewport / screen-space light -- for soft,
# realistic shadows. (Flat shading facets the rig's per-pixel quads into a blocky grid, which looked
# worse than the screen render; smooth keeps the gradients.) Encode the atlas LOSSLESS: a lossy
# JPEG's 8x8 DCT blocks land exactly on the skin pixels (512/64 = 8) and amplify the per-pixel
# relief into a visible grid -- lossless WebP avoids that and is still tiny (the light is low-freq).
ATLAS_BAKE_FLAT = False    # True = flat per-face bake (faceted, faster); False = smooth (recommended)
ATLAS_FORMAT = 'WEBP'      # atlas image format ('WEBP' or 'PNG') -- lossless, NOT the panel's lossy
ATLAS_LOSSLESS = True      # JPEG/lossy block boundaries align with skin pixels -> grid; keep lossless
ATLAS_EXT = {'WEBP': '.webp', 'PNG': '.png'}.get(ATLAS_FORMAT, '.png')
# Optional residual low-pass (atlas px; 8 px = 1 skin texel at 512/64). 0 = off; raise only if the
# per-pixel relief still shows after smooth+lossless.
ATLAS_BLUR_RADIUS = 0
ATLAS_BLUR_PASSES = 2      # box passes (2-3 ~ Gaussian)
STATIC_OUT_SUBDIR = "static"   # self-contained static-v2 export dir (alongside the legacy one)
# Static lighting space:
#   "uv"     = per-player UV light atlas (`_bake_player_light_atlas`). ONE texture per player, baked
#              in one step, covering every region -- and overlay-reveal correct (Option B). With
#              smooth shading + lossless encoding it matches the screen render closely. Default.
#   "screen" = one screen-space light image (players rendered gray at screen resolution, like the
#              old per-part export and the animated mode). Sampled by screen position. Use this if
#              the atlas ever looks off on a different rig.
STATIC_LIGHT_SPACE = "uv"
# Per-step toggles for the static export (mirrored from the panel by _apply_settings). A step turned
# OFF is skipped and its assets are reused from the previous export's manifest. Defaults here keep
# scripted/headless exports doing the full run.
STATIC_DO_ATLAS = STATIC_DO_BACKGROUND = STATIC_DO_FOREGROUND = STATIC_DO_SHADOWS = True
STATIC_DO_SPRITES = STATIC_DO_MASKS = True
# the tint (transmissive surface in front of the players) is gated by the Foreground toggle --
# both are "what is in front of the player" (opaque foreground composite + transmissive tint multiply)
# Static background + foreground images. The foreground is ALWAYS WebP because it needs a real
# alpha channel (the scenery in front of the players over transparency); the matte split is a
# VIDEO-only workaround (no in-browser video codec carries alpha on Safari) -- a still image
# carries alpha natively, so static needs no matte. The background is opaque (full wallpaper).
STATIC_BG_FORMAT = 'WEBP'      # 'WEBP', 'JPEG' or 'PNG' (opaque background)
STATIC_BG_QUALITY = 90         # 0..100 (JPEG/WebP)
STATIC_BG_EXT = {'JPEG': '.jpg', 'WEBP': '.webp'}.get(STATIC_BG_FORMAT, '.png')
# Foreground alpha contract: STRAIGHT alpha, composited with rgb multiplied by alpha at sample
# time -- out = fg.rgb*fg.a + behind*(1 - fg.a). That makes the soft anti-aliased silhouette edge
# blend correctly with the player underneath (no straight/premult mismatch fringe -- the original
# bug was a premult-style `fg.rgb + behind*(1-a)` over straight data). We do NOT pre-premultiply
# into the file: WebP discards the rgb of fully transparent texels (fills them white), which would
# blow out a premult composite; multiplying by alpha at runtime makes that garbage harmless (x0).
# Lossless keeps the alpha edge crisp (lossy WebP blurs/widens it); the fg is ~99% transparent so
# lossless is still tiny.
STATIC_FG_LOSSLESS = True      # lossless WebP -> crisp alpha edge (recommended)
STATIC_FG_QUALITY = 90         # used only when STATIC_FG_LOSSLESS is False

# Per-entity shadow (toggleable multiply ratio). The ratio is the LUMINANCE darkening
# clip(lum(sh)/lum(clean), 0, 1) -- a single achromatic factor, NOT the per-channel ratio, so
# render-noise differences between the two renders never tint the unaffected scenery (the per-channel
# ratio turned low-blue foliage yellow). STATIC_SHADOW_FLOOR drops darkening below this fraction to
# 0 (= keep ratio at 1): kills the faint denoiser-residual speckle far from the entity while keeping
# real cast shadows.
STATIC_SHADOW_FLOOR = 0.06     # darkening (1-ratio) below this -> no shadow (noise floor)

# Sprite layers are baked as a tight silhouette beauty (the layer alone over transparent, occluded by
# scenery as a holdout) PLUS a separate multiply shadow ratio (the same one the mesh entities get),
# so a sprite never paints scenery over a player and there is no double-water.

# A "mesh-type" optional layer (one shared texture across its meshes) is exported like a player --
# geometry in mesh.bin + a UV light atlas + its base texture -- so it can be re-textured in the
# browser (skin(uv)*atlas(uv)*2). Layers that don't qualify (multiple textures, or an untextured
# mesh like procedural eyes) stay flat sprites. LAYER_ATLAS_RES sizes the mesh-layer light atlas
# (mobs are small, so 256 is plenty for the low-frequency light).
STATIC_LAYERS_AS_MESH = True
LAYER_ATLAS_RES = 256
LAYER_PLACEHOLDER_PX = 16   # size of the blank base texture for a bare-UV retexturable layer
                            # (no image in Blender -> textured entirely in the wallpaper tool)
# How a marked optional layer is exported (a per-layer enum on the object/collection, registered in
# register()). SPRITE (default) = a flat baked beauty image; TEXTURE/PLAYER = a re-skinnable mesh
# (geometry + UV light atlas + base texture). PLAYER additionally tags it as a character in the
# manifest, so the wallpaper tool presents it like a player. Opens room for more modes later.
LAYER_MODE_PROP = "novaskin_layer_mode"
LAYER_MODE_ITEMS = [
    ('SPRITE', "Sprite", "Flat baked image -- matches the render exactly. For background props"),
    ('TEXTURE', "Texture", "Re-skinnable 3D mesh: its own swappable texture + baked light"),
    ('PLAYER', "Player", "Like Texture, but shown as a player/character in the wallpaper tool"),
    ('OVERLAY', "Overlay", "Semi-transparent element IN FRONT of everything (rain, glare, smoke): "
                           "rendered alone over transparent and composited on top. Hidden from the "
                           "mask/geometry so it never occludes the players/layers"),
]

# Web wallpaper tool (the panel has a button that opens this URL in the browser).
WALLPAPER_TOOL_URL = "https://minecraft.novaskin.me/wallpapers/tools/blender/"
# The rig this exporter targets. Blender extensions can't declare another extension as a
# dependency (only Python wheels), so we surface this in the panel/README instead.
RIG_SOURCE_URL = "https://extensions.blender.org/add-ons/thomas-rig-legacy/"
# ------------------------------------------------------------------


def _abs(p):
    return bpy.path.abspath(p)


def _mc_part_label(name):
    """Map a Blender object name to a Minecraft-style part label (head/hat/body/jacket/
    arm/sleeve/leg/pant). Strips the ".NNN" duplicate suffix; unmapped names fall back to a
    sanitized form of the object name. Returns the raw name if RENAME_UV_PARTS is off."""
    if not RENAME_UV_PARTS:
        return name
    base = re.sub(r"\.\d+$", "", name)          # strip Blender duplicate suffix ".NNN"
    key = base.lower()
    if key in MC_PART_MAP:
        return MC_PART_MAP[key]
    return re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_").lower() or name


def _assign_part_labels(parts, label=None):
    """Return {object_name: unique_label} for a player's parts, disambiguating collisions
    (e.g. duplicate meshes) with a "_2", "_3"... suffix. Deterministic (sorted by name).
    Warns when two parts collide on the same Minecraft label -- a common sign of a stray or
    duplicate mesh (e.g. an arm sleeve weighted to the leg) that would otherwise slip in as
    a "<label>_2" file silently. The mapping still works; the warning just surfaces it."""
    used, out, first, collisions = {}, {}, {}, {}
    for o in sorted(parts, key=lambda x: x.name):
        base = _mc_part_label(o.name)
        n = used.get(base, 0) + 1
        used[base] = n
        if n == 1:
            first[base] = o.name
            lab = base
        else:
            lab = f"{base}_{n}"
            collisions.setdefault(base, [first[base]]).append(o.name)
        out[o.name] = lab
    for base, names in collisions.items():
        who = f" on {label}" if label else ""
        print(f"[WARN] label collision{who}: {len(names)} meshes map to '{base}' "
              f"-> {names}, disambiguated with _2/_3. This is often a stray/duplicate mesh "
              f"(check the rig) -- '{base}' is the first by name, not necessarily the right "
              f"one.")
    return out


def _lin_to_srgb(x):
    """Encode a linear value/array to sRGB (display) in [0, 1]."""
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1.0 / 2.4) - 0.055)


def _to_display(rgb):
    """Apply the scene's view transform (AgX/Standard/Filmic/...) to scene-linear RGB,
    giving the DISPLAY values -- the same encoding as background.png. Uses OCIO so it
    matches any view transform; falls back to sRGB. (exposure/gamma/look not applied.)"""
    out = np.ascontiguousarray(np.clip(rgb[:, :3], 0.0, None), dtype='float32')
    try:
        import PyOpenColorIO as OCIO
        cfg = OCIO.GetCurrentConfig()
        dt = OCIO.DisplayViewTransform()
        dt.setSrc(OCIO.ROLE_SCENE_LINEAR)
        dt.setDisplay(bpy.context.scene.display_settings.display_device)
        dt.setView(bpy.context.scene.view_settings.view_transform)
        cfg.getProcessor(dt).getDefaultCPUProcessor().applyRGB(out)
        return np.clip(out, 0.0, 1.0)
    except Exception as e:
        print("[shadow] OCIO view transform unavailable, using sRGB:", repr(e))
        return _lin_to_srgb(out)


def _light_for_label(light_maps, player_label, part_label):
    """Pick the per-pixel light map for a part: the right arm variant, and the BASE render
    for base parts (head/body/arm/leg) or the FULL render for overlay parts (hat/jacket/
    sleeve/pant) -- each lit where its layer is front-most. None if no light is available."""
    if not light_maps:
        return None
    if "classic" in part_label:
        vname = "classic"
    elif "slim" in part_label:
        vname = "slim"
    else:
        vname = MASK_ARM_VARIANTS[0][0]
    maps = light_maps.get((player_label, vname))
    if not maps:
        return None
    base_set = {lab.format(variant=vname) for lab in COMPOSITE_BASE_LABELS}
    return maps.get("base" if part_label in base_set else "full")


def _hide_render_drivers():
    out = []
    for o in bpy.data.objects:
        ad = o.animation_data
        if ad:
            for d in ad.drivers:
                if d.data_path == "hide_render":
                    out.append(d)
    return out


def _has_hide_render_driver(o):
    """True if hide_render is controlled by a driver (the rig's visibility logic)."""
    return bool(o.animation_data and any(
        d.data_path == "hide_render" for d in o.animation_data.drivers))


def _basename(name):
    """Object name with the Blender ".NNN" duplicate suffix stripped."""
    return re.sub(r"\.\d+$", "", name)


def _force_player_parts_visible(players):
    """Clear MANUAL (non-driven) hide_render on the players' SELECTED parts (uv_parts) so the
    masks/illum actually render them -- this only touches real export parts that the cross-rig
    reconciliation added back (body/legs a cancelled render left hidden), never helpers or
    driver-hidden parts. Must run BEFORE the _Session snapshot so it sticks across
    restore_visibility(). Returns the meshes changed, to restore at the very end."""
    forced = []
    for p in players:
        for o in p["uv_parts"]:
            if o.hide_render and not _has_hide_render_driver(o):
                o.hide_render = False
                forced.append(o)
    if forced:
        bpy.context.view_layer.update()
        print(f"[VISIBLE] force-rendered {len(forced)} manually-hidden part(s): "
              f"{[o.name for o in forced]}")
    return forced


def _force_selection_props_on(players):
    """Force the SELECTION_FORCE_ON toggles (e.g. 'Second layer') ON on every player rig so
    the overlay parts they reveal are visible in the mask/illum renders too -- not just the
    UV pass. Must run BEFORE the _Session snapshot, so the snapshot captures the overlays
    visible. Returns [(arm, bone, key, original)] to restore at the end."""
    saved, seen = [], set()
    for p in players:
        arm = p.get("arm")
        if arm is None or arm.name in seen:
            continue
        seen.add(arm.name)
        for bn, k in SELECTION_FORCE_ON:
            pb = arm.pose.bones.get(bn)
            if pb is not None and k in pb.keys() and not pb[k]:
                saved.append((arm, bn, k, pb[k]))
                pb[k] = type(pb[k])(1)
                arm.update_tag()
    if saved:
        bpy.context.view_layer.update()
        print("[2ND LAYER] forced ON for export: "
              + ", ".join(f"{a.name}['{k}']" for a, _, k, _ in saved))
    return saved


def _restore_selection_props(saved):
    """Restore the artist's toggle values forced by _force_selection_props_on; re-running the
    drivers (view_layer.update) re-hides the overlays to match the rig UI state."""
    for arm, bn, k, v in saved:
        pb = arm.pose.bones.get(bn)
        if pb is not None:
            pb[k] = v
            arm.update_tag()
    if saved:
        bpy.context.view_layer.update()


# Collections holding the rig's bone custom-shape widgets (so they can be force-hidden for the
# render even if the rig ships them with hide_render OFF). Matched case-insensitively by prefix.
BONE_SHAPE_COLLECTION_PREFIXES = ("BoneShapes", "WGTS", "Widgets")


def _hide_bone_shapes(scene):
    """Bone custom-shape WIDGETS (e.g. the rig's BoneShapes collections) sometimes ship with
    hide_render OFF. They then render as small dark curve/disc shapes at the bones (the wrist
    circles, etc.) in EVERY isolated pass -- visible as 'dirt' in the sprites and the per-entity
    shadows, at a fixed screen spot regardless of the mob. They are never part of the welded mesh,
    so hide them for the whole export. Collects every pose-bone custom_shape AND any renderable
    object that lives ONLY in a bone-shape collection. Returns the objects to un-hide afterwards."""
    widgets = set()
    for o in scene.objects:
        if o.type == 'ARMATURE' and o.pose:
            for pb in o.pose.bones:
                if pb.custom_shape is not None:
                    widgets.add(pb.custom_shape)
    pref = tuple(p.lower() for p in BONE_SHAPE_COLLECTION_PREFIXES)
    for o in scene.objects:
        colls = o.users_collection
        if colls and all(c.name.lower().startswith(pref) for c in colls):
            widgets.add(o)
    hidden = [o for o in widgets if not o.hide_render]
    for o in hidden:
        o.hide_render = True
    if hidden:
        bpy.context.view_layer.update()
    return hidden


def _restore_bone_shapes(hidden):
    for o in hidden:
        o.hide_render = False
    if hidden:
        bpy.context.view_layer.update()


def _node(node_type):
    ng = getattr(bpy.context.scene, "compositing_node_group", None)
    if not ng:
        return None
    for n in ng.nodes:
        if n.type == node_type:
            return n
    return None


def _mesh_collection_for(rig_coll):
    if not rig_coll:
        return None
    return next((ch for ch in rig_coll.children
                 if ch.name.startswith(MESH_COLLECTION_PREFIX)), None)


def _select_uv_parts(arm, char):
    """Parts to export = meshes VISIBLE in the basic look (fingers/3x3 off) of each arm
    variant (classic/slim). Uses the rig's own visibility logic, so it automatically filters
    out the fingers/3x3 variants -- regardless of name/suffix (e.g. alex fingers are
    .001/.002 on rig1 and .004/.005 on rig2).
    Requires the drivers to be active (not muted)."""
    meshes = [o for o in char if o.type == 'MESH']
    if arm is None:
        return meshes

    def _upd():
        arm.update_tag()
        for o in meshes:
            o.update_tag()
        bpy.context.view_layer.update()

    # force fingers/3x3 off (preserving the value type)
    saved = {}
    for bn, k in SELECTION_FORCE_OFF:
        pb = arm.pose.bones.get(bn)
        if pb is not None and k in pb.keys():
            saved[(bn, k)] = pb[k]
            pb[k] = type(pb[k])(0)
    # force "always export" toggles (e.g. Second layer) ON so their overlay parts are
    # detected as visible and selected, regardless of the rig UI toggle state.
    for bn, k in SELECTION_FORCE_ON:
        pb = arm.pose.bones.get(bn)
        if pb is not None and k in pb.keys():
            saved[(bn, k)] = pb[k]
            pb[k] = type(pb[k])(1)

    slim_bn, slim_key = SLIM_CONTROL
    slim_pb = arm.pose.bones.get(slim_bn)
    slim_orig = slim_pb[slim_key] if (slim_pb is not None and slim_key in slim_pb.keys()) else None
    slim_values = set(v for _, v in MASK_ARM_VARIANTS) or {False, True}

    visible = set()
    try:
        for val in slim_values:
            if slim_pb is not None:
                slim_pb[slim_key] = val
            _upd()
            dg = bpy.context.evaluated_depsgraph_get()
            for o in meshes:
                if not o.evaluated_get(dg).hide_render:
                    visible.add(o.name)
    finally:
        for (bn, k), v in saved.items():
            pb = arm.pose.bones.get(bn)
            if pb is not None:
                pb[k] = v
        if slim_pb is not None and slim_orig is not None:
            slim_pb[slim_key] = slim_orig
        _upd()
    return [o for o in meshes if o.name in visible]


def _player_armatures():
    """Just the player armatures (Rig_ID match), sorted -- cheap, no part selection. For the
    panel's player count/list (running the full discover_players() every redraw would toggle
    rig props + update the depsgraph on each UI refresh)."""
    arms = [o for o in bpy.context.scene.objects
            if o.type == 'ARMATURE' and RIG_ID_PROP in o.keys()
            and (RIG_ID_VALUE is None or o[RIG_ID_PROP] == RIG_ID_VALUE)]
    arms.sort(key=lambda a: a.name)
    return arms


def discover_players():
    """Player = armature with the Rig_ID custom property. Parts come from the Mesh
    collection located relative to the armature (independent of global collection names)."""
    players = []
    arms = [o for o in bpy.context.scene.objects
            if o.type == 'ARMATURE' and RIG_ID_PROP in o.keys()
            and (RIG_ID_VALUE is None or o[RIG_ID_PROP] == RIG_ID_VALUE)]
    arms.sort(key=lambda a: a.name)
    for arm in arms:
        # rig container = the armature's collection that has a "Mesh*" child
        rig_coll = next((c for c in arm.users_collection if _mesh_collection_for(c)), None)
        if rig_coll is None and arm.users_collection:
            rig_coll = arm.users_collection[0]
        mesh_coll = _mesh_collection_for(rig_coll)
        if mesh_coll is None:
            print(f"[discover] {arm.name}: Mesh collection not found, skipping.")
            continue
        extras = [o for o in rig_coll.objects
                  if o.type == 'MESH' and 'leggings' in o.name.lower()]
        char_all = [o for o in mesh_coll.objects if o.type == 'MESH'] + extras
        # Selection by visibility in the basic look (robustly filters fingers/3x3).
        uv_parts = _select_uv_parts(arm, char_all)
        label = ("%s%d" % (PLAYER_FOLDER_PREFIX, len(players) + 1)
                 if PLAYER_FOLDER_SCHEME == 'index' else arm.name)
        players.append({"label": label, "armature": arm.name,
                        "rig_id": arm[RIG_ID_PROP], "arm": arm,
                        "char_all": char_all, "uv_parts": uv_parts})

    # Cross-rig reconciliation: a part visible (in the basic look) on ANY rig is a real
    # export part on EVERY rig. This adds back parts left MANUALLY hide_render=True (no
    # driver) on a rig -- e.g. body/legs that a cancelled/interrupted render left hidden --
    # WITHOUT pulling in helpers (Head_Boolean_Eyes, ...) that are hidden on all rigs, nor
    # parts genuinely toggled off by a DRIVER (an overlay disabled for that player).
    canonical = set()
    for p in players:
        for o in p["uv_parts"]:
            canonical.add(_basename(o.name))
    for p in players:
        have = {o.name for o in p["uv_parts"]}
        for o in p["char_all"]:
            if (o.name not in have and o.type == 'MESH'
                    and _basename(o.name) in canonical
                    and o.hide_render and not _has_hide_render_driver(o)):
                p["uv_parts"].append(o)
                print(f"[reconcile] {p['label']}: re-added '{o.name}' "
                      f"(visible on another rig, here left manually hidden)")
    return players


def _mesh_rig_armature(o):
    """The armature a mesh is bound to (parented to, or skinned via an Armature modifier),
    or None. Used to treat a whole rig as one layer."""
    if o.parent is not None and o.parent.type == 'ARMATURE':
        return o.parent
    for m in o.modifiers:
        if m.type == 'ARMATURE' and m.object is not None:
            return m.object
    return None


def _rig_meshes(arm):
    """Every mesh bound to this armature (child OR skinned) -- the rig's renderable parts."""
    return [o for o in bpy.context.scene.objects
            if o.type == 'MESH' and (o.parent == arm
                                     or any(m.type == 'ARMATURE' and m.object == arm
                                            for m in o.modifiers))]


def discover_layers():
    """Optional layers as GROUPS. Each group is ONE independently-toggleable layer made of
    1+ meshes, marked with the LAYER_ID_PROP custom property on:
      - a COLLECTION  -> the group is every mesh in it (recursive);
      - an ARMATURE   -> the group is the rig's meshes (children + skinned);
      - a standalone MESH -> the group is just that mesh.
    A mesh already claimed by a marked collection/armature is not also emitted on its own.
    Returns a list of {name, object, kind, meshes:[...]} sorted by name."""
    scene = bpy.context.scene
    # single pass over scene.objects: the mesh-name set + the marked armatures/meshes at once
    scene_meshes, marked_arms, marked_meshes = set(), [], []
    for o in scene.objects:
        if o.type == 'MESH':
            scene_meshes.add(o.name)
            if LAYER_ID_PROP in o.keys():
                marked_meshes.append(o)
        elif o.type == 'ARMATURE' and LAYER_ID_PROP in o.keys():
            marked_arms.append(o)
    groups, claimed = [], set()

    def _add(name, source_name, kind, meshes):
        meshes = [m for m in meshes if m.name in scene_meshes and m.name not in claimed]
        if meshes:
            groups.append({"name": _layer_safe_name(name), "object": source_name,
                           "kind": kind, "meshes": meshes})
            claimed.update(m.name for m in meshes)

    # order matters: collections claim their meshes first, then armatures, then standalone meshes
    for coll in bpy.data.collections:               # 1) marked collections
        if LAYER_ID_PROP in coll.keys():
            _add(coll.name, coll.name, "collection",
                 [o for o in coll.all_objects if o.type == 'MESH'])
    for o in marked_arms:                            # 2) marked armatures (whole rig)
        _add(o.name, o.name, "armature", _rig_meshes(o))
    for o in marked_meshes:                          # 3) standalone marked meshes
        _add(o.name, o.name, "mesh", [o])

    groups.sort(key=lambda g: g["name"])
    return groups


def _layer_safe_name(name):
    """Filesystem-safe layer file stem (dots would read as extensions)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "layer"


def _object_camera_depth(o):
    """Camera distance to the evaluated bound-box center, in world units (None: no camera)."""
    import mathutils
    cam = bpy.context.scene.camera
    if cam is None:
        return None
    dg = bpy.context.evaluated_depsgraph_get()
    ev = o.evaluated_get(dg)
    mw = ev.matrix_world
    c = mathutils.Vector()
    for corner in ev.bound_box:
        c += mw @ mathutils.Vector(corner)
    return float(-(cam.matrix_world.inverted() @ (c / 8.0)).z)


def _group_camera_depth(meshes):
    """Average camera depth over a group's meshes (None if no camera/empty)."""
    ds = [d for d in (_object_camera_depth(o) for o in meshes) if d is not None]
    return sum(ds) / len(ds) if ds else None


def _fix_2layer_positions(players):
    """Fix each player's hat ('2_Layer_Extrusion', the head outer layer) for export:
      - snap it onto 'NoFace_Head' (match local location); and
      - scale it to the Minecraft hat size (HAT_SCALE_RATIO x the head; the rig leaves it the
        same size as the head). Set HAT_SCALE_RATIO=None to keep the rig's scale.
    Rotation is preserved. Persistent + idempotent. Returns the number of objects changed."""
    changed = 0
    for p in players:
        meshes = p["char_all"]
        head = next((o for o in meshes if o.name.startswith(LAYER2_HEAD_NAME)), None)
        if head is None:
            print(f"[HAT] {p['label']}: '{LAYER2_HEAD_NAME}*' not found, skipping.")
            continue
        for o in meshes:
            if not o.name.startswith(LAYER2_NAME):
                continue
            did = []
            if (o.location - head.location).length > 1e-5:
                o.location = head.location.copy()
                did.append("pos")
            if HAT_SCALE_RATIO is not None:
                target = head.scale * HAT_SCALE_RATIO
                if (o.scale - target).length > 1e-5:
                    o.scale = target
                    did.append(f"scale->{round(target.x, 4)}")
            if did:
                o.update_tag()
                changed += 1
                print(f"[HAT] {p['label']}: {o.name} ({', '.join(did)})")
    if changed:
        bpy.context.view_layer.update()
    return changed


def _opaque_mask_material():
    """Fully opaque gray material to override the skin during the mask."""
    name = "__MASK_OPAQUE__"
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        bsdf = next((n for n in m.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf:
            if 'Base Color' in bsdf.inputs:
                bsdf.inputs['Base Color'].default_value = MASK_OVERRIDE_RGBA
            if 'Alpha' in bsdf.inputs:
                bsdf.inputs['Alpha'].default_value = 1.0
    m.blend_method = 'OPAQUE'
    return m


def _backface_material():
    """Material that only renders the BACK FACES: front transparent, back opaque
    (via Geometry > Backfacing). Color irrelevant for the UV pass; only opacity matters."""
    name = "__MASK_BACKFACE__"
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    mix = nt.nodes.new('ShaderNodeMixShader')
    transp = nt.nodes.new('ShaderNodeBsdfTransparent')
    emit = nt.nodes.new('ShaderNodeEmission')
    emit.inputs['Color'].default_value = MASK_OVERRIDE_RGBA
    geo = nt.nodes.new('ShaderNodeNewGeometry')
    nt.links.new(geo.outputs['Backfacing'], mix.inputs[0])   # fac
    nt.links.new(transp.outputs['BSDF'], mix.inputs[1])      # front (fac=0) -> transparent
    nt.links.new(emit.outputs['Emission'], mix.inputs[2])    # back  (fac=1) -> opaque
    nt.links.new(mix.outputs['Shader'], out.inputs['Surface'])
    m.blend_method = 'BLEND'
    return m


def _swap_materials(meshes, mat):
    """Swap the material of every slot to `mat`. Returns data to restore."""
    saved = []
    for o in meshes:
        if not o.material_slots:
            continue
        saved.append((o, [s.material for s in o.material_slots]))
        for s in o.material_slots:
            s.material = mat
    return saved


def _restore_materials(saved):
    for o, slots in saved:
        for i, m in enumerate(slots):
            if i < len(o.material_slots):
                o.material_slots[i].material = m


def _set_arm_style(player, slim_value):
    """Set the master 'Slim main' (False=Steve, True=Alex) and propagate the driver chain
    (needs update_tag on the objects + view_layer.update). Returns (arm, orig)."""
    arm = player.get("arm")
    if arm is None:
        return None, None
    bn, key = SLIM_CONTROL
    pb = arm.pose.bones.get(bn)
    if pb is None or key not in pb.keys():
        print(f"[arm] {arm.name}: no property {bn}['{key}']")
        return arm, None
    saved = pb[key]
    pb[key] = slim_value
    arm.update_tag()
    for o in player["char_all"]:
        o.update_tag()
    bpy.context.view_layer.update()
    return arm, saved


def _restore_arm_style(arm, player, saved):
    if arm is None or saved is None:
        return
    bn, key = SLIM_CONTROL
    pb = arm.pose.bones.get(bn)
    if pb is not None:
        pb[key] = saved
        arm.update_tag()
        for o in player["char_all"]:
            o.update_tag()
        bpy.context.view_layer.update()


def _remove_stale_variants(path):
    """Delete sibling files with the SAME stem but another exporter extension -- leftovers
    from a previous run with a different output format (e.g. shadow_classic.png after
    switching to WebP). They sit next to the fresh file at a possibly different resolution
    and confuse the consumer."""
    stem, cur = os.path.splitext(path)
    for e in ('.png', '.jpg', '.webp', '.exr'):
        if e == cur.lower():
            continue
        p = stem + e
        if os.path.exists(p):
            try:
                os.remove(p)
                print(f"[clean] removed stale {os.path.basename(p)}")
            except OSError:
                pass


def _save_image(arr_flat, W, H, path, colorspace='Non-Color',
                file_format='PNG', bit_depth=None, quality=None, lossless=False):
    """Save flat RGBA pixels (scene-linear float) to an image file. No view transform is
    applied; the colorspace controls encoding: 'Non-Color' = raw linear values (UV/Depth/
    shadow), 'sRGB' = sRGB curve on encode (illum -> display look).
    PNG honors bit_depth (default PNG_BIT_DEPTH; 8 or 16). JPEG/WEBP are 8-bit; quality
    defaults to JPEG_QUALITY. lossless=True + WEBP forces quality=100 (libwebp's lossless
    mode -- for data like the UVs; byte-exact where alpha=1, the alpha-0 RGB is
    premultiply-zeroed). OPEN_EXR is float (half if EXR_HALF) and preserves the exact
    values -- no premultiply/quantization."""
    if bit_depth is None:
        bit_depth = PNG_BIT_DEPTH
    # float buffer -> 16-bit PNG / float EXR
    use_float = (file_format == 'OPEN_EXR') or (file_format == 'PNG' and bit_depth >= 16)
    img = bpy.data.images.new("__tmp_save__", W, H, alpha=True, float_buffer=use_float)
    img.colorspace_settings.name = colorspace
    img.pixels.foreach_set(arr_flat)
    img.filepath_raw = path
    img.file_format = file_format
    if file_format == 'OPEN_EXR':
        # img.save() writes uncompressed 32-bit EXR (huge); use save_render to control the
        # codec + half precision. color_management OVERRIDE + 'Raw' view writes raw values.
        sc = bpy.context.scene
        ims = sc.render.image_settings
        snap = (ims.file_format, ims.color_depth, ims.exr_codec, ims.color_management)
        vt = None
        try:
            ims.file_format = 'OPEN_EXR'
            ims.color_depth = '16' if EXR_HALF else '32'
            ims.exr_codec = EXR_CODEC
            ims.color_management = 'OVERRIDE'
            vt = ims.view_settings.view_transform
            ims.view_settings.view_transform = 'Raw'
            img.save_render(path, scene=sc)
        finally:
            ims.file_format, ims.color_depth, ims.exr_codec, ims.color_management = snap
            if vt is not None:
                try:
                    ims.view_settings.view_transform = vt
                except Exception:
                    pass
    elif file_format == 'WEBP' and lossless:
        # Data WebP: quality=100 switches libwebp to LOSSLESS mode (q<100 is lossy VP8 and
        # corrupts the values -- never use it for data). Saved via save_render with
        # color_management OVERRIDE + 'Raw' so the bytes are written untransformed.
        sc = bpy.context.scene
        ims = sc.render.image_settings
        snap = (ims.file_format, ims.quality, ims.color_management)
        vt = None
        try:
            ims.file_format = 'WEBP'
            ims.quality = 100
            ims.color_management = 'OVERRIDE'
            vt = ims.view_settings.view_transform
            ims.view_settings.view_transform = 'Raw'
            img.save_render(path, scene=sc)
        finally:
            ims.file_format, ims.quality, ims.color_management = snap
            if vt is not None:
                try:
                    ims.view_settings.view_transform = vt
                except Exception:
                    pass
    elif file_format in ('JPEG', 'WEBP'):
        img.save(quality=JPEG_QUALITY if quality is None else quality)
    elif colorspace == 'Non-Color':
        # Data PNG: img.save() ignores PNG compression (stuck on Blender's low default), so
        # save via save_render to set it. color_management OVERRIDE + 'Raw' writes raw bytes.
        sc = bpy.context.scene
        ims = sc.render.image_settings
        snap = (ims.file_format, ims.compression, ims.color_depth, ims.color_management)
        vt = None
        try:
            ims.file_format = 'PNG'
            ims.compression = PNG_COMPRESSION
            ims.color_depth = '16' if bit_depth >= 16 else '8'
            ims.color_management = 'OVERRIDE'
            vt = ims.view_settings.view_transform
            ims.view_settings.view_transform = 'Raw'
            img.save_render(path, scene=sc)
        finally:
            ims.file_format, ims.compression, ims.color_depth, ims.color_management = snap
            if vt is not None:
                try:
                    ims.view_settings.view_transform = vt
                except Exception:
                    pass
    else:
        img.save()
    bpy.data.images.remove(img)
    _remove_stale_variants(path)


def _save_exr_uvdl(out4, light3, W, H, path):
    """Write a multi-channel EXR via OpenImageIO: the default RGBA layer = (R=U, G=V,
    B=depth, A=coverage) -- same as the PNG, so the existing reader works -- plus a 'light'
    layer (light.R/G/B = sRGB illum color). Half (EXR_HALF) + EXR_CODEC compression."""
    import OpenImageIO as oiio
    n = W * H
    if light3 is None:
        light3 = np.zeros((n, 3), dtype='float32')
    data = np.concatenate([np.asarray(out4, dtype='float32'),
                           np.asarray(light3, dtype='float32')], axis=1)   # (N, 7)
    data = np.ascontiguousarray(data.reshape(H, W, 7))
    spec = oiio.ImageSpec(W, H, 7, oiio.HALF if EXR_HALF else oiio.FLOAT)
    spec.channelnames = ("R", "G", "B", "A", "light.R", "light.G", "light.B")
    spec.attribute("compression", EXR_CODEC.lower())
    out = oiio.ImageOutput.create(path)
    if out is None or not out.open(path, spec):
        raise RuntimeError(f"OIIO cannot write {path}: {oiio.geterror()}")
    out.write_image(data)
    out.close()


class _Session:
    """Full snapshot/restore + reading a pass via the Viewer."""

    def __init__(self):
        s = bpy.context.scene
        vl = bpy.context.view_layer
        self.s, self.vl = s, vl

        # --- self-sufficient compositor ---
        # If there is no node group / Render Layers / Viewer, we create the minimum
        # needed and undo it on restore. This way the script does not depend on the
        # compositor being set up by the user.
        self.orig_use_nodes = getattr(s, "use_nodes", None)
        self.orig_group = s.compositing_node_group
        self.created_group = False
        self.created_rl = False
        self.created_viewer = False
        self.created_group_output = None     # NodeGroupOutput added by ensure_group_output(), if any
        self._created_go_socket = None       # interface OUTPUT socket added for it, if any

        ng = s.compositing_node_group
        if ng is None:
            ng = bpy.data.node_groups.new("__UVMASK_COMP__", "CompositorNodeTree")
            s.compositing_node_group = ng
            if hasattr(s, "use_nodes"):
                s.use_nodes = True
            self.created_group = True
        self.ng = ng

        self.rl = next((n for n in ng.nodes if n.type == 'R_LAYERS'), None)
        if self.rl is None:
            self.rl = ng.nodes.new('CompositorNodeRLayers')
            try:
                self.rl.layer = vl.name
            except Exception:
                pass
            self.created_rl = True

        self.out_node = next((n for n in ng.nodes if n.type == 'OUTPUT_FILE'), None)

        # Ensure a SINGLE viewer: duplicate viewers break reading from
        # bpy.data.images['Viewer Node'] (it is global) -> it would read the wrong "active"
        # viewer (e.g. the beauty) instead of the connected pass. Remove extras (self-heals
        # leaks when a previous _Session did not restore due to a timeout).
        _viewers = [n for n in ng.nodes if n.type == 'VIEWER']
        for _extra in _viewers[1:]:
            ng.nodes.remove(_extra)
        self.viewer = _viewers[0] if _viewers else None
        if self.viewer is None:
            self.viewer = ng.nodes.new('CompositorNodeViewer')
            self.created_viewer = True
        vin = self.viewer.inputs[0]
        self.viewer_orig = vin.links[0].from_socket if vin.is_linked else None

        self.film = s.render.film_transparent
        self.use_comp = s.render.use_compositing
        self.engine = s.render.engine
        self.res_pct = s.render.resolution_percentage
        self.samples = getattr(getattr(s, 'cycles', None), 'samples', None)
        self.denoise = getattr(getattr(s, 'cycles', None), 'use_denoising', None)
        self.pass_uv = vl.use_pass_uv
        self.pass_oi = vl.use_pass_object_index
        self.pass_z = vl.use_pass_z
        self.out_mute = self.out_node.mute if self.out_node else None

        self.hide = {o.name: o.hide_render for o in s.objects}
        self.pass_index = {o.name: o.pass_index for o in s.objects}
        self._drivers = _hide_render_drivers()
        self.driver_mute = [d.mute for d in self._drivers]
        # Object names kept render-hidden by restore_visibility() for the whole batch (the
        # optional layers, excluded from the player pipeline + background). The final
        # restore() still puts back the true original visibility.
        self.force_hidden = set()

    def mute_drivers(self, mute):
        for d in self._drivers:
            d.mute = mute

    def restore_visibility(self):
        """Restore the original visibility (drivers active), keeping force_hidden hidden."""
        self.mute_drivers(False)
        for o in self.s.objects:
            if o.name in self.hide:
                o.hide_render = self.hide[o.name]
        for name in self.force_hidden:
            o = self.s.objects.get(name)
            if o is not None:
                o.hide_render = True
        bpy.context.view_layer.update()

    def ensure_group_output(self):
        """A non-blocking INVOKE_DEFAULT render (the modal tier-2 driver) requires the compositing
        node group to expose a valid output node. The render setup mutes the File Output and reads
        only the Viewer, which a blocking EXEC render tolerates but INVOKE rejects ('No Group Output
        or File Output nodes in scene'). Add a Group Output fed by the Render Layers Image (no file
        written) so INVOKE is happy; the Viewer stays the read source. Idempotent; the node (and the
        interface socket it needs) are removed in restore() unless the whole group was created by us."""
        ng = self.ng
        go = next((n for n in ng.nodes if n.type == 'GROUP_OUTPUT'), None)
        if go is None:
            has_out = any(getattr(it, "in_out", "") == 'OUTPUT'
                          for it in ng.interface.items_tree
                          if getattr(it, "item_type", "") == 'SOCKET')
            if not has_out:
                self._created_go_socket = ng.interface.new_socket(
                    "Image", in_out='OUTPUT', socket_type='NodeSocketColor')
            go = ng.nodes.new('NodeGroupOutput')
            self.created_group_output = go
        img = self.rl.outputs.get('Image')
        if img is not None and go.inputs and not go.inputs[0].is_linked:
            ng.links.new(img, go.inputs[0])

    def render_pass(self, socket_name, engine, samples, res_pct):
        """SYNC wrapper: drains render_pass_gen with a blocking render (the current behaviour)."""
        return _drain_render(self.render_pass_gen(socket_name, engine, samples, res_pct))

    def render_pass_gen(self, socket_name, engine, samples, res_pct):
        """Render one Render-Layers pass (UV / Depth / Object Index), read via the Viewer. GENERATOR:
        yields `_RENDER` where the blocking render used to be, RETURNS (arr, W, H) -- or (None, 0, 0)
        if the socket is missing / the Viewer is empty."""
        s, ng = self.s, self.ng
        # make sure sockets of just-enabled passes already exist on the Render Layers node
        bpy.context.view_layer.update()
        sock = self.rl.outputs.get(socket_name)
        if sock is None:
            print(f"[render_pass] socket '{socket_name}' missing (pass not enabled?).")
            return None, 0, 0
        vin = self.viewer.inputs[0]
        for l in list(vin.links):
            ng.links.remove(l)
        ng.links.new(sock, vin)
        if self.out_node:
            self.out_node.mute = True
        self.ensure_group_output()
        s.render.engine = engine
        if hasattr(s, 'cycles'):
            s.cycles.samples = samples
            s.cycles.use_denoising = False   # data pass (UV/Depth/ObjectIndex): NEVER denoise
        s.render.resolution_percentage = res_pct
        s.render.film_transparent = True
        s.render.use_compositing = True
        bpy.context.view_layer.update()
        yield _RENDER
        vimg = bpy.data.images.get('Viewer Node')
        if not vimg or len(vimg.pixels) == 0:
            print("[render_pass] Viewer Node empty (requires a GUI).")
            return None, 0, 0
        W, H = vimg.size
        arr = np.empty(len(vimg.pixels), dtype='float32')
        vimg.pixels.foreach_get(arr)
        return arr.reshape(-1, 4), W, H

    def restore(self):
        s, vl, ng = self.s, self.vl, self.ng
        vin = self.viewer.inputs[0]
        for l in list(vin.links):
            ng.links.remove(l)
        # If we created the whole group, it is removed at the end (taking its nodes with it).
        # Otherwise, we only remove the nodes we created inside the user's group.
        if not self.created_group:
            if self.created_group_output is not None:
                try:
                    ng.nodes.remove(self.created_group_output)
                except Exception:
                    pass
            if self._created_go_socket is not None:
                try:
                    ng.interface.remove(self._created_go_socket)
                except Exception:
                    pass
            if self.created_viewer:
                ng.nodes.remove(self.viewer)
            elif self.viewer_orig is not None:
                ng.links.new(self.viewer_orig, vin)
            if self.created_rl:
                ng.nodes.remove(self.rl)
        s.render.film_transparent = self.film
        s.render.use_compositing = self.use_comp
        s.render.engine = self.engine
        s.render.resolution_percentage = self.res_pct
        if self.samples is not None and hasattr(s, 'cycles'):
            s.cycles.samples = self.samples
        if self.denoise is not None and hasattr(s, 'cycles'):
            s.cycles.use_denoising = self.denoise
        vl.use_pass_uv = self.pass_uv
        vl.use_pass_object_index = self.pass_oi
        vl.use_pass_z = self.pass_z
        if self.out_node is not None:
            self.out_node.mute = self.out_mute
        for d, m in zip(self._drivers, self.driver_mute):
            d.mute = m
        for o in s.objects:
            if o.name in self.hide:
                o.hide_render = self.hide[o.name]
            if o.name in self.pass_index:
                o.pass_index = self.pass_index[o.name]
        # if we created the compositor node group, tear it all down
        if self.created_group:
            grp = s.compositing_node_group
            s.compositing_node_group = self.orig_group
            if self.orig_use_nodes is not None and hasattr(s, "use_nodes"):
                s.use_nodes = self.orig_use_nodes
            if grp is not None and grp.users == 0:
                bpy.data.node_groups.remove(grp)
        bpy.context.view_layer.update()


def _rendered_depth_range(player, sess, gray_mat, _back_mat=None):
    """Depth range of the whole character, IN THE SAME SCALE as the Viewer (which gives
    the Depth linear but scaled). Uses ONLY the OPAQUE (front) render: opaque blocks the
    ray, so there is no pass-through nor background contamination (unlike the backface
    material, whose UV coverage marks the whole silhouette but the depth is the background
    where there is no back face). The deepest back faces saturate slightly at 1.0 on
    normalization -- acceptable.
    Returns (rng, reason): ((vmin, vmax), None) on success, or (None, reason) on failure
    -- reason names WHICH path failed so the caller can report it instead of silently
    exporting a flat (depth-less) B channel."""
    s = sess.s
    parts = list(player["uv_parts"])
    if not parts:
        return None, "no UV parts"
    sess.mute_drivers(True)
    for o in s.objects:
        o.hide_render = True
    for o in parts:
        o.hide_render = False
    sess.vl.use_pass_uv = True
    sess.vl.use_pass_z = True
    sv = _swap_materials(parts, gray_mat)   # OPAQUE -> reliable depth, no background
    try:
        uv, W, H = sess.render_pass('UV', 'CYCLES', 1, 50)
        dep, _, _ = sess.render_pass('Depth', 'CYCLES', 1, 50)
    finally:
        _restore_materials(sv)
    if uv is None:
        return None, "empty Viewer node on the UV pass (transient GUI/render hiccup)"
    if dep is None:
        return None, "empty Viewer node on the Depth pass (transient GUI/render hiccup)"
    geo = dep[uv[:, 2] > 0.5, 0]   # reliable coverage (opaque: front face = surface)
    if geo.size == 0:
        return None, "zero coverage in the opaque render (no part pixels hit the camera)"
    vmin, vmax = float(geo.min()), float(geo.max())
    if vmax - vmin < 1e-6:
        vmax = vmin + 1e-6
    return (vmin, vmax), None


def _depth_range_or_abort(player, sess, gray_mat, back_mat, label):
    """_rendered_depth_range with ONE retry on failure (covers a transient empty Viewer
    node, common on the first renders of a session), then raise with a clear message if it
    still fails. Exporting without a depth range silently flattens the UV's B channel and
    breaks depth occlusion -- failing loudly is better than a broken-but-'successful' export."""
    rng, reason = _rendered_depth_range(player, sess, gray_mat, back_mat)
    if rng is None:
        print(f"[depth] {label}: depth range failed -- {reason}. Retrying once...")
        rng, reason = _rendered_depth_range(player, sess, gray_mat, back_mat)
    if rng is None:
        raise RuntimeError(
            f"Depth range unavailable for {label}: {reason}. The UV depth (B channel) "
            f"would be a flat placeholder, breaking depth occlusion against the other "
            f"players/layers. Export aborted -- if this was a transient empty Viewer node, "
            f"just run it again.")
    return rng


def export_part_uv(part, sess, out_subdir, tag="_UV", depth_range=None, label=None,
                   light_map=None, occlusion=None):
    """Render one part's UV and save it. The file is RGBA = (R=U, G=V, B=depth, A=coverage);
    if light_map (N,3 sRGB; float 0-1 or uint8) is given AND UV_FORMAT is EXR, the light is
    embedded as an extra 'light' layer. occlusion (N,) bool, if given, is ANDed into the
    coverage (pixels hidden behind the scenery -- used by the optional layers). Returns
    (path, out(N,4), cov, light(N,3) or None, W, H) for the base-layer composite -- or None
    if the part is empty."""
    stem = label if label is not None else part.name
    s = sess.s
    sess.mute_drivers(True)
    for o in s.objects:
        o.hide_render = True
    part.hide_render = False
    sess.vl.use_pass_uv = True
    print(f"[UV] {out_subdir} / {stem}{tag}  ({part.name})")
    uv, W, H = sess.render_pass('UV', 'CYCLES', UV_SAMPLES, MASK_RES_PCT)
    if uv is None:
        return None
    out = uv.copy()                       # R=U, G=V, B=1.0 (coverage), A=1
    cov = uv[:, 2] > 0.5                   # coverage: UV pass B = 1 on geometry
    if occlusion is not None and occlusion.shape == cov.shape:
        cov &= occlusion
    if UV_DEPTH_IN_BLUE and depth_range is not None:
        sess.vl.use_pass_z = True
        dep, _, _ = sess.render_pass('Depth', 'CYCLES', UV_SAMPLES, MASK_RES_PCT)
        if dep is not None:
            zmin, zmax = depth_range
            z = dep[:, 0]
            b = np.zeros(z.shape, dtype='float32')
            b[cov] = np.clip((z[cov] - zmin) / (zmax - zmin), 0.0, 1.0)
            out[:, 2] = b                 # B = normalized depth (0=near, 1=far)
    out[:, 3] = cov.astype('float32')        # A = coverage (1 inside the part, 0 outside)
    # 8-bit PNG: store U/V as the texel index (FLOOR), not round-to-nearest. byte = floor(u*
    # bins) clamped to 255, written as byte/255 so Blender's round() reproduces it exactly.
    if UV_FORMAT != 'OPEN_EXR' and UV_PNG_FLOOR_TEXELS:
        out[:, 0] = np.clip(np.floor(out[:, 0] * UV_TEXEL_BINS), 0.0, 255.0) / 255.0
        out[:, 1] = np.clip(np.floor(out[:, 1] * UV_TEXEL_BINS), 0.0, 255.0) / 255.0
    # Per-part RGB light (sRGB), masked to covered pixels. Embedded in the EXR's 'light'
    # layer; for PNG it is saved as a separate "<part>_light.<ext>" image.
    light = None
    if (light_map is not None and getattr(light_map, "ndim", 0) == 2
            and light_map.shape == (out.shape[0], 3)):
        light = np.zeros((out.shape[0], 3), dtype='float32')
        vals = light_map[cov]
        if vals.dtype == np.uint8:        # light_maps are stored as uint8 to save RAM
            vals = vals.astype('float32') / 255.0
        light[cov] = np.clip(vals, 0.0, 1.0)
    path = os.path.join(_abs(OUT_DIR), out_subdir, stem + tag + UV_EXT)
    # Switching UV_FORMAT also switches the tag (_UV <-> _UVDL): clean every leftover of
    # the OTHER tag (any extension), then the same-stem leftovers via the save below.
    if tag in ("_UV", "_UVDL"):
        _remove_stale_variants(os.path.join(_abs(OUT_DIR), out_subdir,
                               stem + ("_UVDL" if tag == "_UV" else "_UV")))
    if UV_FORMAT == 'OPEN_EXR' and light is not None:
        _save_exr_uvdl(out, light, W, H, path)
        _remove_stale_variants(path)
    else:
        _save_image(out.reshape(-1), W, H, path, file_format=UV_FORMAT, lossless=True)
        if light is not None and EXPORT_PART_LIGHT:
            la = np.ones((light.shape[0], 4), dtype='float32')
            la[:, :3] = light
            _save_image(la.reshape(-1), W, H,
                        os.path.join(_abs(OUT_DIR), out_subdir, stem + "_light" + LIGHTSHADOW_EXT),
                        colorspace='Non-Color', file_format=LIGHTSHADOW_FORMAT)
    return path, out, cov, light, W, H


def export_character_mask_variant(player, vname, vval, sess, all_players=None):
    """Player mask, occluded ONLY by the scenery (the other players are hidden), for one
    arm variant (classic/slim).

    Precondition: the materials have already been swapped to an opaque one (without it the
    skin's transparency would punch holes in the mask on the Object Index pass).
    """
    s = sess.s
    sess.restore_visibility()
    arm, saved_arm = _set_arm_style(player, vval)   # switch classic/slim (master Slim main)

    # Hide the OTHER players: the mask must be occluded only by the scenery, not by other
    # players. Mute their hide_render drivers (otherwise the driver would re-show them) and
    # set hide_render=True. The target player's armature keeps its drivers active (so its
    # arm variant is preserved). Restored in the finally block.
    muted = []
    hidden = []
    if all_players:
        others = set(o.name for op in all_players if op is not player for o in op["char_all"])
        for o in s.objects:
            if o.name in others:
                if o.animation_data:
                    for d in o.animation_data.drivers:
                        if d.data_path == "hide_render" and not d.mute:
                            d.mute = True
                            muted.append(d)
                hidden.append((o, o.hide_render))
                o.hide_render = True
        bpy.context.view_layer.update()
    try:
        for o in s.objects:
            o.pass_index = 0
        names = set(o.name for o in player["char_all"])
        for o in s.objects:
            if o.name in names:
                o.pass_index = PLAYER_INDEX
        sess.vl.use_pass_object_index = True
        print(f"[MASK] {player['label']} / {vname}")
        arr, W, H = sess.render_pass('Object Index', 'CYCLES', MASK_SAMPLES, MASK_RES_PCT)
        if arr is None:
            return None, None, 0, 0
        m = (arr[:, 0] >= 0.5).astype('float32')
        out = np.zeros((m.size, 4), dtype='float32')
        out[:, 0] = out[:, 1] = out[:, 2] = m
        out[:, 3] = 1.0
        path = os.path.join(_abs(OUT_DIR), player["label"],
                            f"mask_{vname}.png")
        _save_image(out.reshape(-1), W, H, path)
        return path, _topleft_bbox(m >= 0.5, W, H), W, H
    finally:
        for d in muted:
            d.mute = False
        for o, hr in hidden:
            o.hide_render = hr
        _restore_arm_style(arm, player, saved_arm)


def _gray_diffuse_material():
    """Pure diffuse gray -- a clean lighting response (no texture albedo)."""
    name = "__ILLUM_GRAY__"
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    dif = nt.nodes.new('ShaderNodeBsdfDiffuse')
    dif.inputs['Color'].default_value = ILLUM_GRAY_RGBA
    nt.links.new(dif.outputs['BSDF'], out.inputs['Surface'])
    return m


def _box_blur_2d(x, r):
    """Separable box blur (radius r px) over the first two axes, edge-clamped. x: (H, W) or
    (H, W, C). Cumulative-sum implementation -- O(n) regardless of radius."""
    if r <= 0:
        return x
    x = np.asarray(x, 'float32')
    k = 2 * r + 1
    for ax in (0, 1):
        n = x.shape[ax]
        pad = [(0, 0)] * x.ndim
        pad[ax] = (r, r)
        xp = np.pad(x, pad, mode='edge')
        z = list(xp.shape); z[ax] = 1
        c = np.concatenate([np.zeros(z, 'float32'), np.cumsum(xp, axis=ax, dtype='float32')],
                           axis=ax)
        hi = np.take(c, np.arange(k, n + k), axis=ax)
        lo = np.take(c, np.arange(0, n), axis=ax)
        x = (hi - lo) / k
    return x


def _masked_blur(rgb, mask, r, passes):
    """Blur `rgb` (H, W, 3) within `mask` (H, W, 0/1) without bleeding the zeroed background in:
    blur(rgb*mask)/blur(mask). Smooths the per-pixel lighting lattice while keeping the light's
    low-frequency gradient; the un-covered texels are left untouched by the caller."""
    if r <= 0 or passes <= 0:
        return rgb
    num = rgb * mask[:, :, None]
    den = mask.copy()
    for _ in range(passes):
        num = _box_blur_2d(num, r)
        den = _box_blur_2d(den, r)
    return num / np.maximum(den, 1e-4)[:, :, None]


def _atlas_bake_material(img):
    """Gray-0.5 diffuse with an Image Texture node = `img`, set active so it is the bake
    target. Same lighting response as `_gray_diffuse_material`, plus the destination image."""
    name = "__ATLAS_BAKE__"
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    dif = nt.nodes.new('ShaderNodeBsdfDiffuse')
    dif.inputs['Color'].default_value = ILLUM_GRAY_RGBA
    tex = nt.nodes.new('ShaderNodeTexImage')
    tex.image = img
    nt.links.new(dif.outputs['BSDF'], out.inputs['Surface'])
    nt.nodes.active = tex            # bake writes to the active image node
    return m


def _force_bake_visible(objs):
    """Make `objs` selectable/visible in the view layer for a bake op. A marked layer object can
    RENDER (hide_render False) while its collection is hidden in the VIEWPORT (eye off), which makes
    visible_get() False and `bpy.ops.object.bake` fail with 'No valid selected objects'. Clear the
    object- and collection-level VIEWPORT hide along the chain to each object (never `exclude`, which
    would change what renders). Returns a restore callable."""
    vl = bpy.context.view_layer
    objset = set(objs)
    saved = []
    for o in objs:
        saved.append(("obj", o, o.hide_viewport, o.hide_select))
        o.hide_viewport = False
        o.hide_select = False
        try:
            o.hide_set(False)
        except RuntimeError:
            pass
    for coll in bpy.data.collections:                  # global (monitor) eye
        if coll.hide_viewport and any(o.name in coll.all_objects for o in objset):
            saved.append(("coll", coll, coll.hide_viewport, None))
            coll.hide_viewport = False

    def enable(lc):                                    # per-view-layer eye, along the chain
        has = any([enable(c) for c in lc.children])    # eval ALL children (no short-circuit)
        if lc.collection and any(o.name in lc.collection.objects for o in objset):
            has = True
        if has and lc.hide_viewport:
            saved.append(("lc", lc, lc.hide_viewport, None))
            lc.hide_viewport = False
        return has
    enable(vl.layer_collection)
    bpy.context.view_layer.update()

    def restore():
        for kind, obj, a, _b in reversed(saved):
            if kind == "obj":
                obj.hide_viewport, obj.hide_select = a, _b
            else:
                obj.hide_viewport = a
        bpy.context.view_layer.update()
    return restore


def _hide_others_for_bake(s, active_names, entity_names):
    """For an atlas bake, hide every OTHER entity (the other players + ALL optional layers) so the
    baked light is INDEPENDENT -- no other player's or toggleable layer's shadow/occlusion bakes
    into this atlas (which a toggle could then never remove). The scenery stays visible for the real
    lighting. Mutes hide_render drivers. Returns the muted drivers to un-mute afterwards."""
    muted = []
    for o in s.objects:
        if o.name in entity_names and o.name not in active_names:
            if o.animation_data:
                for d in o.animation_data.drivers:
                    if d.data_path == "hide_render" and not d.mute:
                        d.mute = True
                        muted.append(d)
            o.hide_render = True
    return muted


def _bake_player_light_atlas(player, atlas_res=None, samples=None, out_dir=None, stem=None):
    """Bake a player's scene lighting into a UV-space light atlas (skin layout) and save it. By
    default goes to `<OUT_DIR>/<player>/light_atlas.<ext>`; pass `out_dir` (absolute) + `stem` to
    place it elsewhere (e.g. the self-contained `static/<label>_atlas.<ext>`). Returns the
    OUT_DIR-relative path. ONE Cycles COMBINED pass: every part wears the gray-0.5 diffuse
    bake material (shared Image Texture node), and the overlay parts (hat/jacket/sleeve/pant)
    are made NON-OCCLUDING via ray visibility (Option B) so the base UV regions are lit
    overlay-free -- a skin that leaves an overlay empty then samples the base's own light, not a
    phantom shadow under it. The saved atlas is display-encoded (`_to_display`) and written with
    the panel Quality "Illum/Shadow" format -- the same model as the screen-space `_light`, so
    the browser relights with `lit = skin(uv) * atlas(uv) * 2`.

    The bake integrates the CURRENT scene (lights + scenery shadows/bounce), so the caller must
    put the scene in its normal look first (e.g. `sess.restore_visibility()`); other players keep
    their own materials and correctly cast shadows. Returns the OUT_DIR-relative path, or None if
    the player has no parts. Self-contained: restores materials, ray visibility, engine, samples,
    bake margin and selection. Runs entirely in one call (reads `img.pixels` in-memory and saves
    via `_save_image`/`save_render` -- never `img.save()`, which would zero the baked buffer)."""
    atlas_res = atlas_res or ATLAS_RES
    samples = samples or ATLAS_BAKE_SAMPLES or ILLUM_SAMPLES   # follow the panel quality (live)
    # Only the parts actually rendered for this player (visible): the unused arm style (slim vs
    # classic) and per-player 2nd-layer toggles are driven hide_render=True. Baking the hidden
    # duplicates would contaminate shared UV islands (e.g. the hidden slim arm, parked far away
    # with different lighting, overlaps the classic arm's UV region). Matches the mesh selection.
    parts = [o for o in (player.get("uv_parts") or []) if not o.hide_render]
    if not parts:
        return None
    scene = bpy.context.scene
    labels = _assign_part_labels(parts, player.get("label"))
    overlays = [o for o in parts
                if any(k in labels[o.name].lower() for k in ATLAS_OVERLAY_LABELS)]

    # ---- snapshot state to restore in finally ----
    saved_mats = {o.name: [s.material for s in o.material_slots] for o in parts}
    saved_slots = {o.name: len(o.material_slots) for o in parts}
    saved_vis = {o.name: (o.visible_diffuse, o.visible_shadow, o.visible_glossy)
                 for o in overlays}
    saved_eng = scene.render.engine
    cyc = getattr(scene, 'cycles', None)
    saved_samp = getattr(cyc, 'samples', None) if cyc else None
    saved_den = getattr(cyc, 'use_denoising', None) if cyc else None
    saved_margin = scene.render.bake.margin
    saved_active = bpy.context.view_layer.objects.active
    saved_sel = list(bpy.context.selected_objects)
    # Flat-shade the bake: disable the per-pixel "Auto Smooth" (geometry nodes) + Subdivision so
    # each face is lit per its own orientation (uniform within, sharp at face edges) instead of a
    # smooth-shaded per-pixel lattice. Snapshot show_render to restore.
    flat_mods = []
    if ATLAS_BAKE_FLAT:
        for o in parts:
            for m in o.modifiers:
                if m.type == 'SUBSURF' or (m.type == 'NODES' and 'smooth' in m.name.lower()):
                    flat_mods.append((m, m.show_render))

    name = "__ATLAS_IMG__"
    if bpy.data.images.get(name):
        bpy.data.images.remove(bpy.data.images[name])
    img = bpy.data.images.new(name, atlas_res, atlas_res, alpha=True, float_buffer=True)
    mat = _atlas_bake_material(img)
    vis_restore = None
    try:
        for o in parts:
            if len(o.material_slots) == 0:        # bake target needs a material slot
                o.data.materials.append(None)
            for i in range(len(o.material_slots)):
                o.material_slots[i].material = mat
        for o in overlays:                        # Option B: overlays don't occlude the base
            o.visible_diffuse = False
            o.visible_shadow = False
            o.visible_glossy = False
        for m, _ in flat_mods:                    # flat-shaded, un-subdivided cage for the bake
            m.show_render = False
        vis_restore = _force_bake_visible(parts)   # a hidden-in-viewport layer collection -> bake fails
        bpy.ops.object.select_all(action='DESELECT')
        for o in parts:
            o.select_set(True)
        bpy.context.view_layer.objects.active = parts[0]
        parts[0].active_material_index = 0
        scene.render.engine = 'CYCLES'
        if cyc:
            scene.cycles.samples = samples
            scene.cycles.use_denoising = True
        scene.render.bake.margin = ATLAS_BAKE_MARGIN
        bpy.context.view_layer.update()
        print(f"[ATLAS] baking '{player.get('label')}' -> {atlas_res}x{atlas_res}, {samples} spp"
              f" ({len(parts)} parts, {len(overlays)} non-occluding overlays)")
        bpy.ops.object.bake(type='COMBINED', use_clear=True)

        # read the baked LINEAR light in-memory
        flat = np.empty(atlas_res * atlas_res * 4, dtype='float32')
        img.pixels.foreach_get(flat)
        grid = flat.reshape(atlas_res, atlas_res, 4)
        # Low-pass the light (masked, within coverage) to kill the per-pixel relief lattice that
        # a sharp bake captures -- the light is low-frequency; the per-pixel detail is the skin's.
        if ATLAS_BLUR_RADIUS > 0:
            rgb = grid[:, :, :3]
            mask = (rgb.sum(2) > 1e-4).astype('float32')   # covered + margin bleed
            grid[:, :, :3] = _masked_blur(rgb, mask, ATLAS_BLUR_RADIUS, ATLAS_BLUR_PASSES) \
                * mask[:, :, None]
        rgba = grid.reshape(-1, 4)
        rgba[:, :3] = _to_display(rgba[:, :3])    # display-encode (same as the _light maps)
        rgba[:, 3] = 1.0                          # opaque light texture (JPEG has no alpha)

        label = player.get("label") or player.get("armature") or "player"
        fname = (stem or "light_atlas") + ATLAS_EXT
        adir = out_dir or os.path.join(_abs(OUT_DIR), label)
        path = os.path.join(adir, fname)
        os.makedirs(adir, exist_ok=True)
        _save_image(rgba.reshape(-1), atlas_res, atlas_res, path, colorspace='Non-Color',
                    file_format=ATLAS_FORMAT, lossless=ATLAS_LOSSLESS)
        rel = os.path.relpath(path, _abs(OUT_DIR)).replace("\\", "/")
        print(f"[ATLAS] saved {rel}")
        return rel
    finally:
        for o in parts:
            for i, mm in enumerate(saved_mats[o.name]):
                if i < len(o.material_slots):
                    o.material_slots[i].material = mm
            while len(o.material_slots) > saved_slots[o.name]:   # drop any slot we added
                o.data.materials.pop()
        for o in overlays:
            d, sh, g = saved_vis[o.name]
            o.visible_diffuse, o.visible_shadow, o.visible_glossy = d, sh, g
        for m, sr in flat_mods:
            m.show_render = sr
        scene.render.engine = saved_eng
        if saved_samp is not None:
            scene.cycles.samples = saved_samp
        if saved_den is not None:
            scene.cycles.use_denoising = saved_den
        scene.render.bake.margin = saved_margin
        if vis_restore is not None:
            vis_restore()
        bpy.ops.object.select_all(action='DESELECT')
        for o in saved_sel:
            try:
                o.select_set(True)
            except RuntimeError:
                pass
        if saved_active is not None:
            bpy.context.view_layer.objects.active = saved_active
        if bpy.data.images.get(name):
            bpy.data.images.remove(bpy.data.images[name])
        bpy.context.view_layer.update()


def _render_illum_background(players, sess, slim_value, vname):
    """Render the FULL scene (scenery+lights) with the characters in gray (already swapped
    by the caller), for the given arm variant. Saves illum_<vname>.png."""
    s = sess.s
    sess.restore_visibility()      # normal scene (drivers active, original visibility)
    saved_arms = []
    for p in players:
        arm, sv = _set_arm_style(p, slim_value)
        saved_arms.append((arm, p, sv))
    saved_fp = s.render.filepath
    saved_fmt = s.render.image_settings.file_format
    saved_q = s.render.image_settings.quality
    try:
        s.render.engine = 'CYCLES'
        if hasattr(s, 'cycles'):
            s.cycles.samples = ILLUM_SAMPLES
            s.cycles.use_denoising = True
        s.render.resolution_percentage = ILLUM_RES_PCT
        s.render.film_transparent = False     # full scene (scenery as background)
        s.render.use_compositing = False
        if sess.out_node:
            sess.out_node.mute = True
        s.render.image_settings.file_format = LIGHTSHADOW_FORMAT
        if LIGHTSHADOW_FORMAT in ('JPEG', 'WEBP'):
            s.render.image_settings.quality = JPEG_QUALITY
        path = os.path.join(_abs(OUT_DIR), "illum_" + vname + LIGHTSHADOW_EXT)
        s.render.filepath = os.path.splitext(path)[0]
        bpy.context.view_layer.update()
        print(f"[ILLUM] full scene, {vname} arms")
        bpy.ops.render.render(write_still=True)
        return path
    finally:
        s.render.filepath = saved_fp
        s.render.image_settings.file_format = saved_fmt
        s.render.image_settings.quality = saved_q
        for arm, p, sv in saved_arms:
            _restore_arm_style(arm, p, sv)


def _render_background(players, sess):
    """Render the FULL scene with the players (and optional layers) HIDDEN -- real scenery
    materials. Saves background.png. (Their cast shadows come from the shadow maps.)"""
    s = sess.s
    sess.restore_visibility()
    sess.mute_drivers(True)        # so hide_render sticks (some are driven)
    char_names = set(o.name for p in players for o in p["char_all"])
    for o in s.objects:
        if o.name in char_names:
            o.hide_render = True
    bpy.context.view_layer.update()
    saved_fp = s.render.filepath
    saved_fmt = s.render.image_settings.file_format
    saved_depth = s.render.image_settings.color_depth
    try:
        if BACKGROUND_USE_SCENE_SETTINGS and not DRAFT_MODE:
            # the user's engine/samples/denoise (snapshot taken before the batch changed them)
            s.render.engine = sess.engine
            if hasattr(s, 'cycles'):
                if sess.samples is not None:
                    s.cycles.samples = sess.samples
                if sess.denoise is not None:
                    s.cycles.use_denoising = sess.denoise
        else:
            s.render.engine = 'CYCLES'
            if hasattr(s, 'cycles'):
                s.cycles.samples = ILLUM_SAMPLES
                s.cycles.use_denoising = True
        s.render.resolution_percentage = ILLUM_RES_PCT   # align with the shadow maps
        s.render.film_transparent = False
        s.render.use_compositing = False
        if sess.out_node:
            sess.out_node.mute = True
        s.render.image_settings.file_format = 'PNG'
        s.render.image_settings.color_depth = str(PNG_BIT_DEPTH)   # '8' or '16'
        path = os.path.join(_abs(OUT_DIR), "background.png")
        s.render.filepath = os.path.splitext(path)[0]
        bpy.context.view_layer.update()
        print("[BG] full scene without players")
        bpy.ops.render.render(write_still=True)
        return path
    finally:
        s.render.filepath = saved_fp
        s.render.image_settings.file_format = saved_fmt
        s.render.image_settings.color_depth = saved_depth
        sess.restore_visibility()   # unmute drivers + original visibility


def _layer_steps(players, groups, sess, prog, layer_infos):
    """Export each optional-layer GROUP independently into <OUT_DIR>/layers/: beauty over a
    transparent background (display-encoded, same look as background.png), the shadow it
    casts on the scenery, and -- for a single-mesh group, if EXPORT_LAYER_UV -- its UV +
    light for generic retexturing. A group's meshes render TOGETHER (they self-occlude),
    so a rigged object (armature/collection of meshes) is one whole layer. Players and the
    OTHER groups stay hidden (they can be toggled off in the wallpaper, so the layer must be
    whole with respect to them), but the SCENERY OCCLUDES the layer (it is always behind the
    composite): in the beauty render the scenery is a HOLDOUT -- it cuts the alpha where it
    is in front while still lighting/shadowing the object -- and the same occlusion is ANDed
    into the UV coverage. Only players render unoccluded. Multi-mesh groups skip the UV/light
    retexture (the meshes don't share one UV/texture space)."""
    s = sess.s
    out_dir = os.path.join(_abs(OUT_DIR), "layers")
    os.makedirs(out_dir, exist_ok=True)
    char_names = set(o.name for p in players for o in p["char_all"])
    layer_mesh_names = set(m.name for g in groups for m in g["meshes"])

    def _isolate(group_meshes):
        """Only this group's meshes visible (players + other groups stay hidden)."""
        sess.restore_visibility()
        sess.mute_drivers(True)
        for o in s.objects:
            if o.name in char_names:
                o.hide_render = True
        for m in group_meshes:
            m.hide_render = False
        bpy.context.view_layer.update()

    # CLEAN scene (no players, no layers) -- the baseline for the layer shadow ratios
    clean_disp = None
    if EXPORT_SHADOW:
        sess.restore_visibility()
        sess.mute_drivers(True)
        for o in s.objects:
            if o.name in char_names:
                o.hide_render = True
        bpy.context.view_layer.update()
        clean, W, H = _render_combined_array(sess, ILLUM_RES_PCT)
        clean_disp = _to_display(clean) if SHADOW_DISPLAY_RATIO else clean[:, :3]
        yield prog("Layers: clean scene")

    for g in groups:
        meshes = g["meshes"]
        safe = g["name"]
        group_names = set(m.name for m in meshes)
        info = {"name": safe, "object": g["object"], "kind": g["kind"],
                "meshes": [m.name for m in meshes]}
        label = f"{g['object']} ({len(meshes)} mesh)" if len(meshes) > 1 else g["object"]
        # real scenery that occludes (not this group, not players/other layers -- hidden)
        scenery = [o for o in s.objects if o.type == 'MESH'
                   and o.name not in group_names and o.name not in char_names
                   and o.name not in layer_mesh_names]

        # BEAUTY: the group alone over a transparent film, OCCLUDED by the scenery. The
        # scenery is a holdout: camera rays hitting it cut the alpha (zero where it is in
        # front of the group) but it still lights/shadows the meshes -- the lighting stays
        # real. Un-premultiply in linear, then encode to display. Final look -> scene
        # settings (engine/samples/denoise), like background.png.
        _isolate(meshes)
        sc = {o: o.is_holdout for o in scenery}
        for o in scenery:
            o.is_holdout = True
        bpy.context.view_layer.update()
        try:
            print(f"[LAYER beauty] {label}")
            comb, W, H = _render_combined_array(
                sess, ILLUM_RES_PCT, transparent=True,
                use_scene_settings=BACKGROUND_USE_SCENE_SETTINGS and not DRAFT_MODE)
        finally:
            for o, c in sc.items():
                o.is_holdout = c
        alpha = np.clip(comb[:, 3], 0.0, 1.0)
        straight = np.where(alpha[:, None] > 1e-4,
                            comb[:, :3] / np.maximum(alpha[:, None], 1e-4), 0.0)
        beauty = np.empty((comb.shape[0], 4), dtype='float32')
        beauty[:, :3] = _to_display(straight)
        beauty[:, 3] = alpha
        _save_image(beauty.reshape(-1), W, H, os.path.join(out_dir, safe + ".png"))
        info["image"] = f"layers/{safe}.png"
        info["bbox"] = _bbox_dict(_topleft_bbox(alpha > 0.004, W, H), (W, H))
        occl = alpha > 0.5      # visible-pixel mask: clips the UV coverage the same way
        yield prog(f"Layer: {label}")

        # SHADOW: the group camera-invisible (still casting), scenery visible.
        if EXPORT_SHADOW:
            _isolate(meshes)
            cam_save = {m: m.visible_camera for m in meshes}
            for m in meshes:
                m.visible_camera = False
            bpy.context.view_layer.update()
            try:
                print(f"[LAYER shadow] {label}")
                comb_sh, _, _ = _render_combined_array(sess, ILLUM_RES_PCT)
            finally:
                for m, c in cam_save.items():
                    m.visible_camera = c
            sh_d = _to_display(comb_sh) if SHADOW_DISPLAY_RATIO else comb_sh[:, :3]
            ratio = np.clip(sh_d / np.clip(clean_disp, 1e-4, None), 0.0, 1.0)
            shadow = np.empty((ratio.shape[0], 4), dtype='float32')
            shadow[:, :3] = ratio
            shadow[:, 3] = 1.0
            _save_image(shadow.reshape(-1), W, H,
                        os.path.join(out_dir, safe + "_shadow" + LIGHTSHADOW_EXT),
                        file_format=LIGHTSHADOW_FORMAT)
            info["shadow"] = f"layers/{safe}_shadow{LIGHTSHADOW_EXT}"
            yield prog(f"Layer shadow: {label}")

        # UV + LIGHT (generic retexture) -- SINGLE-mesh groups only: a multi-mesh group's
        # meshes don't share one UV/texture space, so a combined UV is meaningless (the
        # beauty already bakes their real materials). gray render for the light (only if the
        # Illum toggle is on), then the UV pass clipped by the beauty's occlusion.
        if EXPORT_LAYER_UV and len(meshes) == 1:
            ly = meshes[0]
            lmap = None
            if EXPORT_ILLUM:
                _isolate(meshes)
                sv = _swap_materials([ly], _gray_diffuse_material())
                # light must be UNOCCLUDED (camera-invisible scenery, still lighting): the
                # occluded pixels are simply outside the coverage and never sampled.
                sc = {o: o.visible_camera for o in scenery}
                for o in scenery:
                    o.visible_camera = False
                bpy.context.view_layer.update()
                try:
                    print(f"[LAYER light] {ly.name}")
                    comb_l, _, _ = _render_combined_array(sess, ILLUM_RES_PCT)
                    lmap = _lin_to_srgb(comb_l[:, :3]).astype('float32')
                finally:
                    for o, c in sc.items():
                        o.visible_camera = c
                    _restore_materials(sv)
            dr = None
            if UV_DEPTH_IN_BLUE:
                dr, reason = _rendered_depth_range({"uv_parts": [ly]}, sess,
                                                   _opaque_mask_material())
                if dr is None:   # retry once (transient empty Viewer); non-fatal for layers
                    print(f"[depth] layer '{safe}': depth range failed -- {reason}. Retry...")
                    dr, reason = _rendered_depth_range({"uv_parts": [ly]}, sess,
                                                       _opaque_mask_material())
                if dr is None:
                    print(f"[depth] layer '{safe}': no depth range ({reason}); the layer "
                          f"will composite by camera_depth (draw order), not per-pixel.")
            # Same scale/key as the players' "depth_range_viewer": the UV's B decodes to an
            # absolute (Viewer-scale) depth via zmin + B*(zmax-zmin) -- comparable across
            # players and layers for per-pixel depth-checked compositing.
            info["depth_range_viewer"] = ([round(x, 5) for x in dr] if dr else None)
            sv = _swap_materials([ly], _opaque_mask_material())
            try:
                tag = ("_UVDL" if (UV_FORMAT == 'OPEN_EXR' and lmap is not None)
                       else "_UV")
                res = export_part_uv(ly, sess, "layers", tag=tag, label=safe,
                                     depth_range=dr, light_map=lmap, occlusion=occl)
            finally:
                _restore_materials(sv)
            if res is not None:
                info["uv"] = f"layers/{os.path.basename(res[0])}"
                # res[3] is the masked light actually used; None if the light render's
                # resolution didn't match the UV render (then no light file was written).
                if UV_FORMAT != 'OPEN_EXR' and EXPORT_PART_LIGHT and res[3] is not None:
                    info["light"] = f"layers/{safe}_light{LIGHTSHADOW_EXT}"
            else:
                print(f"[LAYER uv] {ly.name}: no UV coverage (missing UV map?), skipped")
            yield prog(f"Layer UV: {ly.name}")

        cd = _group_camera_depth(meshes)
        info["camera_depth"] = round(cd, 4) if cd is not None else None
        layer_infos.append(info)


# --- Tier-2 cooperative render ---------------------------------------------------------------
# The render primitives YIELD this sentinel where they used to call bpy.ops.render.render() inline.
# A DRIVER decides how to run it: _drain_render() (sync, below) runs a blocking EXEC render -- the
# current behaviour, used by execute()/headless/MCP and the non-streamed callers (animated export,
# atlas bake, depth probe). The static modal operator instead runs each as a non-blocking
# INVOKE_DEFAULT job so the UI stays live and Cycles shows its own sample progress.
_RENDER = object()


def _drain_render(gen):
    """Run a render-yielding generator to completion SYNCHRONOUSLY: each `_RENDER` it yields becomes
    a blocking `bpy.ops.render.render()`. Returns the generator's return value. Progress tuples it
    forwards (from a nested streaming gen) are ignored on the sync path."""
    try:
        x = next(gen)
        while True:
            if x is _RENDER:
                bpy.ops.render.render(write_still=False)
                x = gen.send(None)
            else:
                x = next(gen)
    except StopIteration as e:
        return e.value


def _render_combined_array(sess, res_pct, transparent=False, use_scene_settings=False):
    """SYNC wrapper around _render_combined_array_gen (drains it with blocking renders). Existing
    callers -- animated export, atlas bake, depth probe, the execute() path -- keep calling this and
    behave exactly as before. The static modal export uses `yield from _render_combined_array_gen(...)`
    so each render runs as a live INVOKE job instead."""
    return _drain_render(_render_combined_array_gen(sess, res_pct, transparent, use_scene_settings))


def _render_combined_array_gen(sess, res_pct, transparent=False, use_scene_settings=False):
    """Render the Combined pass (beauty, with denoise) -> linear RGBA array, read via the Viewer.
    GENERATOR: yields `_RENDER` where the blocking render used to be (incl. the empty-Viewer retry),
    reads the Viewer when resumed, and RETURNS (arr, W, H). transparent=True renders over a
    transparent film (alpha = coverage). use_scene_settings=True uses the USER's engine/samples/
    denoise (from the _Session snapshot) -- for final-look images (layer beauty), like background.png."""
    s = sess.s
    ng = sess.ng
    vin = sess.viewer.inputs[0]
    for l in list(vin.links):
        ng.links.remove(l)
    img_sock = sess.rl.outputs.get('Image')
    ng.links.new(img_sock, vin)
    if sess.out_node:
        sess.out_node.mute = True
    sess.ensure_group_output()
    if use_scene_settings:
        s.render.engine = sess.engine
        if hasattr(s, 'cycles'):
            if sess.samples is not None:
                s.cycles.samples = sess.samples
            if sess.denoise is not None:
                s.cycles.use_denoising = sess.denoise
    else:
        s.render.engine = 'CYCLES'
        if hasattr(s, 'cycles'):
            s.cycles.samples = ILLUM_SAMPLES
            s.cycles.use_denoising = True
    s.render.resolution_percentage = res_pct
    s.render.film_transparent = transparent
    s.render.use_compositing = True
    bpy.context.view_layer.update()
    yield _RENDER
    vimg = bpy.data.images.get('Viewer Node')
    if not vimg or len(vimg.pixels) == 0:
        # transient empty Viewer (same hiccup the depth probe retries) -- retry once
        print("[render] empty Viewer node on the combined render -- retrying once")
        yield _RENDER
        vimg = bpy.data.images.get('Viewer Node')
        if not vimg or len(vimg.pixels) == 0:
            raise RuntimeError("Viewer node empty after retry (combined render)")
    W, H = vimg.size
    a = np.empty(len(vimg.pixels), dtype='float32')
    vimg.pixels.foreach_get(a)
    return a.reshape(-1, 4), W, H


def _load_gray_channel(path, W, H):
    if not os.path.exists(path):
        return None
    img = bpy.data.images.load(path, check_existing=False)
    iw, ih = img.size
    a = np.empty(len(img.pixels), dtype='float32')
    img.pixels.foreach_get(a)
    bpy.data.images.remove(img)
    if (iw, ih) != (W, H):
        return None
    return a.reshape(-1, 4)[:, 0]


def _illum_shadow_steps(players, sess, gray_mat, prog, light_maps=None):
    """Generator form of export_player_illum_shadow: yields prog(msg) after the clean
    render and after each player/variant, so a modal operator can show progress."""
    s = sess.s
    all_char = [o for p in players for o in p["char_all"]]
    char_names = set(o.name for o in all_char)

    # CLEAN scene (all players hidden) -- the shadow ratio's baseline (SHADOW only)
    clean = None
    W = H = None
    if EXPORT_SHADOW:
        sess.restore_visibility()
        sess.mute_drivers(True)
        for o in s.objects:
            if o.name in char_names:
                o.hide_render = True
        bpy.context.view_layer.update()
        clean, W, H = _render_combined_array(sess, ILLUM_RES_PCT)
        yield prog("Shadow: clean scene")

    # Base-layer label set per arm variant (the base light and the shadow are base-only).
    base_sets = {v: {lab.format(variant=v) for lab in COMPOSITE_BASE_LABELS}
                 for v, _ in MASK_ARM_VARIANTS}

    saved_vd = {o.name: o.visible_diffuse for o in all_char}
    saved_mats = _swap_materials(all_char, gray_mat)   # gray; only the active player shows
    try:
        for p in players:
            for vname, vval in MASK_ARM_VARIANTS:
                sess.restore_visibility()
                arm, saved_arm = _set_arm_style(p, vval)
                others = set(o.name for op in players if op is not p for o in op["char_all"])
                muted = []
                for o in s.objects:
                    if o.name in others:
                        if o.animation_data:
                            for d in o.animation_data.drivers:
                                if d.data_path == "hide_render" and not d.mute:
                                    d.mute = True
                                    muted.append(d)
                        o.hide_render = True
                if ILLUM_PURE_SHADOW:
                    for o in p["char_all"]:
                        o.visible_diffuse = False   # pure shadow, but loses self-bounce
                active_names = set(o.name for o in p["char_all"])
                scenery = ([o for o in s.objects
                            if o.type == 'MESH' and o.name not in active_names]
                           if ILLUM_HIDE_SCENERY_FROM_CAMERA else [])
                bpy.context.view_layer.update()
                comb_full = comb_base = comb_sh = None
                try:
                    # FULL light (illum): overlays visible, scenery invisible to the camera
                    # (player not occluded by foreground; their light/shadow stays).
                    if EXPORT_ILLUM:
                        sc = {o: o.visible_camera for o in scenery}
                        for o in scenery:
                            o.visible_camera = False
                        bpy.context.view_layer.update()
                        try:
                            print(f"[ILLUM full] {p['label']} / {vname}")
                            comb_full, W, H = _render_combined_array(sess, ILLUM_RES_PCT)
                        finally:
                            for o, c in sc.items():
                                o.visible_camera = c

                    # Hide the overlays (hat/jacket/sleeve/pant): base light + shadow are
                    # base-only.
                    base_set = base_sets[vname]
                    labels = p.get("uv_labels") or {}
                    overlays = [o for o in p["uv_parts"]
                                if o.name in labels and labels[o.name] not in base_set]
                    sh_muted, sh_hidden = [], []
                    for o in overlays:
                        if o.animation_data:
                            for d in o.animation_data.drivers:
                                if d.data_path == "hide_render" and not d.mute:
                                    d.mute = True
                                    sh_muted.append(d)
                        sh_hidden.append((o, o.hide_render))
                        o.hide_render = True
                    bpy.context.view_layer.update()
                    try:
                        if EXPORT_ILLUM:
                            # BASE light: base visible, scenery invisible to the camera.
                            sc = {o: o.visible_camera for o in scenery}
                            for o in scenery:
                                o.visible_camera = False
                            bpy.context.view_layer.update()
                            try:
                                print(f"[ILLUM base] {p['label']} / {vname}")
                                comb_base, W, H = _render_combined_array(sess, ILLUM_RES_PCT)
                            finally:
                                for o, c in sc.items():
                                    o.visible_camera = c
                        if EXPORT_SHADOW:
                            # SHADOW: base INVISIBLE to the camera (still casting), scenery
                            # VISIBLE (the shadow falls on it).
                            sh_cam = {o: o.visible_camera for o in p["char_all"]}
                            for o in p["char_all"]:
                                o.visible_camera = False
                            bpy.context.view_layer.update()
                            try:
                                print(f"[SHADOW] {p['label']} / {vname} (base only, no-camera)")
                                comb_sh, W, H = _render_combined_array(sess, ILLUM_RES_PCT)
                            finally:
                                for o, c in sh_cam.items():
                                    o.visible_camera = c
                    finally:
                        for o, hr in sh_hidden:
                            o.hide_render = hr
                        for d in sh_muted:
                            d.mute = False
                finally:
                    for o in p["char_all"]:
                        if o.name in saved_vd:
                            o.visible_diffuse = saved_vd[o.name]
                    for d in muted:
                        d.mute = False
                    _restore_arm_style(arm, p, saved_arm)

                # ILLUM outputs: full-body illum image (masked) + per-layer light maps
                if EXPORT_ILLUM:
                    mpath = os.path.join(_abs(OUT_DIR), p["label"], f"mask_{vname}.png")
                    mask = _load_gray_channel(mpath, W, H)
                    body = (mask > 0.5) if mask is not None else np.zeros(W * H, dtype=bool)
                    illum = comb_full.copy()
                    illum[~body] = 0.0
                    illum[:, 3] = 1.0
                    _save_image(illum.reshape(-1), W, H,
                                os.path.join(_abs(OUT_DIR), p["label"],
                                             f"illum_{vname}{LIGHTSHADOW_EXT}"),
                                colorspace=ILLUM_COLORSPACE, file_format=LIGHTSHADOW_FORMAT)
                    if light_maps is not None:
                        # uint8: these stay in RAM for the whole batch (2 maps per player/
                        # variant; float32 would be ~800 MB at 4K with 2 players) and end up
                        # in 8-bit files anyway. export_part_uv converts back to float.
                        light_maps[(p["label"], vname)] = {
                            "base": (_lin_to_srgb(comb_base[:, :3]) * 255.0 + 0.5).astype('uint8'),
                            "full": (_lin_to_srgb(comb_full[:, :3]) * 255.0 + 0.5).astype('uint8'),
                        }

                # SHADOW output: ratio vs the clean scene (no body masking -- the body isn't
                # in the render, so the contact shadow under/around it is kept). In DISPLAY
                # space (view-transformed) so a multiply onto the display background matches.
                if EXPORT_SHADOW:
                    if SHADOW_DISPLAY_RATIO:
                        sh_d, cl_d = _to_display(comb_sh), _to_display(clean)
                    else:
                        sh_d, cl_d = comb_sh[:, :3], clean[:, :3]
                    ratio = np.clip(sh_d / np.clip(cl_d, 1e-4, None), 0.0, 1.0)
                    shadow = np.empty((ratio.shape[0], 4), dtype='float32')
                    shadow[:, :3] = ratio
                    shadow[:, 3] = 1.0
                    _save_image(shadow.reshape(-1), W, H,
                                os.path.join(_abs(OUT_DIR), p["label"],
                                             f"shadow_{vname}{LIGHTSHADOW_EXT}"),
                                file_format=LIGHTSHADOW_FORMAT)
                yield prog(f"Light: {p['label']} / {vname}")
    finally:
        _restore_materials(saved_mats)


def export_player_illum_shadow(players, sess, gray_mat):
    """Per player and arm variant: body illum (masked) + shadow (ratio vs the clean
    scene), all from ONE render per player/variant (player in gray, others hidden).
    Precondition: the masks have already been generated (same variant/resolution)."""
    for _ in _illum_shadow_steps(players, sess, gray_mat, lambda msg=None: None):
        pass


def _player_camera_depth(player):
    """Average depth of the player (camera distance to the centroid), in WORLD units --
    comparable across rigs, to order them back to front. Render-free."""
    import mathutils
    cam = bpy.context.scene.camera
    if cam is None:
        return None
    inv = cam.matrix_world.inverted()
    dg = bpy.context.evaluated_depsgraph_get()
    centers = []
    for o in player["uv_parts"]:
        if o.type != 'MESH':
            continue
        ev = o.evaluated_get(dg)
        mw = ev.matrix_world
        c = mathutils.Vector()
        for corner in ev.bound_box:
            c += mw @ mathutils.Vector(corner)
        centers.append(c / 8.0)
    if not centers:
        return None
    centroid = mathutils.Vector()
    for c in centers:
        centroid += c
    centroid /= len(centers)
    return float(-(inv @ centroid).z)   # camera depth (larger = further back)


def _topleft_bbox(flat_mask, W, H):
    """Bbox (x0,y0,x1,y1 inclusive, TOP-LEFT pixels, matching the saved PNG) of the True
    pixels in a flat bottom-left mask (the Viewer array is bottom-left). None if empty."""
    ys, xs = np.nonzero(np.asarray(flat_mask).reshape(H, W))
    if not xs.size:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    rb0, rb1 = int(ys.min()), int(ys.max())
    return (x0, H - 1 - rb1, x1, H - 1 - rb0)


def _bbox_dict(bbox, size):
    """Format an inclusive top-left pixel bbox (x0,y0,x1,y1) + (W,H) for the manifest:
    pixels {x,y,w,h} and normalized {x,y,w,h} (0..1), origin top-left. None if no bbox."""
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0 + 1, y1 - y0 + 1
    out = {"origin": "top-left", "px": {"x": x0, "y": y0, "w": w, "h": h}}
    if size and size[0] and size[1]:
        W, H = size
        out["resolution"] = [W, H]
        out["norm"] = {"x": round(x0 / W, 5), "y": round(y0 / H, 5),
                       "w": round(w / W, 5), "h": round(h / H, 5)}
    return out


def _write_manifest(players, out_path, layer_infos=None):
    s = bpy.context.scene
    ordered = sorted((p for p in players if p.get("camera_depth") is not None),
                     key=lambda p: p["camera_depth"], reverse=True)  # back -> front
    # back-to-front order mixing players and optional layers (by camera depth)
    entries = ([(p["label"], p["camera_depth"]) for p in players
                if p.get("camera_depth") is not None]
               + [(li["name"], li["camera_depth"]) for li in (layer_infos or [])
                  if li.get("camera_depth") is not None])
    draw_order = [name for name, _ in sorted(entries, key=lambda e: e[1], reverse=True)]
    # Effective EXPORT resolution (what every saved image actually measures): the real
    # render size when available, else base * res_pct. The base scene resolution is kept
    # separately -- consumers must compare files against "resolution".
    eff = next((tuple(p["render_size"]) for p in players if p.get("render_size")), None)
    if eff is None:
        eff = (s.render.resolution_x * MASK_RES_PCT // 100,
               s.render.resolution_y * MASK_RES_PCT // 100)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "addon_version": ADDON_VERSION,
        "render": {
            "resolution": list(eff),
            "base_resolution": [s.render.resolution_x, s.render.resolution_y],
            "engine": s.render.engine,
            "draft": DRAFT_MODE or None,
            "res_pct": MASK_RES_PCT,
            "uv_samples": UV_SAMPLES,
            "mask_samples": MASK_SAMPLES,
            "mask_res_pct": MASK_RES_PCT,
            "uv_depth_in_blue": UV_DEPTH_IN_BLUE,
            "export_backface_uv": EXPORT_BACKFACE_UV,
            "uv_file_suffix": ("_UVDL" if (UV_FORMAT == 'OPEN_EXR'
                                           and EXPORT_ILLUM) else "_UV"),
            "uv_format": UV_FORMAT,
            "uv_ext": UV_EXT,
            "uv_channels": (
                ("R=U, G=V, B=depth(0=near,1=far) normalized per character"
                 if UV_DEPTH_IN_BLUE else "R=U, G=V, B=1.0")
                + ", A=coverage (1 in part, 0 outside)"
                + (" + light layer (light.R/G/B, sRGB)"
                   if (UV_FORMAT == 'OPEN_EXR' and EXPORT_ILLUM) else "")),
            "uv_png_texel_bins": (UV_TEXEL_BINS
                                  if (UV_FORMAT != 'OPEN_EXR' and UV_PNG_FLOOR_TEXELS)
                                  else None),
            "uv_decode_note": ("PNG/WebP U/V byte = floor(u * %d); texel = floor(byte * "
                               "texW / %d) (== byte for a %d-wide skin). WebP is lossless "
                               "(quality 100). EXR stores the raw float."
                               % (UV_TEXEL_BINS, UV_TEXEL_BINS, UV_TEXEL_BINS)),
            "depth_decode_note": ("absolute depth = zmin + B*(zmax - zmin), with "
                                  "[zmin, zmax] = depth_range_viewer (players and layers "
                                  "share the same scale -> comparable for depth-checked "
                                  "compositing)"),
            "base_layer": ([COMPOSITE_OUTPUT_NAME.format(variant=v) + UV_EXT
                            for v, _ in MASK_ARM_VARIANTS] if COMPOSITE_BASE_LAYER else None),
            "base_layer_parts": (COMPOSITE_BASE_LABELS if COMPOSITE_BASE_LAYER else None),
            "png_bit_depth": PNG_BIT_DEPTH,
            "lightshadow_format": LIGHTSHADOW_FORMAT,
            "illum_backgrounds": ([f"illum_{v}{LIGHTSHADOW_EXT}" for v, _ in MASK_ARM_VARIANTS]
                                  if EXPORT_ILLUM_BACKGROUND else None),
            "background": ("background.png" if EXPORT_BACKGROUND else None),
            "player_illum_shadow": ({
                "illum": ([f"illum_{v}{LIGHTSHADOW_EXT}" for v, _ in MASK_ARM_VARIANTS]
                          if EXPORT_ILLUM else None),
                "shadow": ([f"shadow_{v}{LIGHTSHADOW_EXT}" for v, _ in MASK_ARM_VARIANTS]
                           if EXPORT_SHADOW else None),
                "note": ("per player (in the subfolder); shadow is a DISPLAY-space multiply "
                         "(1=no shadow) -- multiply it onto the display background."
                         if SHADOW_DISPLAY_RATIO else
                         "per player; shadow is a LINEAR multiply (1=no shadow)"),
            } if (EXPORT_ILLUM or EXPORT_SHADOW) else None),
        },
        "players": [
            {
                "label": p["label"],
                "armature": p.get("armature"),
                "rig_id": p.get("rig_id"),
                "folder": p["label"],
                "visible_bbox": _bbox_dict(p.get("bbox"), p.get("render_size")),
                "camera_depth": round(p["camera_depth"], 4) if p.get("camera_depth") is not None else None,
                "depth_range_viewer": ([round(x, 5) for x in p["depth_range"]]
                                       if p.get("depth_range") else None),
                "uv_parts": {lab: obj for obj, lab in
                             sorted((p.get("uv_labels") or {o.name: o.name for o in p["uv_parts"]}).items(),
                                    key=lambda kv: kv[1])},
                "part_bboxes": {lab: _bbox_dict(bb, p.get("render_size"))
                                for lab, bb in sorted((p.get("part_bboxes") or {}).items())},
                "base_layer": ([COMPOSITE_OUTPUT_NAME.format(variant=v) + UV_EXT
                                for v, _ in MASK_ARM_VARIANTS] if COMPOSITE_BASE_LAYER else None),
                "masks": [v for v, _ in MASK_ARM_VARIANTS],
            }
            for p in players
        ],
        "layers": (layer_infos or None),
        "layers_note": ("each layer is a GROUP of meshes (marked collection/armature/mesh) "
                        "rendered together; beauty/UV are OCCLUDED by the scenery (players "
                        "and other layers excluded -- they can be toggled off); only "
                        "players render unoccluded. Multi-mesh groups have no UV/light "
                        "(meshes don't share one texture space)" if layer_infos else None),
        "draw_order_back_to_front": draw_order or [p["label"] for p in ordered],
    }
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return manifest


def _preflight(players):
    """Check preconditions; return a list of blocking error messages (empty = OK)."""
    errors = []
    if getattr(bpy.app, "background", False):
        errors.append("Running headless (--background). This script reads via the Viewer "
                      "node and needs a GUI; run Blender without --background.")
    if not bpy.data.filepath:
        errors.append("The .blend file is not saved. OUT_DIR uses '//' (relative to the "
                      ".blend), so save the file first.")
    if bpy.context.scene.camera is None:
        errors.append("The scene has no active camera (scene.camera) -- set one.")
    if not players:
        msg = f"No player found: no armature has the custom property '{RIG_ID_PROP}'"
        if RIG_ID_VALUE is not None:
            msg += f" == '{RIG_ID_VALUE}'"
        errors.append(msg + ".")
    else:
        empty = [p["label"] for p in players if not p["uv_parts"]]
        if empty:
            errors.append("These players have no selectable UV parts (check the rig "
                          f"structure / CONFIG): {empty}")
    return errors


def _render_steps(players, op=None):
    """Generator that performs the full export, yielding (fraction_0_to_1, message) after
    each unit of work. Returns the results dict (as StopIteration.value). Everything runs
    inside try/finally, so closing the generator (modal cancel) still restores the scene."""
    if FIX_2LAYER_POSITION:
        _fix_2layer_positions(players)
    # Force the "always export" toggles (Second layer) ON, and make manually render-hidden
    # parts visible -- BEFORE the _Session snapshot, so masks/illum render them. Both are put
    # back in the finally below.
    forced_props = _force_selection_props_on(players)
    forced_visible = _force_player_parts_visible(players)
    for p in players:
        os.makedirs(os.path.join(_abs(OUT_DIR), p["label"]), exist_ok=True)

    sess = _Session()
    results = {p["label"]: {"uv": {}, "masks": {}} for p in players}

    # Optional layers (as groups): kept hidden through the WHOLE player pipeline +
    # background (their cast shadow/look is exported independently in step 6). force_hidden
    # makes every restore_visibility() keep them hidden; the final sess.restore() puts them
    # back. A group can be many meshes (a marked armature/collection) -> all hidden together.
    layer_groups = discover_layers()
    layer_meshes = [m for g in layer_groups for m in g["meshes"]]
    sess.force_hidden = {m.name for m in layer_meshes}
    for m in layer_meshes:
        m.hide_render = True
    if layer_groups:
        bpy.context.view_layer.update()
        print("[LAYERS] optional layers: "
              + ", ".join(f"{g['object']}[{len(g['meshes'])}]" for g in layer_groups))

    # Opaque override active during the WHOLE process (UV and mask): the skin has alpha
    # (HASHED + alpha from a Math node), which would punch holes in both the UV pass and the
    # Object Index where the texture is transparent. UV/Object Index ignore color -> only
    # opacity matters.
    mask_mat = _opaque_mask_material()
    back_mat = _backface_material()
    all_char = [o for p in players for o in p["char_all"]]

    # Progress accounting (one tick per yielded step below).
    n_parts = sum(len(p["uv_parts"]) for p in players)
    n_variants = len(MASK_ARM_VARIANTS)
    total = (len(players)                                          # depth ranges
             + len(players) * n_variants                           # masks
             + (((1 if EXPORT_SHADOW else 0) + len(players) * n_variants)
                if (EXPORT_ILLUM or EXPORT_SHADOW)
                else (n_variants if EXPORT_ILLUM_BACKGROUND else 0))  # illum/shadow
             + n_parts                                             # front UVs
             + (len(players) * n_variants if COMPOSITE_BASE_LAYER else 0)  # base composites
             + (n_parts if EXPORT_BACKFACE_UV else 0)              # back UVs
             + (1 if EXPORT_BACKGROUND else 0)          # background
             + (((1 if EXPORT_SHADOW else 0)                       # layers: clean baseline
                 + sum(1 + (1 if EXPORT_SHADOW else 0)             # beauty + shadow
                       + (1 if (EXPORT_LAYER_UV and len(g["meshes"]) == 1) else 0)  # UV
                       for g in layer_groups))
                if layer_groups else 0)                            # optional layers
             + 1)                                                  # manifest
    total = max(total, 1)
    state = {"done": 0}

    def prog(msg):
        state["done"] += 1
        print(f"[{state['done']}/{total}] {msg}")
        return (state["done"] / total, msg)

    # light_maps[(player_label, variant)] = sRGB RGB light (N,3), filled by the illum step
    # and embedded as a 'light' layer in EXR UVs. Empty if the illum step is disabled.
    light_maps = {}
    have_light = EXPORT_ILLUM
    embed_light = (UV_FORMAT == 'OPEN_EXR' and have_light)   # light goes inside the EXR
    front_tag = "_UVDL" if embed_light else "_UV"

    try:
        # 0) Depth range per player (for depth in the B channel), in the Viewer's scale.
        #    Retry once and ABORT on persistent failure -- a None range here silently
        #    flattens the B channel and breaks depth occlusion (see _depth_range_or_abort).
        for p in players:
            p["depth_range"] = (_depth_range_or_abort(p, sess, mask_mat, back_mat,
                                                      f"player '{p['label']}'")
                                if UV_DEPTH_IN_BLUE else None)
            p["uv_labels"] = _assign_part_labels(p["uv_parts"], label=p["label"])
            yield prog(f"Depth range: {p['label']}")
        # 1) Character MASKS (opaque override) -- generated BEFORE illum (the shadow/illum
        #    step reads them to mask the body).
        saved_mats = _swap_materials(all_char, mask_mat)
        try:
            for p in players:
                for vname, vval in MASK_ARM_VARIANTS:
                    mpath, bbox, mW, mH = \
                        export_character_mask_variant(p, vname, vval, sess, players)
                    results[p["label"]]["masks"][vname] = mpath
                    if bbox is not None:
                        p["render_size"] = (mW, mH)
                        b = p.get("bbox")
                        p["bbox"] = list(bbox) if b is None else [
                            min(b[0], bbox[0]), min(b[1], bbox[1]),
                            max(b[2], bbox[2]), max(b[3], bbox[3])]   # union over variants
                    yield prog(f"Mask: {p['label']} / {vname}")
        finally:
            _restore_materials(saved_mats)
        # 2) ILLUM (light) + shadow FIRST, so the light can be packed into the UV alpha.
        if EXPORT_ILLUM or EXPORT_SHADOW:
            yield from _illum_shadow_steps(players, sess, _gray_diffuse_material(), prog,
                                           light_maps)
        elif EXPORT_ILLUM_BACKGROUND:   # global illum (all together) -- legacy path
            gd = _gray_diffuse_material()
            saved_il = _swap_materials(all_char, gd)
            try:
                for vname, vval in MASK_ARM_VARIANTS:
                    _render_illum_background(players, sess, vval, vname)
                    yield prog(f"Illum background: {vname}")
            finally:
                _restore_materials(saved_il)
        # 3) FRONT-face UVs (opaque override): R=U, G=V, B=depth, A=coverage (+ a 'light'
        #    layer in EXR). Then composite the base layers per player (nearest pixel wins).
        variant_names = [v for v, _ in MASK_ARM_VARIANTS]
        base_sets = {v: {lab.format(variant=v) for lab in COMPOSITE_BASE_LABELS}
                     for v in variant_names}
        saved_mats = _swap_materials(all_char, mask_mat)
        try:
            for p in players:
                comps = {}            # variant -> [comp (N,4), zbuf (N,), light (N,3)|None]
                cW = cH = None
                for part in p["uv_parts"]:
                    lab = p["uv_labels"][part.name]
                    lmap = (_light_for_label(light_maps, p["label"], lab)
                            if have_light else None)
                    res = export_part_uv(part, sess, p["label"], tag=front_tag, label=lab,
                                         depth_range=p["depth_range"], light_map=lmap)
                    if res is None:
                        yield prog(f"UV front: {p['label']} / {lab} (empty)")
                        continue
                    path, out, cov, light, cW, cH = res
                    results[p["label"]]["uv"][part.name] = path
                    p.setdefault("part_bboxes", {})[lab] = _topleft_bbox(cov, cW, cH)
                    p["render_size"] = (cW, cH)
                    if COMPOSITE_BASE_LAYER:
                        for v in variant_names:
                            if lab not in base_sets[v]:
                                continue
                            c = comps.get(v)
                            if c is None:
                                c = [np.zeros((cW * cH, 4), dtype='float32'),
                                     np.full(cW * cH, np.inf, dtype='float32'),
                                     (np.zeros((cW * cH, 3), dtype='float32')
                                      if have_light else None)]
                                comps[v] = c
                            comp, zbuf, clight = c
                            d = np.where(cov, out[:, 2], np.inf)   # nearest (0=near) wins
                            win = d < zbuf
                            comp[win] = out[win]
                            zbuf[win] = d[win]
                            if clight is not None and light is not None:
                                clight[win] = light[win]
                    yield prog(f"UV front: {p['label']} / {lab}")
                if COMPOSITE_BASE_LAYER:
                    for v in variant_names:
                        c = comps.get(v)
                        if c is None:
                            continue
                        comp, zbuf, clight = c
                        stem = COMPOSITE_OUTPUT_NAME.format(variant=v)
                        cpath = os.path.join(_abs(OUT_DIR), p["label"], stem + UV_EXT)
                        if UV_FORMAT == 'OPEN_EXR' and clight is not None:
                            _save_exr_uvdl(comp, clight, cW, cH, cpath)
                            _remove_stale_variants(cpath)
                        else:
                            _save_image(comp.reshape(-1), cW, cH, cpath,
                                        file_format=UV_FORMAT, lossless=True)
                            if clight is not None and EXPORT_PART_LIGHT:
                                la = np.ones((clight.shape[0], 4), dtype='float32')
                                la[:, :3] = clight
                                _save_image(la.reshape(-1), cW, cH,
                                            os.path.join(_abs(OUT_DIR), p["label"],
                                                         stem + "_light" + LIGHTSHADOW_EXT),
                                            colorspace='Non-Color', file_format=LIGHTSHADOW_FORMAT)
                        results[p["label"]].setdefault("composite", {})[v] = cpath
                        yield prog(f"Composite base_layer ({v}): {p['label']}")
        finally:
            _restore_materials(saved_mats)
        # 4) BACK-face UVs (backface-only material) -> <part>_UV_back.png (no light)
        if EXPORT_BACKFACE_UV:
            saved_mats = _swap_materials(all_char, back_mat)
            try:
                for p in players:
                    for part in p["uv_parts"]:
                        lab = p["uv_labels"][part.name]
                        res = export_part_uv(part, sess, p["label"], tag="_UV_back",
                                             label=lab, depth_range=p["depth_range"])
                        if res is not None:
                            results[p["label"]]["uv"][part.name + "_back"] = res[0]
                        yield prog(f"UV back: {p['label']} / {lab}")
            finally:
                _restore_materials(saved_mats)
        # 5) background of the scene without players/layers (empty scene)
        if EXPORT_BACKGROUND:
            _render_background(players, sess)
            yield prog("Background")
        # 6) optional layers (marked scenery objects/rigs/collections), each as one group
        layer_infos = []
        if layer_groups:
            yield from _layer_steps(players, layer_groups, sess, prog, layer_infos)
        # 7) manifest with export details + average depth to order the rigs
        for p in players:
            p["camera_depth"] = _player_camera_depth(p)
        _write_manifest(players, os.path.join(_abs(OUT_DIR), "manifest.json"),
                        layer_infos)
        yield prog("Manifest")
    finally:
        sess.restore()
        for o in forced_visible:   # put back the original manual hide_render
            o.hide_render = True
        _restore_selection_props(forced_props)   # restore the artist's Second-layer toggle
    return results


def render_all(op=None, draft=False):
    """Synchronous full run (UI is blocked while it works). Returns the results dict, or
    None if the preflight aborts. The menu uses the modal operator instead (see invoke)."""
    _apply_settings(bpy.context.scene, draft=draft)
    players = discover_players()
    errors = _preflight(players)
    if errors:
        print("\n" + "=" * 64)
        print("render_uv_mask: ABORTED -- preflight checks failed:")
        for e in errors:
            print("  * " + e)
        print("=" * 64 + "\n")
        if op is not None:
            for e in errors:
                op.report({'ERROR'}, e)
        return None
    print(f"Players found: {[p['label'] for p in players]}")
    gen = _render_steps(players, op=op)
    results = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        results = e.value
    except Exception as ex:
        # an abort (e.g. depth range unavailable) -- the generator's finally already
        # restored the scene; report cleanly instead of a raw Python traceback.
        print(f"\nrender_uv_mask: ABORTED -- {ex}\n")
        if op is not None:
            op.report({'ERROR'}, str(ex))
        return None
    print("Done. Players:",
          {k: {"uv": len(v["uv"]), "masks": list(v["masks"].keys())} for k, v in results.items()})
    if op is not None:
        op.report({'INFO'}, f"NovaSkin: exported {len(players)} player(s) -> {OUT_DIR}")
    return results


# ----------------------- ANIMATED EXPORT (docs/animated-export-plan.md) -----------------------

def _anim_frame_cached(adir, i):
    """True if frame i's four PNGs already exist (non-empty) -- resume skips re-rendering."""
    fn = f"{i + 1:04d}.png"
    return all(os.path.exists(p) and os.path.getsize(p) > 0
               for p in (os.path.join(adir, "seq", k, fn)
                         for k in ("bg", "fg", "fg_matte", "light")))


def _anim_dilate_light(rgb, covered, W, H, passes=None):
    """Pad the light outward: fill uncovered pixels with the nearest covered color
    (iterative 4-neighbour dilation, `passes` px). Keeps player-edge sampling from
    reading black; the background is never affected (the light is only sampled by
    player fragments). rgb (N,3) flat, covered (N,) bool -> filled rgb."""
    if passes is None:
        passes = ANIM_LIGHT_PAD
    if passes <= 0:
        return rgb
    img = rgb.reshape(H, W, 3).copy()
    m = covered.reshape(H, W).copy()
    for _ in range(passes):
        if m.all():
            break
        for axis, shift in ((0, 1), (0, -1), (1, 1), (1, -1)):
            sm = np.roll(m, shift, axis)
            sc = np.roll(img, shift, axis)
            take = (~m) & sm
            img[take] = sc[take]
            m |= take
    return img.reshape(-1, 3)


def _anim_collect_static(players, W, H):
    """Build the static mesh buffers at the CURRENT frame: weld vertices by
    (part, vertex, uv) for the GPU buffers, and map each welded vert to a UNIQUE
    (part, vertex) position index -- anim.bin only carries unique positions, mesh.bin
    carries the static src (welded -> unique) map. Players are emitted back-to-front
    (painter's order). Returns the dict used by _anim_frame_positions/_anim_write_mesh."""
    dg = bpy.context.evaluated_depsgraph_get()
    ordered = sorted(players, key=lambda p: _player_camera_depth(p) or 0.0, reverse=True)
    base_set = set(ANIM_BASE_LABELS)
    st = {"parts": [], "uv": [], "src": [], "tris": [], "players": [],
          "uniq": []}                                  # uniq: list of (part_idx, vert_idx)
    weld, uniq_keys = {}, {}
    for p in ordered:
        labs = p.get("uv_labels") or _assign_part_labels(p["uv_parts"], label=p["label"])
        parts = sorted([o for o in p["uv_parts"] if labs.get(o.name) in base_set],
                       key=lambda o: o.name)
        w0, t0 = len(st["src"]), len(st["tris"]) // 3
        for o in parts:
            pi = len(st["parts"]); st["parts"].append(o.name)
            ev = o.evaluated_get(dg); me = ev.to_mesh(); me.calc_loop_triangles()
            uvl = me.uv_layers.active.data
            for lt in me.loop_triangles:
                for li, vi in zip(lt.loops, lt.vertices):
                    u, v = uvl[li].uv
                    k = (pi, vi, round(u * 4096), round(v * 4096))
                    j = weld.get(k)
                    if j is None:
                        j = len(st["src"]); weld[k] = j
                        uk = (pi, vi)
                        ui = uniq_keys.get(uk)
                        if ui is None:
                            ui = len(st["uniq"]); uniq_keys[uk] = ui
                            st["uniq"].append(uk)
                        st["uv"] += [min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0)]
                        st["src"].append(ui)
                    st["tris"].append(j)
            ev.to_mesh_clear()
        st["players"].append({"label": p["label"],
                              "welded_range": [w0, len(st["src"])],
                              "tri_range": [t0, len(st["tris"]) // 3],
                              "camera_depth": round(_player_camera_depth(p) or 0.0, 3)})
    # per-part gather arrays: part_idx -> (vert indices, unique indices)
    gather = {}
    for ui, (pi, vi) in enumerate(st["uniq"]):
        gather.setdefault(pi, ([], []))
        gather[pi][0].append(vi); gather[pi][1].append(ui)
    st["gather"] = {pi: (np.asarray(v, np.int64), np.asarray(u, np.int64))
                    for pi, (v, u) in gather.items()}
    return st


def _anim_frame_positions(st, W, H):
    """Screen positions + camera depth (float32 (U,3): x px, y px, depth) of the unique
    vertices at the current frame, using the evaluated depsgraph and the evaluated camera
    (animated constraints). Depth is the perspective divisor (camera-space distance) --
    one consistent scale across players, used for the GPU depth test (smaller = nearer)."""
    dg = bpy.context.evaluated_depsgraph_get()
    cam = bpy.context.scene.camera
    ce = cam.evaluated_get(dg)
    P = np.array(ce.calc_matrix_camera(dg, x=W, y=H) @ ce.matrix_world.inverted())
    out = np.empty((len(st["uniq"]), 3), np.float32)
    for pi, name in enumerate(st["parts"]):
        o = bpy.context.scene.objects[name]
        ev = o.evaluated_get(dg); me = ev.to_mesh()
        co = np.empty(len(me.vertices) * 3, 'float32')
        me.vertices.foreach_get('co', co)
        co = co.reshape(-1, 3)
        mw = np.array(ev.matrix_world)
        w = co @ mw[:3, :3].T + mw[:3, 3]
        clip = w @ P[:3, :3].T + P[:3, 3]
        wcl = w @ P[3, :3] + P[3, 3]
        scr = (clip[:, :2] / wcl[:, None] * 0.5 + 0.5) * [W, H]
        vi, ui = st["gather"].get(pi, (None, None))
        if vi is not None:
            out[ui, :2] = scr[vi]
            out[ui, 2] = wcl[vi]
        ev.to_mesh_clear()
    return out


def _anim_write_mesh(path, st):
    """mesh.bin: 'NSKM' header + zlib payload (uv u16 pairs, src u16, tris u16 triples).
    Browser-side: DecompressionStream('deflate')."""
    import struct, zlib
    welded, unique, ntris = len(st["src"]), len(st["uniq"]), len(st["tris"]) // 3
    uv = np.round(np.asarray(st["uv"], np.float64) * 65535).astype('<u2')
    src = np.asarray(st["src"], '<u2')
    tris = np.asarray(st["tris"], '<u2')
    payload = zlib.compress(uv.tobytes() + src.tobytes() + tris.tobytes(), 9)
    with open(path, "wb") as f:
        f.write(struct.pack('<4sIIII', b'NSKM', 1, welded, unique, ntris))
        f.write(payload)
    return welded, unique, ntris


def _anim_write_anim(path, keys, quant, keys_fps):
    """anim.bin v3: 'NSKA' header + zlib payload of int16 (x, y, z) per vertex for K mesh
    keys -- key 0 absolute, key 1 delta, keys 2+ delta-of-delta (linear motion predictor).
    x/y are pixels x `quant` (1/8 px); z is camera depth normalized to [zmin, zmax] x zq
    (zq = 2^ANIM_Z_BITS - 1; one scale across all players, for the GPU depth test). zq is
    in the header so the consumer divides by the right value."""
    import struct, zlib
    K, V = len(keys), keys[0].shape[0]
    zmin = float(min(k[:, 2].min() for k in keys))
    zmax = float(max(k[:, 2].max() for k in keys))
    if zmax - zmin < 1e-6:
        zmax = zmin + 1e-6
    zq = float((1 << ANIM_Z_BITS) - 1)
    keys_q = []
    for k in keys:
        q = np.empty((V, 3), '<i2')
        q[:, :2] = np.round(k[:, :2] * quant)
        q[:, 2] = np.round((k[:, 2] - zmin) / (zmax - zmin) * zq)
        keys_q.append(q)
    stream = [keys_q[0].tobytes()]
    if K > 1:
        deltas = [(b - a) for a, b in zip(keys_q, keys_q[1:])]
        stream.append(deltas[0].tobytes())
        stream += [(b - a).tobytes() for a, b in zip(deltas, deltas[1:])]
    payload = zlib.compress(b"".join(stream), 9)
    with open(path, "wb") as f:
        f.write(struct.pack('<4sIIIfffff', b'NSKA', 3, V, K,
                            float(quant), float(keys_fps), zmin, zmax, zq))
        f.write(payload)
    return K, V


def _mesh_uv_handedness(me, vco):
    """Sign relating the texture (U,V) frame to the mesh's geometric winding-normal, from the first
    non-degenerate per-pixel quad. Used to rebuild a COLLAPSED axis of a degenerate face in the
    correct orientation (so its texture is not mirrored). +1 or -1 (defaults +1 if none found)."""
    uvl = me.uv_layers.active.data
    for poly in me.polygons:
        if len(poly.loop_indices) != 4:
            continue
        p = np.array([uvl[l].uv for l in poly.loop_indices], 'f')
        if p[:, 0].max() - p[:, 0].min() <= 1e-6 or p[:, 1].max() - p[:, 1].min() <= 1e-6:
            continue
        c = vco[list(poly.vertices)]
        cu, cv = p[:, 0].mean(), p[:, 1].mean()
        r, t = p[:, 0] > cu, p[:, 1] > cv
        u3 = c[r].mean(0) - c[~r].mean(0)            # 3D dir of +U
        v3 = c[t].mean(0) - c[~t].mean(0)            # 3D dir of +V
        ng = np.cross(c[1] - c[0], c[2] - c[0])      # geometric normal (loop winding)
        return 1.0 if float(np.dot(np.cross(u3, v3), ng)) >= 0 else -1.0
    return 1.0


def _expand_pixel_uvs(parts, skin_px=None):
    """The rig insets each per-pixel UV face to ~50% of its skin-pixel cell (an anti-bleed trick),
    leaving gaps that bake as a grid and lose HD-skin detail. SNAP each per-pixel quad to fill its
    whole cell (cell = 1/skin_px): the cell is `floor(centre/cell)`, and each loop is sent to the
    cell corner on its own side. This handles every case the rig throws at it:
      * inset faces                 -> grow to the full cell;
      * already-full faces          -> unchanged (already at the cell bounds, e.g. arm/sleeve bottoms);
      * degenerate faces (one axis collapsed to extent 0, e.g. the front face's last row, w=1/128,
        h=0) -> the quad is real in 3D even though its UV collapsed, so the collapsed axis is rebuilt
        from the GEOMETRY: the defined axis gives its 3D direction, the face normal + the mesh UV
        handedness give the collapsed axis's 3D direction, and the loop-edge on the +side of it goes
        to the cell's top/right. This keeps the texture upright (no vertical flip) -- invisible on a
        64px skin but correct for higher-resolution textures.
    The skin still samples NEAREST, so 64px skins look identical (the pixel centre is unchanged) and
    higher-res skins now use the full pixel, upright. Modifies the BASE mesh UVs (Subdivision
    interpolates them), so the atlas bake AND the collected geometry stay consistent. Returns
    `restore()`. Dedups by mesh."""
    skin_px = skin_px or STATIC_SKIN_PX
    cell = 1.0 / float(skin_px)
    edges = ((0, 1), (1, 2), (2, 3), (3, 0))
    saved, meshes = [], set()
    for o in parts:
        me = getattr(o, "data", None)
        uvl = me.uv_layers.active if me else None
        if uvl is None or me in meshes:
            continue
        meshes.add(me)
        orig = np.empty(len(uvl.data) * 2, 'f'); uvl.data.foreach_get("uv", orig)
        saved.append((uvl, orig))
        uv = orig.reshape(-1, 2).copy()
        vco = np.empty(len(me.vertices) * 3, 'f'); me.vertices.foreach_get('co', vco)
        vco = vco.reshape(-1, 3)
        s_hand = _mesh_uv_handedness(me, vco)

        def _split_collapsed(co, defined3, perp_edges, sign):
            """Assign `perp_edges[0]/[1]` (the two edges crossing the collapsed axis) to the low/high
            cell side, by where each edge sits along the collapsed axis's 3D direction
            `sign * (normal x defined3)`. Returns (low_edge_loops, high_edge_loops)."""
            ng = np.cross(co[1] - co[0], co[2] - co[0])
            axis3 = sign * np.cross(ng, defined3)
            c0 = co[list(perp_edges[0])].mean(0)
            c1 = co[list(perp_edges[1])].mean(0)
            if float(np.dot(c1 - c0, axis3)) >= 0.0:
                return list(perp_edges[0]), list(perp_edges[1])     # edge0 low, edge1 high
            return list(perp_edges[1]), list(perp_edges[0])

        for poly in me.polygons:
            li = list(poly.loop_indices)
            if len(li) != 4:
                continue                                            # per-pixel faces are quads
            pts = uv[li]
            cu, cv = pts[:, 0].mean(), pts[:, 1].mean()
            col = int(np.floor(cu / cell)); row = int(np.floor(cv / cell))
            u0, u1, v0, v1 = col * cell, (col + 1) * cell, row * cell, (row + 1) * cell
            new = pts.copy()
            u_ok = pts[:, 0].max() - pts[:, 0].min() > 1e-6
            v_ok = pts[:, 1].max() - pts[:, 1].min() > 1e-6
            co = vco[list(poly.vertices)]
            if u_ok:
                new[:, 0] = np.where(pts[:, 0] > cu, u1, u0)        # U from each loop's side
            else:                                                  # U collapsed: rebuild from geometry
                vert = [e for e in edges if abs(pts[e[0], 1] - pts[e[1], 1]) > 1e-6]
                if len(vert) == 2:
                    v3 = co[pts[:, 1] > cv].mean(0) - co[pts[:, 1] <= cv].mean(0)
                    lo, hi = _split_collapsed(co, v3, vert, -s_hand)   # U is normal x V, opposite sign
                    new[lo, 0] = u0; new[hi, 0] = u1
            if v_ok:
                new[:, 1] = np.where(pts[:, 1] > cv, v1, v0)        # V from each loop's side
            else:                                                  # V collapsed: rebuild from geometry
                horiz = [e for e in edges if abs(pts[e[0], 0] - pts[e[1], 0]) > 1e-6]
                if len(horiz) == 2:
                    u3 = co[pts[:, 0] > cu].mean(0) - co[pts[:, 0] <= cu].mean(0)
                    lo, hi = _split_collapsed(co, u3, horiz, s_hand)
                    new[lo, 1] = v0; new[hi, 1] = v1
            uv[li] = new
        uvl.data.foreach_set("uv", uv.reshape(-1))
        me.update()

    def restore():
        for uvl, orig in saved:
            uvl.data.foreach_set("uv", orig)
        for m in meshes:
            m.update()
    return restore


def _part_variant(label):
    """Arm-style variant a part belongs to, from its Minecraft label suffix: 'classic'/'slim', or
    None for parts shared by both styles (head/body/legs/hat/jacket/pant)."""
    l = (label or "").lower()
    return "classic" if "_classic" in l else "slim" if "_slim" in l else None


def _static_collect(players, W, H, mesh_layers=None):
    """Collect the static export geometry into a welded mesh. Includes BOTH the base parts AND the
    overlays (2nd layer), per player, in painter's order, and -- for the arm style -- BOTH variants
    (classic + slim) so the wallpaper can switch them: the rig is flipped to each style and the
    arm/sleeve parts are collected while posed correctly. Screen positions are captured PER PART at
    collection time (so the non-active arm variant is right even though it would be parked away when
    re-evaluated at the end). Each player records welded_range, tri_range, overlay_tri_start, and a
    `parts` list ({label, tri_range, overlay, variant}) for per-part / per-variant toggling.
    Returns the dict consumed by `_static_write_geometry` (positions in st['pos'])."""
    dg = bpy.context.evaluated_depsgraph_get()
    cam = bpy.context.scene.camera
    ce = cam.evaluated_get(dg)
    P = np.array(ce.calc_matrix_camera(dg, x=W, y=H) @ ce.matrix_world.inverted())
    ordered = sorted(players, key=lambda p: _player_camera_depth(p) or 0.0, reverse=True)
    st = {"parts": [], "uv": [], "src": [], "tris": [], "players": [], "uniq": [], "pos": []}
    weld, uniq_keys = {}, {}

    def add_part(o):
        pi = len(st["parts"]); st["parts"].append(o.name)
        dgl = bpy.context.evaluated_depsgraph_get()      # fresh: the arm-style switch re-evaluates
        ev = o.evaluated_get(dgl); me = ev.to_mesh(); me.calc_loop_triangles()
        uvl = me.uv_layers.active.data
        # project this part's vertices in the CURRENT pose/variant -> screen px + camera depth
        co = np.empty(len(me.vertices) * 3, 'f'); me.vertices.foreach_get('co', co)
        co = co.reshape(-1, 3)
        mw = np.array(ev.matrix_world)
        wp = co @ mw[:3, :3].T + mw[:3, 3]
        clip = wp @ P[:3, :3].T + P[:3, 3]
        wcl = wp @ P[3, :3] + P[3, 3]
        scr = (clip[:, :2] / wcl[:, None] * 0.5 + 0.5) * np.array([W, H], 'f')
        for lt in me.loop_triangles:
            for li, vi in zip(lt.loops, lt.vertices):
                u, v = uvl[li].uv
                k = (pi, vi, round(u * 4096), round(v * 4096))
                j = weld.get(k)
                if j is None:
                    j = len(st["src"]); weld[k] = j
                    uk = (pi, vi)
                    ui = uniq_keys.get(uk)
                    if ui is None:
                        ui = len(st["uniq"]); uniq_keys[uk] = ui
                        st["uniq"].append(uk)
                        st["pos"].append((float(scr[vi, 0]), float(scr[vi, 1]), float(wcl[vi])))
                    st["uv"] += [min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0)]
                    st["src"].append(ui)
                st["tris"].append(j)
        ev.to_mesh_clear()

    for p in ordered:
        labs = p.get("uv_labels") or _assign_part_labels(p["uv_parts"], label=p["label"])
        # Parts visible in the player's CURRENT arm style: base (head/body/legs + the active arm),
        # overlay (2nd layer). The other arm variant is collected separately below.
        vis = [o for o in p["uv_parts"] if not o.hide_render]
        def _is_ov(o, _labs=labs):
            return any(k in (_labs.get(o.name) or "").lower() for k in ATLAS_OVERLAY_LABELS)
        base = sorted([o for o in vis if not _is_ov(o)], key=lambda o: o.name)
        ov = sorted([o for o in vis if _is_ov(o)], key=lambda o: o.name)
        parts_meta = []
        w0, t0 = len(st["src"]), len(st["tris"]) // 3

        def _emit(o, overlay):
            pt0 = len(st["tris"]) // 3
            add_part(o)
            parts_meta.append({"label": labs.get(o.name, o.name),
                               "tri_range": [pt0, len(st["tris"]) // 3],
                               "overlay": overlay, "variant": _part_variant(labs.get(o.name))})
        for o in base:
            _emit(o, False)
        for o in ov:
            _emit(o, True)
        # The OTHER arm variant (classic<->slim): flip the rig so its arm/sleeve parts pose at the
        # arm position, then collect them (positions captured here, while correct). Atlas is baked
        # from the default variant only -- the two UV regions differ ~1px so the bake margin covers
        # the other. The browser draws the parts of whichever variant is active.
        cur_variant = next((pm["variant"] for pm in parts_meta if pm["variant"]), None)
        if cur_variant:
            other = "slim" if cur_variant == "classic" else "classic"
            arm, saved = _set_arm_style(p, dict(MASK_ARM_VARIANTS)[other])
            try:
                other_parts = sorted([o for o in p["uv_parts"] if not o.hide_render
                                      and _part_variant(labs.get(o.name)) == other],
                                     key=lambda o: o.name)
                for o in other_parts:
                    _emit(o, _is_ov(o))
            finally:
                _restore_arm_style(arm, p, saved)
        ov_t0 = next((pm["tri_range"][0] for pm in parts_meta if pm["overlay"]),
                     len(st["tris"]) // 3)
        st["players"].append({"label": p["label"],
                              "welded_range": [w0, len(st["src"])],
                              "tri_range": [t0, len(st["tris"]) // 3],
                              "overlay_tri_start": ov_t0,
                              "parts": parts_meta,
                              "default_variant": cur_variant,   # the arm style the atlas baked for
                              "n_base_parts": len(base), "n_overlay_parts": len(ov),
                              "camera_depth": round(_player_camera_depth(p) or 0.0, 3)})
    # mesh-type layers (retexturable): each group's meshes appended as one entity, its own tri range
    st["layers"] = []
    for ml in (mesh_layers or []):
        w0, t0 = len(st["src"]), len(st["tris"]) // 3
        for o in ml["meshes"]:
            if o.type == 'MESH' and o.data.uv_layers.active is not None:
                add_part(o)
        if len(st["tris"]) // 3 > t0:
            st["layers"].append({"name": ml["name"], "object": ml["object"], "kind": ml["kind"],
                                 "role": ml.get("role", "layer"),
                                 "welded_range": [w0, len(st["src"])],
                                 "tri_range": [t0, len(st["tris"]) // 3],
                                 "camera_depth": round(ml.get("camera_depth") or 0.0, 3)})
    gather = {}
    for ui, (pi, vi) in enumerate(st["uniq"]):
        gather.setdefault(pi, ([], []))
        gather[pi][0].append(vi); gather[pi][1].append(ui)
    st["gather"] = {pi: (np.asarray(v, np.int64), np.asarray(u, np.int64))
                    for pi, (v, u) in gather.items()}
    return st


def _static_write_mesh(path, st):
    """static mesh.bin: 'NSKM' **v2** -- u32 `src` + u32 `tris` (the DENSE mesh exceeds the u16
    the animated NSKM v1 uses); `uv` stays u16 (0..65535). Header `<4sIIII>` = magic, ver=2,
    welded, unique, ntris; payload = zlib(uv u16x2/65535, src u32, tris u32x3). Browser-side:
    DecompressionStream('deflate'), then read indices as Uint32 (v2) vs Uint16 (v1)."""
    import struct, zlib
    welded, unique, ntris = len(st["src"]), len(st["uniq"]), len(st["tris"]) // 3
    uv = np.round(np.asarray(st["uv"], np.float64) * 65535).astype('<u2')
    src = np.asarray(st["src"], '<u4')
    tris = np.asarray(st["tris"], '<u4')
    payload = zlib.compress(uv.tobytes() + src.tobytes() + tris.tobytes(), 9)
    with open(path, "wb") as f:
        f.write(struct.pack('<4sIIII', b'NSKM', 2, welded, unique, ntris))
        f.write(payload)
    return welded, unique, ntris


def _static_write_geometry(players, out_dir=None, mesh_layers=None):
    """Write the static `mesh.bin` (K=1) + `positions.bin` for the CURRENT frame, using the DENSE
    mesh (full subdivision, no AntiLag/Slim reduction -- one key, so there is no per-frame delta
    budget to fit). Reuses `_static_collect` / `_anim_frame_positions` / `_anim_write_mesh` /
    `_anim_write_anim` (positions.bin = NSKA v3 with K=1: one absolute key; the consumer
    degenerates to "use key 0"). Returns a stats dict (incl. per-player ranges for the manifest).
    Self-contained: restores AntiLag / Slim / simplify state."""
    s = bpy.context.scene
    W = s.render.resolution_x * MASK_RES_PCT // 100
    H = s.render.resolution_y * MASK_RES_PCT // 100
    out_dir = out_dir or os.path.join(_abs(OUT_DIR), STATIC_OUT_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    for p in players:
        p["uv_labels"] = _assign_part_labels(p["uv_parts"], label=p["label"])
    arms = list({p["arm"] for p in players if p.get("arm")})
    simp = (s.render.use_simplify, s.render.simplify_subdivision_render)
    rig_snap = {}
    for a in arms:
        pb = a.pose.bones.get('Main_Properties')
        if pb is not None:
            rig_snap[a.name] = (pb.get('AntiLag'), pb.get('Slim main'))
    try:
        # DENSE: full subdivision + no AntiLag (render-matching silhouettes; K=1 has no per-frame
        # delta budget to fit). Each player's arm STYLE (Slim main) is left UNTOUCHED so the
        # hide_render-based selection in _static_collect picks the variant the player actually
        # uses (classic vs slim) instead of overriding it.
        for a in arms:
            pb = a.pose.bones.get('Main_Properties')
            if pb is not None and 'AntiLag' in pb.keys():
                pb['AntiLag'] = False
                a.update_tag()
        s.render.use_simplify = False
        bpy.context.view_layer.update()
        st = _static_collect(players, W, H, mesh_layers=mesh_layers)
        pos = np.asarray(st["pos"], 'float32')   # captured per part at collect time (variant-correct)
        welded, unique, ntris = _static_write_mesh(os.path.join(out_dir, "mesh.bin"), st)
        K, V = _anim_write_anim(os.path.join(out_dir, "positions.bin"), [pos], ANIM_QUANT, 0.0)
        # global camera-depth range (== the positions.bin z scale) so the per-sprite depth maps
        # normalize to the SAME window depth as the mesh, for a GPU depth test in the browser
        zmin = float(pos[:, 2].min()) if len(pos) else 0.0
        zmax = float(pos[:, 2].max()) if len(pos) else 1.0
        return {"resolution": [W, H], "welded": welded, "unique": unique, "tris": ntris,
                "keys": K, "verts": V, "players": st["players"],
                "layers": st.get("layers", []), "zmin": zmin, "zmax": zmax,
                "mesh_bytes": os.path.getsize(os.path.join(out_dir, "mesh.bin")),
                "pos_bytes": os.path.getsize(os.path.join(out_dir, "positions.bin")),
                "mesh_file": STATIC_OUT_SUBDIR + "/mesh.bin",
                "positions_file": STATIC_OUT_SUBDIR + "/positions.bin"}
    finally:
        s.render.use_simplify, s.render.simplify_subdivision_render = simp
        for a in arms:
            pb = a.pose.bones.get('Main_Properties')
            snap = rig_snap.get(a.name)
            if pb is not None and snap is not None:
                if snap[0] is not None:
                    pb['AntiLag'] = snap[0]
                if snap[1] is not None:
                    pb['Slim main'] = snap[1]
                a.update_tag()
        bpy.context.view_layer.update()


def _static_render_images(players, out_dir=None, groups=None, mesh_layers=None, prog=None,
                          do_bg=True, do_fg=True, do_shadows=True, existing=None):
    """Render the static scenery images for the CURRENT frame, matching the DENSE mesh (AntiLag
    off, no simplify), plus a TOGGLEABLE per-entity shadow.
      * `background.<ext>` = the CLEAN scene -- every player AND optional layer hidden (no baked
        shadows). The players/layers and their shadows are composited on top in the browser.
      * `foreground.webp` = the scenery IN FRONT of the players (scenery-with-players-holdout
        INTERSECT the player silhouette), straight WebP alpha -- no matte (a video-only workaround);
        composited `out = fg.rgb*fg.a + behind*(1-fg.a)` so the AA edge blends without a fringe.
      * `<entity>_shadow.webp` per player and per layer group = the legacy DISPLAY-space multiply
        ratio `clip(sh_display / clean_display, 0, 1)` (1 = untouched): the entity rendered
        camera-invisible (still casting / reflecting / refracting), every OTHER entity hidden, over
        the SAME clean baseline. The browser multiplies the clean bg by each ENABLED entity's ratio
        before drawing the meshes, so a toggle removes the entity and its shadow together. Captures
        cast shadows AND reflections darker than the water (brighter reflections clamp to 1 -- a
        future additive pass). Optional layers (`groups`) are kept out of bg/fg (their own sprites).
    GENERATOR: `yield`s a per-render progress tuple before each beauty render (bg / each foreground /
    light / each entity shadow) so the modal panel bar advances during the long pass, and RETURNS
    {'background', 'foreground', 'light', 'shadows', 'resolution'} (capture it via `yield from`).
    Self-contained: restores AntiLag/simplify, the full session, and visibility."""
    s = bpy.context.scene
    W = s.render.resolution_x * MASK_RES_PCT // 100
    H = s.render.resolution_y * MASK_RES_PCT // 100
    out_dir = out_dir or os.path.join(_abs(OUT_DIR), STATIC_OUT_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    groups = groups or []
    mesh_layers = mesh_layers or []
    def _y(msg):
        # progress tuple for the modal bar (prog is supplied by _static_export_steps). Called
        # standalone (prog=None) it returns a harmless no-op fraction so the generator still drains.
        return prog(msg) if prog else (0.0, msg)
    layer_mesh_names = {m.name for g in groups for m in g["meshes"]}      # all layers (mesh+sprite)
    mesh_layer_names = {m.name for ml in mesh_layers for m in ml["meshes"]}   # retexturable -> drawn
    sprite_layer_names = layer_mesh_names - mesh_layer_names              # own beauty, own fg/shadow
    char_names = {o.name for p in players for o in p["char_all"]}
    arms = list({p["arm"] for p in players if p.get("arm")})
    simp = (s.render.use_simplify, s.render.simplify_subdivision_render)
    rig_snap = {a.name: a.pose.bones['Main_Properties'].get('AntiLag')
                for a in arms if a.pose.bones.get('Main_Properties')}
    sess = _Session()
    bg_rel = STATIC_OUT_SUBDIR + "/background" + STATIC_BG_EXT
    fg_rel = STATIC_OUT_SUBDIR + "/foreground.webp"
    try:
        # DENSE render (silhouettes match the exported mesh); arm style left as the player's own
        for a in arms:
            pb = a.pose.bones.get('Main_Properties')
            if pb is not None and 'AntiLag' in pb.keys():
                pb['AntiLag'] = False
                a.update_tag()
        s.render.use_simplify = False
        sess.restore_visibility()          # drivers active -> real per-player visibility
        for o in s.objects:                # toggleable layers are their own sprites: keep them out
            if layer_mesh_names and o.name in layer_mesh_names:   # of the baked bg/fg
                o.hide_render = True
        bpy.context.view_layer.update()
        player_objs = [o for o in s.objects if o.name in char_names and not o.hide_render]
        player_set = {o.name for o in player_objs}
        entity_names = char_names | layer_mesh_names   # everything that is composited on top

        # 1) BACKGROUND: the CLEAN scene -- every player + layer hidden (drivers muted), so no
        #    baked shadows; the entities and their shadows composite on top in the browser. Rendered
        #    at the panel "Samples" -- the SAME quality as the per-entity shadow / sprite renders, so
        #    the shadow ratio and the sprite |S - clean| stay consistent and the composite has no
        #    quality seam. Raise "Samples" for a cleaner backdrop.
        rW = rH = None
        clean_disp = None
        need_bg = do_bg or do_shadows           # the shadow ratio needs the clean-background baseline
        if need_bg:
            yield _y("rendering background" if do_bg else "rendering background (for shadows)")
            sess.mute_drivers(True)
            clean_hr = {o: o.hide_render for o in s.objects if o.name in entity_names}
            for o in clean_hr:
                o.hide_render = True
            bpy.context.view_layer.update()
            bg, rW, rH = yield from _render_combined_array_gen(sess, ILLUM_RES_PCT)
            clean_disp = np.clip(_to_display(bg), 1e-4, None)   # shadow-ratio baseline (display space)
            if do_bg:
                buf = np.ones((bg.shape[0], 4), 'float32')
                buf[:, :3] = _to_display(bg)
                _save_image(buf.reshape(-1), rW, rH, os.path.join(out_dir, "background" + STATIC_BG_EXT),
                            file_format=STATIC_BG_FORMAT, quality=STATIC_BG_QUALITY)
            else:
                bg_rel = existing["background"]             # rendered for clean_disp only; reuse file
            sess.restore_visibility()                       # drivers back
        else:
            yield _y("background (reused)")
            bg_rel = existing["background"]
        for o in s.objects:                                 # only SPRITE layers stay hidden (their
            if o.name in sprite_layer_names:                # own beauty/fg); mesh layers participate
                o.hide_render = True
        bpy.context.view_layer.update()
        # mesh ENTITIES = players + retexturable mesh layers (drawn raw -> need the fg in front)
        mesh_entity_objs = player_objs + [o for o in s.objects
                                          if o.name in mesh_layer_names and not o.hide_render]
        mesh_entity_set = {o.name for o in mesh_entity_objs}

        # 2) FOREGROUND: scenery-with-entities-holdout INTERSECT the entity silhouette, WebP+alpha
        if do_fg:
            yield _y("rendering foreground (scenery)")
            hold = {o: o.is_holdout for o in mesh_entity_objs}
            for o in mesh_entity_objs:
                o.is_holdout = True
            bpy.context.view_layer.update()
            try:
                C, cW, cH = yield from _render_combined_array_gen(sess, ILLUM_RES_PCT, transparent=True)
            finally:                                   # restore holdout even if cancelled mid-render
                for o, v in hold.items():
                    o.is_holdout = v
            if rW is None:                             # no bg render this time -- take size from here
                rW, rH = cW, cH
            # entity silhouette: render only the mesh entities (players + mesh layers)
            yield _y("rendering foreground (silhouette)")
            camsnap = {}
            for o in s.objects:
                if o.type == 'MESH' and o.name not in mesh_entity_set:
                    camsnap[o.name] = o.hide_render
                    o.hide_render = True
            bpy.context.view_layer.update()
            D, _, _ = yield from _render_combined_array_gen(sess, ILLUM_RES_PCT, transparent=True)
            for n, v in camsnap.items():
                ob = s.objects.get(n)
                if ob is not None:
                    ob.hide_render = v
            ca = np.clip(C[:, 3], 0.0, 1.0)
            straight = np.where(ca[:, None] > 1e-4, C[:, :3] / np.maximum(ca[:, None], 1e-4), 0.0)
            alpha = np.where(D[:, 3] > 0.5, ca, 0.0)        # scenery in front AND over an entity
            vis = alpha > 1e-4
            fg = np.zeros((C.shape[0], 4), 'float32')
            fg[vis, :3] = _to_display(straight[vis])
            # STRAIGHT alpha, composited as out = fg.rgb*fg.a + behind*(1-fg.a) (multiply by alpha at
            # sample time). Dilate the color outward so partial-alpha EDGE texels carry real scenery
            # color (not zeroed black); the fully transparent rgb is don't-care (x alpha=0).
            fg[:, :3] = _anim_dilate_light(fg[:, :3], vis, rW, rH)
            fg[:, 3] = alpha
            _save_image(fg.reshape(-1), rW, rH, os.path.join(out_dir, "foreground.webp"),
                        file_format='WEBP', lossless=STATIC_FG_LOSSLESS, quality=STATIC_FG_QUALITY)
        else:
            yield _y("foreground (reused)")
            fg_rel = existing["foreground"]

        # 3) LIGHT (screen-space): the visible players rendered GRAY, scenery camera-invisible but
        # still lighting/shadowing -- the combined lit-gray at screen resolution, like the old
        # per-part export and the animated 'light' frame. VIEWPORT-quality soft shadows, no UV
        # lattice. The renderer samples it by screen position and relights as skin*light*2.
        light_rel = (existing.get("light") if (existing and not do_bg) else None)
        if STATIC_LIGHT_SPACE == "screen" and do_bg:
            yield _y("rendering light (screen-space)")
            scenery = [o for o in s.objects if o.type == 'MESH' and o.name not in player_set]
            gray = _gray_diffuse_material()
            sv = _swap_materials(player_objs, gray)
            sc2 = {o: o.visible_camera for o in scenery}
            for o in scenery:
                o.visible_camera = False
            bpy.context.view_layer.update()
            try:
                L, _, _ = yield from _render_combined_array_gen(sess, ILLUM_RES_PCT, transparent=True)
            finally:                                   # restore camera-vis + materials even on cancel
                for o, v in sc2.items():
                    o.visible_camera = v
                _restore_materials(sv)
            la = np.clip(L[:, 3], 0.0, 1.0)
            lst = np.where(la[:, None] > 1e-4, L[:, :3] / np.maximum(la[:, None], 1e-4), 0.0)
            lst = _anim_dilate_light(lst, la > 0.01, rW, rH)
            lbuf = np.ones((L.shape[0], 4), 'float32')
            lbuf[:, :3] = _lin_to_srgb(lst)
            _save_image(lbuf.reshape(-1), rW, rH, os.path.join(out_dir, "light" + STATIC_BG_EXT),
                        file_format=STATIC_BG_FORMAT, quality=STATIC_BG_QUALITY)
            light_rel = STATIC_OUT_SUBDIR + "/light" + STATIC_BG_EXT

        # 4) per-entity TOGGLEABLE shadow: each entity camera-invisible (still casting / reflecting)
        #    over the same clean baseline, every OTHER entity hidden -> display-space multiply ratio.
        #    Players + SPRITE layers only. MESH-type layers are flat retexturable billboards (e.g. a
        #    sign/poster with no material) -- letting them cast would dim the whole scene; skip them.
        mesh_layer_group_names = {ml["name"] for ml in mesh_layers}
        shadow_groups = [g for g in groups if g["name"] not in mesh_layer_group_names]
        if do_shadows:
            shadows = {}        # players + sprite layers: each gets its own multiply ratio
            entities = ([("player", p["label"], list(p["char_all"])) for p in players]
                        + [("layer", g["name"], list(g["meshes"])) for g in shadow_groups])
        else:
            shadows = dict((existing or {}).get("shadows") or {})   # reuse the previous shadows
            entities = []
            yield _y("shadows (reused)")
        for _si, (kind, name, objs) in enumerate(entities):
            yield _y(f"rendering shadow {_si + 1}/{len(entities)}: {name}")
            obj_names = {o.name for o in objs}
            sess.restore_visibility()                       # active entity keeps its real visibility
            muted = []
            for o in s.objects:                             # hide + mute every OTHER entity
                if o.name in entity_names and o.name not in obj_names:
                    if o.animation_data:
                        for d in o.animation_data.drivers:
                            if d.data_path == "hide_render" and not d.mute:
                                d.mute = True
                                muted.append(d)
                    o.hide_render = True
            cam_save = {o: o.visible_camera for o in objs if o.type == 'MESH'}
            for o in cam_save:                              # camera-invisible: only casts/reflects
                o.visible_camera = False
            bpy.context.view_layer.update()
            try:
                sh, _, _ = yield from _render_combined_array_gen(sess, ILLUM_RES_PCT)
            finally:
                for o, v in cam_save.items():
                    o.visible_camera = v
                for d in muted:
                    d.mute = False
            # LUMINANCE darkening (achromatic): a single factor per texel, so render-noise between
            # the two renders never tints the unaffected scenery. Drop sub-floor darkening to 0.
            w709 = np.array([0.2126, 0.7152, 0.0722], 'float32')
            rL = np.clip((_to_display(sh) @ w709) / np.maximum(clean_disp @ w709, 1e-3), 0.0, 1.0)
            dark = np.clip((1.0 - rL) - STATIC_SHADOW_FLOOR, 0.0, 1.0)   # noise-floor suppression
            r = 1.0 - dark
            sbuf = np.ones((r.shape[0], 4), 'float32')
            sbuf[:, :3] = r[:, None]
            fn = _layer_safe_name(name) + "_shadow.webp"
            _save_image(sbuf.reshape(-1), rW, rH, os.path.join(out_dir, fn),
                        file_format='WEBP', quality=STATIC_BG_QUALITY)
            shadows[name] = fn
            print(f"[STATIC shadow] {kind} {name} -> {fn}")
        return {"background": bg_rel, "foreground": fg_rel, "light": light_rel,
                "shadows": shadows, "resolution": [W, H]}
    finally:
        sess.restore()
        s.render.use_simplify, s.render.simplify_subdivision_render = simp
        for a in arms:
            pb = a.pose.bones.get('Main_Properties')
            if pb is not None and rig_snap.get(a.name) is not None:
                pb['AntiLag'] = rig_snap[a.name]
                a.update_tag()
        bpy.context.view_layer.update()


def _static_layer_mesh_info(group):
    """Decide whether an optional-layer GROUP can be exported as a retexturable MESH (like a player)
    instead of a flat sprite, and what its base texture is. Qualifies when every mesh has an active
    UV and the group's materials reference AT MOST one image texture (one UV/texture space):
      * exactly one shared image      -> that image is the base texture;
      * NO image at all (a bare UV surface, e.g. a plane you texture entirely in the wallpaper tool)
                                       -> a blank PLACEHOLDER base is generated (image = None).
    Two+ distinct images, a MIX of textured + untextured materials, or a missing UV -> not a mesh
    layer. Returns (image_or_None, qualifies)."""
    meshes = [o for o in group["meshes"] if o.type == 'MESH']
    if not meshes:
        return None, False
    images, any_untextured = set(), False
    for o in meshes:
        if o.data.uv_layers.active is None:
            return None, False
        if not o.material_slots:
            any_untextured = True
        for ms in o.material_slots:
            mat = ms.material
            imgs = ([n.image for n in mat.node_tree.nodes
                     if n.type == 'TEX_IMAGE' and n.image is not None]
                    if (mat and mat.use_nodes) else [])
            images.update(imgs)
            if not imgs:
                any_untextured = True
    if len(images) >= 2:
        return None, False                   # conflicting textures -> no single UV/texture space
    if len(images) == 1 and not any_untextured:
        return next(iter(images)), True       # one shared texture -> the base
    if len(images) == 0:
        return None, True                     # bare UV surface -> blank placeholder base
    return None, False                        # mixed (one texture + untextured) -> ambiguous


def _static_export_layer_texture(image, out_dir, stem):
    """Save a mesh-type layer's base texture (the swappable 'skin') as `<stem>_tex.png`. With an
    image: its sRGB color map (linear pixels -> sRGB bytes). Without one (a bare UV surface): a small
    neutral PLACEHOLDER, so the layer is re-textured entirely in the wallpaper tool. Sampled NEAREST.
    Returns the bare name."""
    if image is None:
        w = h = LAYER_PLACEHOLDER_PX
        rgba = np.full((w * h, 4), 0.5, 'float32'); rgba[:, 3] = 1.0   # mid-gray, opaque placeholder
    else:
        w, h = image.size
        buf = np.empty(w * h * 4, 'float32')
        image.pixels.foreach_get(buf)
        rgba = buf.reshape(-1, 4).copy()
        rgba[:, :3] = _lin_to_srgb(rgba[:, :3])      # sRGB color map (alpha stays linear 0/1)
    fn = stem + "_tex.png"
    _save_image(rgba.reshape(-1), w, h, os.path.join(out_dir, fn), file_format='PNG')
    return fn


def _layer_mode(group):
    """The export mode set on the layer's source (collection / armature / mesh): 'SPRITE' (default),
    'TEXTURE' or 'PLAYER' -- the LAYER_MODE_PROP enum registered on Object/Collection."""
    src, kind = group.get("object"), group.get("kind")
    holder = (bpy.data.collections.get(src) if kind == "collection"
              else bpy.context.scene.objects.get(src))
    return getattr(holder, LAYER_MODE_PROP, "SPRITE") if holder is not None else "SPRITE"


def _static_split_layers(groups):
    """Split discovered layer groups by their export mode into mesh-type (TEXTURE/PLAYER -- a
    re-skinnable mesh with player logic) and sprite-type (SPRITE, the default -- a single baked
    beauty that matches the render). A mesh-type layer must also QUALIFY (its meshes share one
    texture), else it falls back to a sprite. Mesh entries carry their shared `image` and a `role`
    ('player' for PLAYER mode, else 'layer'). Returns (mesh_layers, sprite_groups)."""
    mesh_layers, sprite_groups = [], []
    for g in groups:
        mode = _layer_mode(g) if STATIC_LAYERS_AS_MESH else "SPRITE"
        if mode in {"TEXTURE", "PLAYER"}:
            image, ok = _static_layer_mesh_info(g)
            if ok:
                mesh_layers.append(dict(g, image=image,
                                        camera_depth=_group_camera_depth(g["meshes"]),
                                        role=("player" if mode == "PLAYER" else "layer")))
                continue
            print(f"[STATIC] layer '{g['name']}' set to {mode} but its meshes don't share one "
                  f"texture -- exporting as a sprite instead.")
        sprite_groups.append(g)
    return mesh_layers, sprite_groups


def _material_is_transmissive(mat):
    """True if the material is a SEE-THROUGH surface you look INTO (glass / water / ice), by scanning
    its shader nodes (recursing a few levels into node groups). Detected from REFRACTIVE/transmission
    signals: a Glass/Refraction BSDF, a Principled with Transmission > 0, or a Principled with a
    constant Alpha < 1 (uniform alpha-blend). Deliberately NOT a bare Transparent BSDF nor a LINKED
    Alpha -- those are the Minecraft CUTOUT idiom (texture-keyed transparency on a SOLID prop: a leaf,
    a held spear/bucket, a particle), which must stay opaque so it occludes/holds out normally."""
    if mat is None:
        return False
    if not mat.use_nodes:
        dc = getattr(mat, "diffuse_color", None)
        return bool(dc is not None and len(dc) > 3 and dc[3] < 0.999)

    def scan(nodes, depth):
        for n in nodes:
            t = n.type
            if t in ('BSDF_GLASS', 'BSDF_REFRACTION'):     # refractive surface you look into
                return True
            if t == 'BSDF_PRINCIPLED':
                tr = n.inputs.get('Transmission Weight') or n.inputs.get('Transmission')
                if tr is not None and (tr.is_linked or tr.default_value > 1e-3):
                    return True
                al = n.inputs.get('Alpha')
                if al is not None and not al.is_linked and al.default_value < 0.999:
                    return True
            if t == 'GROUP' and n.node_tree is not None and depth < 3:
                if scan(n.node_tree.nodes, depth + 1):
                    return True
        return False

    return scan(mat.node_tree.nodes, 0)


def _obj_is_transmissive(o):
    """Does this object contain a see-through SURFACE (water / glass / ice) -- so a submerged entity
    behind it shows THROUGH it (and gets the tint) rather than being occluded/clipped? True if
    ANY material is transmissive: the water is often part of a MULTI-material object (a voxel terrain
    whose water blocks share the mesh with grass/dirt/stone, an ocean plane), so requiring EVERY
    material would miss it. False positives are avoided in `_material_is_transmissive`, which does NOT
    count a bare Transparent-BSDF cutout (a leaf, a held spear/bucket, a particle) as see-through.
    Override with `o["nsk_transmissive"]` (bool) for a material the node scan cannot read."""
    ov = o.get("nsk_transmissive")
    if ov is not None:
        return bool(ov)
    return any(_material_is_transmissive(sl.material) for sl in o.material_slots if sl.material)


def _screen_bbox(objs, margin=0.08):
    """Screen-space (0..1) bounding box of `objs` -- their EVALUATED bound-boxes projected through the
    active camera -- padded by `margin`, as (xmin, xmax, ymin, ymax) for a render BORDER, or None.
    Lets a sprite render only the mob's region instead of the whole 4K frame."""
    s = bpy.context.scene
    cam = s.camera
    if cam is None:
        return None
    dg = bpy.context.evaluated_depsgraph_get()
    ce = cam.evaluated_get(dg)
    P = np.array(ce.calc_matrix_camera(dg, x=s.render.resolution_x, y=s.render.resolution_y)
                 @ ce.matrix_world.inverted())
    xs, ys = [], []
    for o in objs:
        ev = o.evaluated_get(dg)
        mw = np.array(ev.matrix_world)
        for c in ev.bound_box:
            clip = P @ (mw @ np.array([c[0], c[1], c[2], 1.0]))
            if clip[3] <= 1e-6:
                continue
            xs.append(clip[0] / clip[3] * 0.5 + 0.5)
            ys.append(clip[1] / clip[3] * 0.5 + 0.5)
    if not xs:
        return None
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    px = (xmax - xmin) * margin + 0.02
    py = (ymax - ymin) * margin + 0.02
    xmin = max(0.0, xmin - px); xmax = min(1.0, xmax + px)
    ymin = max(0.0, ymin - py); ymax = min(1.0, ymax + py)
    if xmax - xmin < 1e-3 or ymax - ymin < 1e-3:
        return None
    return (xmin, xmax, ymin, ymax)


def _static_render_layers(players, sprite_groups, all_layer_mesh_names, zmin, zmax, out_dir=None,
                          prog=None):
    """Render each SPRITE-type layer EXACTLY as the full Blender scene shows it, then CUT it to the
    group's own silhouette: render the mob with ALL scenery visible and normal (every OTHER entity
    hidden) -> `S` has the real water/glass surface, with its real reflections, OVER the mob's
    submerged part (not a flat decal); a second render with all scenery hidden gives the mob's tight
    silhouette `D`, and the sprite = S masked to D. (An earlier holdout approach made the water reflect
    emptiness and transmit the film, so a half-submerged mob looked like it sat ON the water.)
    Display-encoded like the background, edge-dilated. Its cast shadow / water darkening is NOT folded
    in here -- it is a SEPARATE multiply ratio rendered in _static_render_images (like mesh entities).
    Also writes a per-sprite DEPTH map `<name>_depth.webp`: the group's camera depth normalized to the
    GLOBAL `[zmin, zmax]` (the SAME window scale as the mesh / positions.bin), re-encoded 8-bit over
    the sprite's OWN `[wmin, wmax]` for precision, so the browser can `gl_FragDepth` it and depth-test
    the sprite per-pixel against the players / mesh-layers (so a spear/bucket in front occludes -- and
    is occluded by -- the arms correctly). GENERATOR: `yield`s a progress tuple per sprite group and
    RETURNS [{name, object, kind, image, depth, depth_range, camera_depth}], back -> front (capture via
    `yield from`). Self-contained."""
    if not sprite_groups:
        return []
    s = bpy.context.scene
    out_dir = out_dir or os.path.join(_abs(OUT_DIR), STATIC_OUT_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    char_names = {o.name for p in players for o in p["char_all"]}
    entity_names = char_names | set(all_layer_mesh_names)
    zspan = max(zmax - zmin, 1e-6)
    sess = _Session()
    infos = []
    def _y(msg):
        return prog(msg) if prog else (0.0, msg)
    try:
        sess.vl.use_pass_z = True                  # for the per-sprite depth map
        for _si, g in enumerate(sprite_groups):
            yield _y(f"rendering sprite {_si + 1}/{len(sprite_groups)}: {g['name']}")
            meshes = g["meshes"]
            safe = g["name"]
            group_names = {m.name for m in meshes}
            # isolate: THIS group visible, every OTHER entity (players + other layers) hidden, but ALL
            # scenery kept VISIBLE and rendered NORMALLY. So `S` is EXACTLY what Blender renders for
            # this mob in its environment -- the water/glass surface (with its real reflections) OVER
            # its submerged part, opaque scenery occluding it. We then just CUT S to the mob's own
            # silhouette. NO holdout: a holdout makes the water reflect emptiness and transmit the
            # film instead of the terrain, which is exactly the "flat decal on top of the water" look.
            sess.restore_visibility()
            sess.mute_drivers(True)
            for o in s.objects:
                if o.name in entity_names and o.name not in group_names:
                    o.hide_render = True
            scenery = [o for o in s.objects if o.type == 'MESH'
                       and o.name not in group_names and o.name not in entity_names]
            bpy.context.view_layer.update()
            # BBOX CROP: render ONLY the mob's screen-space region (a render border) instead of the
            # whole 4K frame -- the mob is a tiny part of it, so this is ~10-50x faster (and keeps the
            # UI block short). These three renders run SYNCHRONOUSLY (blocking): the back-to-back
            # S -> hide-scenery -> D sequence needs the silhouette pass to render with the scenery
            # actually hidden, and the async modal could NOT guarantee that -- its INVOKE render did not
            # reliably pick up the mid-sequence scene change (a depsgraph update AND a settle grace both
            # failed), so the silhouette rendered the whole scenery and the sprite kept its background.
            # Sync renders off the live depsgraph, so the cut is always correct. Only these quick
            # (bbox-cropped) renders block; the slow image / shadow / water passes stay async (UI live).
            bb = _screen_bbox(meshes)
            bsave = (s.render.use_border, s.render.use_crop_to_border, s.render.border_min_x,
                     s.render.border_max_x, s.render.border_min_y, s.render.border_max_y)
            if bb:
                s.render.use_border = True
                s.render.use_crop_to_border = False
                (s.render.border_min_x, s.render.border_max_x,
                 s.render.border_min_y, s.render.border_max_y) = bb
            try:
                # S: the mob in the FULL (opaque) scene -- the exact Blender look, water over submerged
                comb, rW, rH = _render_combined_array(sess, ILLUM_RES_PCT)
                # D: the mob ALONE (all scenery hidden) -> tight silhouette (incl. the submerged part
                # the water hides in S) + the mob's own clean camera depth (S's depth over the
                # submerged part would read the water surface, not the mob).
                sc_hr = {o: o.hide_render for o in scenery}
                for o in scenery:
                    o.hide_render = True
                bpy.context.view_layer.update()
                sil, _, _ = _render_combined_array(sess, ILLUM_RES_PCT, transparent=True)
                dep, _, _ = sess.render_pass('Depth', 'CYCLES', 1, ILLUM_RES_PCT)   # mob camera depth
                for o in scenery:
                    o.hide_render = sc_hr[o]
            finally:
                bpy.context.view_layer.update()
                (s.render.use_border, s.render.use_crop_to_border, s.render.border_min_x,
                 s.render.border_max_x, s.render.border_min_y, s.render.border_max_y) = bsave

            alpha = np.clip(sil[:, 3], 0.0, 1.0)               # the mob's own silhouette (soft AA edge)
            vis = alpha > 1e-3
            spr = np.zeros((comb.shape[0], 4), 'float32')
            spr[vis, :3] = _to_display(comb[vis, :3])          # S is opaque -> rgb is the straight color
            spr[:, :3] = _anim_dilate_light(spr[:, :3], vis, rW, rH)
            spr[:, 3] = alpha
            _save_image(spr.reshape(-1), rW, rH, os.path.join(out_dir, safe + ".webp"),
                        file_format='WEBP', lossless=STATIC_FG_LOSSLESS, quality=STATIC_FG_QUALITY)

            # DEPTH map: window depth (same scale as the mesh) re-encoded over the sprite's own range.
            # Tight sprite -> the range is the mob's own depth (no broad reflection to clamp it).
            depth_fn = depth_range = None
            if dep is not None:
                wd = (dep[:, 0] - zmin) / zspan        # window depth; may be <0 (nearer than players)
                # the Depth pass is a huge sentinel where a ray misses (sky / through a holdout); a
                # few such texels on the silhouette edge would otherwise blow up wmax. Keep finite.
                sel = (alpha > 0.5) & np.isfinite(dep[:, 0]) & (dep[:, 0] < 1e6)
                if sel.any():
                    wmin, wmax = float(wd[sel].min()), float(wd[sel].max())
                    if wmax - wmin < 1e-4:
                        wmax = wmin + 1e-4
                    b = np.clip((wd - wmin) / (wmax - wmin), 0.0, 1.0)   # 8-bit over [wmin, wmax]
                    dbuf = np.ones((b.shape[0], 4), 'float32'); dbuf[:, :3] = b[:, None]
                    depth_fn = safe + "_depth.webp"
                    _save_image(dbuf.reshape(-1), rW, rH, os.path.join(out_dir, depth_fn),
                                file_format='WEBP', lossless=True)
                    depth_range = [round(wmin, 6), round(wmax, 6)]
            cd = _group_camera_depth(meshes)
            infos.append({"name": safe, "object": g["object"], "kind": g["kind"],
                          "image": safe + ".webp", "depth": depth_fn, "depth_range": depth_range,
                          "camera_depth": round(cd, 4) if cd is not None else None})
            print(f"[STATIC sprite] {safe} -> camera_depth={cd}, depth_range={depth_range}")
        infos.sort(key=lambda i: (i["camera_depth"] is None, -(i["camera_depth"] or 0.0)))
        return infos
    finally:
        sess.restore()
        bpy.context.view_layer.update()


def _close_mask(m2d, iterations=2):
    """Morphological CLOSE of radius `iterations` on a 0/1 mask: dilate N times THEN erode N times
    (NOT N separate dilate-erode passes -- that only ever fills 1-2px). This fills interior holes up
    to ~2*N px wide (the sub-pixel index-0 cracks the dense lattice-deformed meshes leave in the
    Object-Index pass) while keeping the outer silhouette intact (the dilation is undone by the
    matching erosion). Real holes / concavities wider than 2*N px survive."""
    def _dil(a):
        r = a
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                r = np.maximum(r, np.roll(np.roll(a, dy, 0), dx, 1))
        return r
    def _ero(a):
        r = a
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                r = np.minimum(r, np.roll(np.roll(a, dy, 0), dx, 1))
        return r
    for _ in range(iterations):
        m2d = _dil(m2d)
    for _ in range(iterations):
        m2d = _ero(m2d)
    return m2d


def _box_blur_rgb(rgb2d, iterations=2):
    """Cheap 3x3 box blur of an (H,W,3) array (np.roll, edge-wrapping is fine for an interior map).
    Used to denoise the water-tint ratio so the glass surface's render noise does not speckle it."""
    out = rgb2d
    for _ in range(iterations):
        acc = np.zeros_like(out)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                acc += np.roll(np.roll(out, dy, 0), dx, 1)
        out = acc / 9.0
    return out


def _static_render_tint(players, all_layer_mesh_names=None, out_dir=None, prog=None):
    """Per player, a screen-space TINT map so a SUBMERGED player stays re-skinnable but is
    tinted by the transmissive surface in front of it (water/glass), instead of looking like a flat
    decal on top of it. The PURE transmission is extracted by two-background matting -- the player as
    flat WHITE then BLACK emission behind the (lit) water; `T = white - black` CANCELS the surface's
    own reflection, leaving only the colored attenuation (the deliberate T-only choice: we do NOT add
    the reflection, which would hide the skin). A third water-hidden silhouette gives a clean coverage
    so only the SOLID submerged interior is tinted, not the anti-aliased rim; the result is box-blurred
    (the glass render is noisy / refraction-patterned) and clamped to `[lo, 1]` (the deepest / most
    reflective water never goes to black). 1 = no change (dry / above the waterline); the browser
    multiplies the relit skin by it, and it is skin-independent. GENERATOR: `yield`s a progress tuple
    per player and RETURNS {label: file} (capture via `yield from`); a dry player is skipped, and if
    the scene has no transmissive scenery nothing is written. Self-contained."""
    s = bpy.context.scene
    out_dir = out_dir or os.path.join(_abs(OUT_DIR), STATIC_OUT_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    char_names = {o.name for p in players for o in p["char_all"]}
    entity_names = char_names | set(all_layer_mesh_names or [])   # players AND layers are composited
    scenery = [o for o in s.objects if o.type == 'MESH' and o.name not in entity_names]
    transmissive = [o for o in scenery if _obj_is_transmissive(o)]
    if not transmissive:
        return {}                                  # no water/glass anywhere -> no tint to bake
    opaque = [o for o in scenery if o not in transmissive]
    TINT_LO = 0.25                                  # darkest the deep/reflective tints (no black
    #                                                 blobs where the surface is fully reflective)

    def _emit(name, v):
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()
        em = nt.nodes.new("ShaderNodeEmission")
        em.inputs[0].default_value = (v, v, v, 1.0)
        op = nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(em.outputs[0], op.inputs[0])
        return mat

    white = _emit("NSK_tint_white", 1.0)
    black = _emit("NSK_tint_black", 0.0)
    sess = _Session()
    out = {}
    def _y(msg):
        return prog(msg) if prog else (0.0, msg)
    try:
        for _wi, p in enumerate(players):
            yield _y(f"rendering tint {_wi + 1}/{len(players)}: {p['label']}")
            meshes = [o for o in p["char_all"] if o.type == 'MESH']
            own = {m.name for m in p["char_all"]}
            saved = {o: [sl.material for sl in o.material_slots] for o in meshes}

            def _set(mat):
                for o in meshes:
                    for sl in o.material_slots:
                        sl.material = mat

            sess.restore_visibility()
            sess.mute_drivers(True)
            for o in s.objects:                    # isolate: other entities (players AND layers, e.g.
                if o.name in entity_names and o.name not in own:  # leg armor) + OPAQUE scenery hidden,
                    o.hide_render = True                          # so only THIS player + the water remain
            op_hr = {o: o.hide_render for o in opaque}
            for o in opaque:
                o.hide_render = True
            tr_hr = {o: o.hide_render for o in transmissive}
            # SYNCHRONOUS (blocking) + bbox-cropped. The matte swaps the player's emission material AND
            # hides the water between back-to-back renders. Async could NOT guarantee the silhouette
            # render saw the water hidden -- its INVOKE render did not reliably pick up the mid-sequence
            # scene change (the same race that broke the sprites), so a player whose render raced got the
            # whole water SURFACE folded into its silhouette -> a wide water blob tinted instead of the
            # player. Sync renders off the live depsgraph, always correct; the bbox (the player's screen
            # region, at MASK_RES_PCT) keeps the block short.
            bb = _screen_bbox(meshes)
            bsave = (s.render.use_border, s.render.use_crop_to_border, s.render.border_min_x,
                     s.render.border_max_x, s.render.border_min_y, s.render.border_max_y)
            if bb:
                s.render.use_border = True
                s.render.use_crop_to_border = False
                (s.render.border_min_x, s.render.border_max_x,
                 s.render.border_min_y, s.render.border_max_y) = bb
            try:
                _set(white)
                bpy.context.view_layer.update()
                Aw, W, H = _render_combined_array(sess, MASK_RES_PCT)  # white emission
                _set(black)
                bpy.context.view_layer.update()
                Ab, _, _ = _render_combined_array(sess, MASK_RES_PCT)  # black emission
                for o in transmissive:
                    o.hide_render = True                                  # silhouette: the player alone
                bpy.context.view_layer.update()
                cov_arr, _, _ = _render_combined_array(sess, MASK_RES_PCT, transparent=True)
            finally:
                (s.render.use_border, s.render.use_crop_to_border, s.render.border_min_x,
                 s.render.border_max_x, s.render.border_min_y, s.render.border_max_y) = bsave
                for o, v in tr_hr.items():
                    o.hide_render = v
                for o, v in op_hr.items():
                    o.hide_render = v
                for o, mats in saved.items():
                    for i, sl in enumerate(o.material_slots):
                        sl.material = mats[i]

            T = np.clip(_box_blur_rgb((Aw[:, :3] - Ab[:, :3]).reshape(H, W, 3), 2).reshape(-1, 3),
                        0.0, 1.0)                                         # blurred pure transmission
            cov = np.clip(cov_arr[:, 3], 0.0, 1.0)
            tinted = (cov > 0.85) & (T.mean(1) < 0.8)                    # solid interior the water dims
            if int(tinted.sum()) < 16:
                continue                                                # essentially dry -> no map
            tint = np.ones((T.shape[0], 4), 'float32')
            # ONE representative water colour over the whole submerged region, NOT the per-pixel
            # transmission. The per-pixel T collapses to the TINT_LO clamp where the water is deep or
            # reflective (T -> 0, T-only cannot see through there), painting a dark "bar" that reads as
            # a shadow instead of the water. The median is robust to those dark outliers, so it is the
            # actual tint -- a clean, uniform overlay.
            tint[tinted, :3] = np.clip(np.median(T[tinted], axis=0), TINT_LO, 1.0)
            fn = f"{p['label']}_tint.webp"
            _save_image(tint.reshape(-1), W, H, os.path.join(out_dir, fn),
                        file_format='WEBP', lossless=True)
            out[p["label"]] = fn
            print(f"[STATIC tint] {p['label']} -> {fn} ({int(tinted.sum())} px tinted)")
    finally:
        sess.restore()
        bpy.context.view_layer.update()
        for mat in (white, black):
            bpy.data.materials.remove(mat)
    return out


def _static_render_masks(players, mesh_layers, all_layer_mesh_names, out_dir=None, prog=None):
    """Per-entity SCENERY-occlusion mask via the Object Index pass: the entity's meshes get
    pass_index=1, every OTHER entity (the optional players/layers) is hidden, the real scenery stays.
    `mask = (front-most object index == 1)`: 1 where the entity is visible, 0 where the FIXED scenery
    (including geometry-nodes objects, which a holdout cannot cut) is in front of it. Players get one
    mask PER ARM VARIANT (classic/slim -- the arm width differs); mesh-type layers get a single mask.
    The browser clips each drawn entity to its mask, so the scenery occludes it WITHOUT a foreground
    painting over the (toggleable) optional layers. Because the clean background already contains that
    front scenery, revealing it through the clip is identical to a foreground for opaque scenery.
    GENERATOR: `yield`s a progress tuple per mask (player x arm-variant, then mesh-layer) and RETURNS
    {player_label: {variant: file}, layer_name: file} (capture via `yield from`). Self-contained."""
    s = bpy.context.scene
    out_dir = out_dir or os.path.join(_abs(OUT_DIR), STATIC_OUT_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    char_names = {o.name for p in players for o in p["char_all"]}
    entity_names = char_names | set(all_layer_mesh_names)
    # A TRANSMISSIVE surface (water/glass) must NOT clip a submerged entity: it is see-through, so the
    # entity shows THROUGH it (and then gets the tint on top), not hidden behind it. Hide the
    # transmissive scenery for the Object-Index render so only OPAQUE scenery occludes -- otherwise the
    # glass water is the front-most index and the submerged part falls out of the mask (vanishes).
    transmissive_scenery = [o for o in s.objects if o.type == 'MESH'
                            and o.name not in entity_names and _obj_is_transmissive(o)]
    # Render the entity OPAQUE for the Object-Index: the 2nd-layer (jacket / sleeves / pants) material
    # is a CUTOUT -- transparent wherever the CURRENT skin has no overlay there -- so the textured
    # Object-Index sees straight through it and the overlay falls OUT of the mask (a default skin with
    # no jacket -> the mask is base-only). But the mask must clip ANY skin (another skin MAY have a
    # jacket there), so it has to follow the full GEOMETRY, not the loaded skin's texture.
    mask_opaque = bpy.data.materials.new("NSK_mask_opaque")
    mask_opaque.use_nodes = True
    _nt = mask_opaque.node_tree
    _nt.nodes.clear()
    _nt.links.new(_nt.nodes.new("ShaderNodeBsdfDiffuse").outputs[0],
                  _nt.nodes.new("ShaderNodeOutputMaterial").inputs[0])
    sess = _Session()
    masks = {}
    saved_idx = {o.name: o.pass_index for o in s.objects}
    def _y(msg):
        return prog(msg) if prog else (0.0, msg)
    try:
        sess.vl.use_pass_object_index = True

        def _render_mask(active_objs, fn):
            for o in s.objects:
                o.pass_index = 0
            for o in active_objs:
                o.pass_index = 1
            tr_hidden = [o for o in transmissive_scenery if not o.hide_render]
            for o in tr_hidden:
                o.hide_render = True                  # water does not occlude the submerged entity
            mesh_objs = [o for o in active_objs if o.type == 'MESH']
            saved_mats = {o: [sl.material for sl in o.material_slots] for o in mesh_objs}
            for o in mesh_objs:
                for sl in o.material_slots:
                    sl.material = mask_opaque          # full geometry silhouette, skin-independent
            bpy.context.view_layer.update()
            try:
                oid, W, H = yield from sess.render_pass_gen(
                    'Object Index', 'CYCLES', STATIC_MASK_SAMPLES, MASK_RES_PCT)
            finally:                                   # restore materials + water vis even on cancel
                for o, mats in saved_mats.items():
                    for i, sl in enumerate(o.material_slots):
                        sl.material = mats[i]
                for o in tr_hidden:
                    o.hide_render = False
            if oid is None:
                return None
            m = (np.round(oid[:, 0]).astype(np.int32) == 1).astype('float32')   # 1 = entity visible
            m = _close_mask(m.reshape(H, W), iterations=3).reshape(-1)           # fill cracks <= ~6px
            buf = np.empty((m.shape[0], 4), 'float32')
            buf[:, 0] = buf[:, 1] = buf[:, 2] = m
            buf[:, 3] = 1.0
            _save_image(buf.reshape(-1), W, H, os.path.join(out_dir, fn),
                        file_format='WEBP', lossless=True)
            return fn

        for p in players:
            active = {o.name for o in p["char_all"]}
            variants = {}
            for vname, vval in MASK_ARM_VARIANTS:
                yield _y(f"rendering mask: {p['label']} / {vname}")
                sess.restore_visibility()
                arm, saved = _set_arm_style(p, vval)
                muted = _hide_others_for_bake(s, active, entity_names)
                try:
                    fn = yield from _render_mask(list(p["char_all"]), f"{p['label']}_mask_{vname}.webp")
                finally:
                    for d in muted:
                        d.mute = False
                    _restore_arm_style(arm, p, saved)
                if fn:
                    variants[vname] = fn
                print(f"[STATIC mask] {p['label']} {vname} -> {fn}")
            masks[p["label"]] = variants

        for ml in mesh_layers:
            yield _y(f"rendering mask layer: {ml['name']}")
            active = {m.name for m in ml["meshes"]}
            sess.restore_visibility()
            muted = _hide_others_for_bake(s, active, entity_names)
            try:
                fn = yield from _render_mask(list(ml["meshes"]), f"{ml['name']}_mask.webp")
            finally:
                for d in muted:
                    d.mute = False
            if fn:
                masks[ml["name"]] = fn
            print(f"[STATIC mask] layer {ml['name']} -> {fn}")
        return masks
    finally:
        for o in s.objects:
            o.pass_index = saved_idx.get(o.name, 0)
        sess.restore()
        bpy.data.materials.remove(mask_opaque)
        bpy.context.view_layer.update()


def _static_render_overlay(overlay_names, out_dir=None):
    """Render the OVERLAY-mode layers (rain / glare / smoke) ALONE over a transparent film -> the
    static `foreground.webp` (straight alpha, display-encoded, edge-dilated). These sit IN FRONT of
    everything and are additive/emissive, so rendering them isolated is faithful (unlike a refractive
    surface, which needs its backdrop). Composited over the final image in the browser. They are
    hidden from every other pass (bg / mask / geometry / sprites / shadows), so they never occlude
    the players/layers nor break the Object-Index mask. Returns the file name, or None. Self-contained
    -- shows ONLY the overlay renderables, keeps the lights/world for the lit ones."""
    if not overlay_names:
        return None
    s = bpy.context.scene
    out_dir = out_dir or os.path.join(_abs(OUT_DIR), STATIC_OUT_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    sess = _Session()
    try:
        sess.restore_visibility()
        sess.mute_drivers(True)
        for o in s.objects:        # show ONLY the overlay meshes; keep lights/camera/world
            if o.type in ('MESH', 'CURVE', 'SURFACE', 'META', 'VOLUME', 'FONT', 'GPENCIL'):
                o.hide_render = (o.name not in overlay_names)
        bpy.context.view_layer.update()
        comb, rW, rH = _render_combined_array(sess, ILLUM_RES_PCT, transparent=True)
        alpha = np.clip(comb[:, 3], 0.0, 1.0)
        straight = np.where(alpha[:, None] > 1e-4,
                            comb[:, :3] / np.maximum(alpha[:, None], 1e-4), 0.0)
        vis = alpha > 1e-4
        fg = np.zeros((comb.shape[0], 4), 'float32')
        if vis.any():
            fg[vis, :3] = _to_display(straight[vis])
        fg[:, :3] = _anim_dilate_light(fg[:, :3], vis, rW, rH)
        fg[:, 3] = alpha
        _save_image(fg.reshape(-1), rW, rH, os.path.join(out_dir, "foreground.webp"),
                    file_format='WEBP', lossless=STATIC_FG_LOSSLESS, quality=STATIC_FG_QUALITY)
        print(f"[STATIC overlay] {len(overlay_names)} mesh(es) -> foreground.webp "
              f"(coverage {float(vis.mean()):.3f})")
        return "foreground.webp"
    finally:
        sess.restore()
        bpy.context.view_layer.update()


def _static_write_manifest(out_dir, geo, imgs, atlas_by_label, layers=None, masks=None,
                           tints=None):
    """Write `static/manifest.json` tying the static-v2 pieces together (paths are bare file names,
    relative to the manifest's own dir). `light_space` selects how the renderer samples the light:
    "screen" -> one `light` image sampled by screen position (viewport-quality, default); "uv" ->
    each player's `atlas` sampled by uv. Players carry their tri/overlay ranges. `layers` (optional)
    are independent scenery sprites, each with a `camera_depth` for depth-ordered compositing."""
    light_space = STATIC_LIGHT_SPACE
    shadows = imgs.get("shadows") or {}
    masks = masks or {}
    tints = tints or {}
    players_meta = []
    for pm in geo["players"]:
        e = dict(pm)
        if light_space == "uv":
            e["atlas"] = atlas_by_label.get(pm["label"])
        e["shadow"] = shadows.get(pm["label"])    # display-space multiply ratio, toggleable
        e["mask"] = masks.get(pm["label"])        # {variant: file} scenery-occlusion clip
        e["tint"] = tints.get(pm["label"])   # screen-space colored multiply (submerged)
        players_meta.append(e)
    if layers:
        layers = [dict(l, shadow=shadows.get(l["name"]), mask=masks.get(l["name"]))
                  for l in layers]
    light_term = ("atlas(uv)" if light_space == "uv" else "light(screenUv)")
    manifest = {
        "static_version": 1,
        "addon_version": ADDON_VERSION,
        "resolution": geo["resolution"],
        "light_space": light_space,
        "background": os.path.basename(imgs["background"]),
        "foreground": os.path.basename(imgs["foreground"]),   # WebP with alpha -- no matte
        "foreground_alpha": "straight",                       # out = fg.rgb*fg.a + behind*(1-fg.a)
        # True when the foreground is an OVERLAY (rain/glare in front of everything) -> the renderer
        # composites it on top by default. False/absent = the legacy opaque foreground (off by default).
        "foreground_overlay": bool(imgs.get("foreground_overlay")),
        "mesh": {"file": "mesh.bin", "format": "NSKM2",
                 "welded": geo["welded"], "unique": geo["unique"], "tris": geo["tris"],
                 "layout": ("NSKM v2: u32x4 header (magic, ver=2, welded, unique, ntris) + "
                            "zlib(uv u16x2/65535, src u32, tris u32x3)")},
        "positions": {"file": "positions.bin", "format": "NSKA3", "keys": geo["keys"],
                      "verts": geo["verts"], "quant": ANIM_QUANT, "z_bits": ANIM_Z_BITS,
                      "layout": ("NSKA v3 header (magic, u32 ver/V/K, f32 quant/keys_fps/zmin/"
                                 "zmax/zq) + zlib(int16 xyz, K=1 absolute); x,y = px*quant; "
                                 "z = (camDepth-zmin)/(zmax-zmin)*zq for the depth test "
                                 "(shared scale, smaller = nearer)")},
        "players": players_meta,
        "shader_note": (f"draw the CLEAN background; multiply each ENABLED entity's `shadow` ratio "
                        f"(players + layers, display-space, 1=untouched) onto it; then draw players "
                        f"and `layers` together back -> front by camera_depth (larger = farther): a "
                        f"player mesh is depth-tested with color = skin(uv) * {light_term} * 2, base "
                        "tris then overlay tris [overlay_tri_start] (alpha-discard the overlay's "
                        "transparent texels); a layer is either type='mesh' (depth-tested "
                        "tex(uv)*atlas(uv)*2 over its tri_range, retexturable) or type='sprite' (a "
                        "straight-alpha quad, painter's order). Finally composite foreground "
                        "(straight alpha) over the top: out = fg.rgb*fg.a + behind*(1 - fg.a)."),
    }
    if layers:
        manifest["layers"] = layers           # scenery sprites, ordered back -> front
    if light_space == "screen" and imgs.get("light"):
        # screen-space light image, sampled at the fragment's screen pixel (full frame, no crop)
        manifest["light"] = os.path.basename(imgs["light"])
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def _existing_static_data(out_dir):
    """Load the previous export's manifest.json (if any) and reconstruct the per-step INPUT dicts that
    `_static_export_steps` feeds the manifest writer, so a SKIPPED (toggled-off) step can reuse its
    already-rendered assets on disk. Returns None when there is no prior manifest to reuse."""
    path = os.path.join(out_dir, "manifest.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            m = json.load(fh)
    except Exception as e:
        print(f"[STATIC] cannot read existing manifest for reuse ({e!r}) -- will re-render all")
        return None
    players, layers = m.get("players", []), m.get("layers", [])
    sub = STATIC_OUT_SUBDIR + "/"
    shadows = {p["label"]: p["shadow"] for p in players if p.get("shadow")}
    for l in layers:
        if l.get("shadow"):
            shadows[l["name"]] = l["shadow"]
    imgs = {"background": sub + m["background"], "foreground": sub + m["foreground"],
            "foreground_overlay": m.get("foreground_overlay"),
            "shadows": shadows, "resolution": m.get("resolution")}
    if m.get("light"):
        imgs["light"] = sub + m["light"]
    masks = {p["label"]: p["mask"] for p in players if p.get("mask")}
    for l in layers:
        if l.get("mask"):
            masks[l["name"]] = l["mask"]
    return {
        "atlas_by_label": {p["label"]: p["atlas"] for p in players if p.get("atlas")},
        "mesh_layer_assets": {l["name"]: {"atlas": l.get("atlas"), "tex": l.get("tex")}
                              for l in layers if l.get("type") == "mesh"},
        "imgs": imgs,
        "sprite_infos": [l for l in layers if l.get("type") == "sprite"],
        "masks": masks,
        "tints": {p["label"]: p["tint"] for p in players if p.get("tint")},
    }


def _static_export_steps(players, op=None, out_dir=None):
    """Generator for the static-v2 export (yields (frac, msg) like `_anim_render_steps`) into
    `<OUT_DIR>/static/`: per-player UV light atlas + DENSE `mesh.bin`/`positions.bin` (K=1) +
    `background`/`foreground` (WebP) + one `<name>.webp` sprite per optional toggleable layer
    (depth-ordered scenery, excluded from bg/fg) + `manifest.json`. Returns the manifest dict (the
    StopIteration value). Each sub-step is self-contained (restores its own state): the atlases
    bake in the scene's normal look (`restore_visibility`), the geometry/images in the DENSE
    state. Cancellation only happens at a yield (between phases), where every sub-step has already
    restored -- so it is always clean."""
    out_dir = out_dir or os.path.join(_abs(OUT_DIR), STATIC_OUT_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    if FIX_2LAYER_POSITION:                         # snap the hat onto the head (persistent +
        _fix_2layer_positions(players)             # idempotent) -- same as the legacy export
    uv_light = (STATIC_LIGHT_SPACE == "uv")        # else screen-space light (default)
    all_groups = discover_layers()                 # optional toggleable scenery layers
    # OVERLAY layers (rain/glare/smoke) are pulled out: they are composited on TOP via the foreground
    # and must be hidden from every other pass (so they don't occlude or break the mask).
    overlay_groups = [g for g in all_groups if _layer_mode(g) == "OVERLAY"]
    overlay_names = {m.name for g in overlay_groups for m in g["meshes"]}
    groups = [g for g in all_groups if _layer_mode(g) != "OVERLAY"]
    mesh_layers, sprite_groups = _static_split_layers(groups)   # retexturable meshes vs flat sprites
    n_atlas = (len(players) if uv_light else 0) + len(mesh_layers)
    # EVERY render step now streams ONE yield per render so the modal bar advances during each long
    # pass. Keep these counts in sync with the yields in the matching functions (prog() clamps the
    # fraction so a mismatch only shifts where the bar lands, never overshoots):
    #   images : bg + 2 foreground + optional screen light + one per entity shadow (players + sprites)
    #   sprites: one beauty-cut per sprite group
    #   masks  : one Object-Index per player x arm-variant + one per mesh-layer
    #   water  : one two-background matte per player, but only if the scene has transmissive scenery
    #            (else _static_render_tint early-returns with no renders)
    _entity_nm = ({o.name for p in players for o in p["char_all"]}
                  | {m.name for g in groups for m in g["meshes"]})
    has_transmissive = any(_obj_is_transmissive(o) for o in bpy.context.scene.objects
                           if o.type == 'MESH' and o.name not in _entity_nm)
    n_mask_renders = len(players) * len(MASK_ARM_VARIANTS) + len(mesh_layers)
    n_tint_renders = len(players) if has_transmissive else 0
    # per-step toggles: a step turned OFF is reused from the previous manifest (1 yield instead of N).
    reuse_any = not (STATIC_DO_ATLAS and STATIC_DO_BACKGROUND and STATIC_DO_FOREGROUND
                     and STATIC_DO_SHADOWS and STATIC_DO_SPRITES and STATIC_DO_MASKS)
    existing = _existing_static_data(out_dir) if reuse_any else None
    if reuse_any and existing is None:
        print("[STATIC] a step is OFF but there is no previous export to reuse -- rendering all")
    # render a step when it is ON, or when there is nothing to reuse (the manifest needs every part)
    do_atlas = STATIC_DO_ATLAS or not existing
    do_bg = STATIC_DO_BACKGROUND or not existing
    do_fg = STATIC_DO_FOREGROUND or not existing      # also gates the tint (front of the player)
    do_shadows = STATIC_DO_SHADOWS or not existing
    do_sprites = STATIC_DO_SPRITES or not existing
    do_masks = STATIC_DO_MASKS or not existing
    _has_atlas = uv_light or bool(mesh_layers)
    ya = n_atlas if do_atlas else (1 if _has_atlas else 0)
    # images sub-yields: 1 background + (2 foreground | 1) + (screen light if bg) + (N shadows | 1)
    yi = (1 + (2 if do_fg else 1) + (1 if (STATIC_LIGHT_SPACE == "screen" and do_bg) else 0)
          + ((len(players) + len(sprite_groups)) if do_shadows else 1))
    ys = (len(sprite_groups) if do_sprites else 1) if sprite_groups else 0
    ym = n_mask_renders if do_masks else 1
    yw = (n_tint_renders if do_fg else 1) if has_transmissive else 0   # tint follows Foreground
    # atlases + geometry + images + sprites + masks + water + (overlay) + manifest
    total = ya + 1 + yi + ys + ym + yw + (1 if overlay_groups else 0) + 1
    state = {"done": 0}
    def prog(msg):
        state["done"] += 1
        print(f"[STATIC {state['done']}/{total}] {msg}")
        return (min(state["done"] / total, 1.0), msg)   # clamp: a miscount never overshoots the bar
    # Expand the rig's inset per-pixel UVs to fill their skin-pixel cells (no gap-grid in the atlas,
    # high-res atlas usable again, HD skins work). Modifies the base mesh UVs so the atlas bake AND
    # the collected mesh.bin UVs both use them; restored in the finally.
    restore_uv = None
    if STATIC_EXPAND_PIXEL_UVS:
        restore_uv = _expand_pixel_uvs([o for p in players for o in (p.get("uv_parts") or [])])
        bpy.context.view_layer.update()
    # Force the "Second layer" toggle ON for the WHOLE export (before the _Session snapshots) so the
    # overlay parts (jacket / sleeves / pants) are visible in the collected mesh AND in the rendered
    # silhouette / shadow / atlas -- otherwise a rig left with the toggle OFF exports only the hat
    # (which _fix_2layer_positions unhides explicitly) and the other overlays are driver-hidden out.
    # The browser toggles each overlay part per player; restored to the artist's value at the end.
    forced_props = _force_selection_props_on(players)
    hidden_widgets = _hide_bone_shapes(bpy.context.scene)   # kill the bone-widget 'dirt' in every pass
    # hide OVERLAY layers for every pass below (so the per-pass _Session snapshots capture them
    # hidden); the dedicated overlay render un-hides them itself. Restored at the end.
    hidden_overlays = []
    for _o in bpy.context.scene.objects:
        if _o.name in overlay_names and not _o.hide_render:
            _o.hide_render = True
            hidden_overlays.append(_o)
    if hidden_overlays:
        bpy.context.view_layer.update()
    try:
        # 1) per-player UV light atlases (only for light_space="uv"; the screen-space light is
        #    rendered in step 3 instead). Baked in the normal look so it sees the real lighting.
        atlas_by_label = {}
        mesh_layer_assets = {}      # layer name -> {"atlas": file, "tex": file}
        if not do_atlas and _has_atlas:                 # reuse the previous export's atlas (toggle off)
            atlas_by_label = (existing or {}).get("atlas_by_label", {})
            mesh_layer_assets = (existing or {}).get("mesh_layer_assets", {})
            yield prog("light atlas (reused)")
        elif do_atlas and (uv_light or mesh_layers):
            s = bpy.context.scene
            sess = _Session()
            # other entities are hidden per bake so each atlas is independent (keep the scenery)
            entity_names = ({o.name for p in players for o in p["char_all"]}
                            | {m.name for g in groups for m in g["meshes"]})
            try:
                if uv_light:
                    for p in players:
                        # prog BEFORE each step so the panel shows the step that is RUNNING, not the one
                        # that just finished (the renders below block, so an after-yield would label the
                        # render with the previous step's name -- e.g. "mesh.bin" during the image pass).
                        yield prog(f"baking light atlas: {p['label']}")
                        sess.restore_visibility()
                        muted = _hide_others_for_bake(s, {o.name for o in p["char_all"]},
                                                      entity_names)
                        bpy.context.view_layer.update()
                        rel = _bake_player_light_atlas(p, out_dir=out_dir,
                                                       stem=f"{p['label']}_atlas")
                        for d in muted:
                            d.mute = False
                        if rel:
                            atlas_by_label[p["label"]] = os.path.basename(rel)
                for ml in mesh_layers:       # retexturable layer: own UV light atlas + base texture
                    yield prog(f"baking atlas+tex: {ml['name']}")
                    sess.restore_visibility()
                    muted = _hide_others_for_bake(s, {m.name for m in ml["meshes"]}, entity_names)
                    bpy.context.view_layer.update()
                    syn = {"uv_parts": ml["meshes"], "label": ml["name"]}
                    rel = _bake_player_light_atlas(syn, atlas_res=LAYER_ATLAS_RES,
                                                   out_dir=out_dir, stem=f"{ml['name']}_atlas")
                    for d in muted:
                        d.mute = False
                    tex = _static_export_layer_texture(ml["image"], out_dir, ml["name"])
                    mesh_layer_assets[ml["name"]] = {
                        "atlas": os.path.basename(rel) if rel else None, "tex": tex}
            finally:
                sess.restore()
                bpy.context.view_layer.update()
        all_layer_mesh_names = {m.name for g in groups for m in g["meshes"]}
        # Each step below yields its label BEFORE running (see the atlas loop note): the blocking
        # render then carries its OWN name in the panel, not the previous step's (the geometry is <1s,
        # so an after-yield made the slow image pass show up labelled "mesh.bin / positions.bin").
        # 2) DENSE mesh.bin + positions.bin (K=1), players + mesh-type layers
        yield prog("mesh.bin / positions.bin")
        geo = _static_write_geometry(players, out_dir=out_dir, mesh_layers=mesh_layers)
        # 3) clean background + foreground (+ screen-space light when light_space="screen") +
        #    per-entity toggleable shadow ratios. Layers are excluded from bg/fg (own sprites).
        # _static_render_images streams one yield PER beauty render (bg / each foreground / light /
        # each entity shadow), so the panel bar advances during the long pass instead of freezing on
        # one label. `yield from` forwards those to the modal and captures the returned dict.
        imgs = yield from _static_render_images(
            players, out_dir=out_dir, groups=groups, mesh_layers=mesh_layers, prog=prog,
            do_bg=do_bg, do_fg=do_fg, do_shadows=do_shadows,
            existing=(existing["imgs"] if existing else None))
        # 4) sprite-type layers: tight silhouette beauty each (shadow is a separate ratio, step 3)
        sprite_infos = []
        if do_sprites and sprite_groups:
            sprite_infos = yield from _static_render_layers(
                players, sprite_groups, all_layer_mesh_names,
                geo["zmin"], geo["zmax"], out_dir=out_dir, prog=prog)
        elif sprite_groups:
            sprite_infos = list(existing["sprite_infos"]); yield prog("sprites (reused)")
        # 4b) per-entity scenery-occlusion masks (players per variant + mesh-layers) -- the static
        #     replacement for the foreground (occludes by the fixed scenery, leaves layers alone)
        if do_masks:
            masks = yield from _static_render_masks(players, mesh_layers, all_layer_mesh_names,
                                                    out_dir=out_dir, prog=prog)
        else:
            masks = dict(existing["masks"]); yield prog("masks (reused)")
        # 4d) per-player TINT: a submerged player stays re-skinnable but is tinted by the water
        #     in front of it (a colored multiply over the relit skin). It is the TRANSMISSIVE half of
        #     "what is in front of the player", so it follows the Foreground (do_fg) toggle.
        if do_fg:
            tints = yield from _static_render_tint(players, all_layer_mesh_names,
                                                               out_dir=out_dir, prog=prog)
        else:
            tints = dict(existing["tints"]); yield prog("tint (reused)")
        # 4c) OVERLAY layers (rain / glare): rendered alone -> foreground.webp, composited on top
        if overlay_groups:
            yield prog(f"rendering overlay ({len(overlay_groups)})")
            ov_fg = _static_render_overlay(overlay_names, out_dir=out_dir)
            if ov_fg:
                imgs["foreground"] = STATIC_OUT_SUBDIR + "/" + ov_fg
                imgs["foreground_overlay"] = True
        # unify layer manifest entries: mesh-type (geometry range + atlas + tex) + sprite-type
        layer_entries = []
        for lr in geo.get("layers", []):
            a = mesh_layer_assets.get(lr["name"], {})
            layer_entries.append({**lr, "type": "mesh",
                                  "atlas": a.get("atlas"), "tex": a.get("tex")})
        layer_entries += [dict(si, type="sprite") for si in sprite_infos]
        layer_entries.sort(key=lambda e: (e.get("camera_depth") is None,
                                          -(e.get("camera_depth") or 0.0)))   # back -> front
        # 5) manifest
        yield prog("manifest.json")
        manifest = _static_write_manifest(out_dir, geo, imgs, atlas_by_label, layers=layer_entries,
                                          masks=masks, tints=tints)
        print(f"[STATIC] export complete -> {out_dir} ({geo['tris']} tris, "
              f"light={STATIC_LIGHT_SPACE})")
        return manifest
    finally:
        for _o in hidden_overlays:
            _o.hide_render = False
        _restore_bone_shapes(hidden_widgets)
        _restore_selection_props(forced_props)         # restore the artist's Second-layer toggle
        if restore_uv is not None:
            restore_uv()
            bpy.context.view_layer.update()


def _static_export(players, out_dir=None, op=None):
    """Synchronous drain of `_static_export_steps` (scripts/standalone). Returns the manifest.
    Uses _drain_render so each `_RENDER` the streaming functions emit runs as a blocking render here
    (the modal operator runs them as live INVOKE_DEFAULT jobs instead)."""
    return _drain_render(_static_export_steps(players, op=op, out_dir=out_dir))


def _anim_encode(adir, fps):
    """Encode the PNG sequences to WebM/VP9 with ffmpeg. The foreground is split into an RGB
    video + a grayscale MATTE video (its alpha) -- no alpha channel anywhere, so it decodes
    on Safari too (which lacks VP9-alpha). All streams are plain yuv420p. Returns (ok,
    message); if ffmpeg is missing, writes encode.sh/.bat with the commands instead."""
    import shutil as _sh, subprocess, sys
    seq = os.path.join(adir, "seq")
    jobs = [("bg", "background.webm", ["-pix_fmt", "yuv420p"], "bg"),
            ("fg", "foreground.webm", ["-pix_fmt", "yuv420p"], "fg"),
            ("fg_matte", "foreground_matte.webm", ["-pix_fmt", "yuv420p"], "fg"),
            ("light", "light.webm", ["-pix_fmt", "yuv420p"], "light")]
    # GUI Blender doesn't inherit the shell PATH -- look in the usual per-OS spots too.
    cands = (["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]
             if sys.platform != "win32" else
             [r"C:\ffmpeg\bin\ffmpeg.exe",
              os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe")])
    ff = _sh.which("ffmpeg") or next((p for p in cands if os.path.exists(p)), None)
    def cmd(name, out, extra, crf_key):
        return [ff or "ffmpeg", "-y", "-framerate", str(fps),
                "-i", os.path.join(seq, name, "%04d.png"),
                "-c:v", "libvpx-vp9", "-crf", str(ANIM_CRF[crf_key]), "-b:v", "0",
                "-row-mt", "1", "-cpu-used", "4", *extra, os.path.join(adir, out)]
    if ff is None:
        import shlex
        win = sys.platform == "win32"
        script = os.path.join(adir, "encode.bat" if win else "encode.sh")
        with open(script, "w") as f:
            f.write("@echo off\n" if win else "#!/bin/sh\n")
            f.write(("rem " if win else "# ") + "ffmpeg not found -- run this to encode\n")
            for name, out, extra, ck in jobs:
                c = cmd(name, out, extra, ck)
                c[0] = "ffmpeg"
                f.write((" ".join(c) if win else " ".join(shlex.quote(x) for x in c)) + "\n")
        if not win:
            os.chmod(script, 0o755)
        return False, f"ffmpeg not found -- wrote {os.path.basename(script)}"
    for name, out, extra, ck in jobs:
        r = subprocess.run(cmd(name, out, extra, ck), capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"ffmpeg failed on {out}: {r.stderr[-400:]}"
    return True, "encoded bg / foreground (+ matte) / light .webm"


def _anim_render_steps(players, op=None, frame_start=None, frame_end=None):
    """Generator for the animated export (one yield per unit of work, like _render_steps).
    Per frame: background (players camera-invisible, shadows baked), foreground (per-pixel:
    scenery-holdout AND player silhouette), combined light; plus mesh keys every
    ANIM_KEYS_STEP frames. Writes mesh.bin/anim.bin/manifest.json and encodes the videos."""
    s = bpy.context.scene
    f0 = s.frame_start if frame_start is None else frame_start
    f1 = s.frame_end if frame_end is None else frame_end
    fps = s.render.fps
    # Draft (panel button): only the first few seconds, for fast look/quality iteration.
    if DRAFT_MODE and frame_start is None and frame_end is None:
        f1 = min(f1, f0 + max(1, int(ANIM_DRAFT_SECONDS * fps)) - 1)
    nf = f1 - f0 + 1
    W = s.render.resolution_x * MASK_RES_PCT // 100
    H = s.render.resolution_y * MASK_RES_PCT // 100
    adir = os.path.join(_abs(OUT_DIR), ANIM_OUT_SUBDIR)
    for k in ("bg", "fg", "fg_matte", "light"):
        os.makedirs(os.path.join(adir, "seq", k), exist_ok=True)

    base_set = set(ANIM_BASE_LABELS)
    base_parts, other_char = [], []
    for p in players:
        p["uv_labels"] = _assign_part_labels(p["uv_parts"], label=p["label"])
        for o in p["char_all"]:
            (base_parts if p["uv_labels"].get(o.name) in base_set
             else other_char).append(o)
    base_names = {o.name for o in base_parts}
    char_names = {o.name for p in players for o in p["char_all"]}

    forced_props = _force_selection_props_on(players)   # not strictly needed (base only)
    sess = _Session()
    cur_frame = s.frame_current
    simp = (s.render.use_simplify, s.render.simplify_subdivision_render)
    arms = list({p["arm"] for p in players if p.get("arm")})
    rig_snap = {}
    for a in arms:
        pb = a.pose.bones.get('Main_Properties')
        if pb is not None:
            rig_snap[a.name] = (pb.get('AntiLag'), pb.get('Slim main'))

    total = 1 + nf * 3 + 2
    state = {"done": 0}
    def prog(msg):
        state["done"] += 1
        print(f"[ANIM {state['done']}/{total}] {msg}")
        return (state["done"] / total, msg)

    def setup():
        """players reduced to their base parts; scenery untouched."""
        sess.restore_visibility()
        sess.mute_drivers(True)
        for o in other_char:
            o.hide_render = True
        for o in base_parts:
            o.hide_render = False
        bpy.context.view_layer.update()

    try:
        # AntiLag (viewport mesh) + classic arms + render geometry == viewport geometry
        for a in arms:
            pb = a.pose.bones.get('Main_Properties')
            if pb is not None:
                if 'AntiLag' in pb.keys():
                    pb['AntiLag'] = True
                if 'Slim main' in pb.keys():
                    pb['Slim main'] = False
                a.update_tag()
        s.render.use_simplify = True
        s.render.simplify_subdivision_render = 0
        s.frame_set(f0)
        bpy.context.view_layer.update()

        setup()
        st = _anim_collect_static(players, W, H)
        yield prog(f"static mesh ({len(st['src'])} welded / {len(st['uniq'])} unique verts)")

        # Pre-pass: capture the crop rect from EVERY frame's vertex bbox (+ padding) and the
        # mesh keys (every ANIM_KEYS_STEP frames) for anim.bin. Capturing every frame -- not
        # just keys -- is cheap (~2 ms/frame) and bounds the real (non-linear) motion between
        # keys, so a non-key silhouette can't extend past the crop and get clipped.
        keys = []
        pmin = np.array([W, H], 'float64')
        pmax = np.array([0.0, 0.0], 'float64')
        for i, f in enumerate(range(f0, f1 + 1)):
            s.frame_set(f)
            setup()
            p = _anim_frame_positions(st, W, H)
            pmin = np.minimum(pmin, p[:, :2].min(0))
            pmax = np.maximum(pmax, p[:, :2].max(0))
            if i % ANIM_KEYS_STEP == 0 or f == f1:
                keys.append(p)
        if ANIM_CROP_PAD > 0:
            cx0 = max(0, int(np.floor(pmin[0] - ANIM_CROP_PAD)))
            cy0 = max(0, int(np.floor(pmin[1] - ANIM_CROP_PAD)))
            cx1 = min(W, int(np.ceil(pmax[0] + ANIM_CROP_PAD)))
            cy1 = min(H, int(np.ceil(pmax[1] + ANIM_CROP_PAD)))
            cx1 -= (cx1 - cx0) % 2      # even dims for yuv420p
            cy1 -= (cy1 - cy0) % 2
        else:
            cx0, cy0, cx1, cy1 = 0, 0, W, H

        def _crop(flat):
            """Crop a flat RGBA (full WxH, bottom-up) to the player rect -> (flat, w, h)."""
            sub = flat.reshape(H, W, 4)[cy0:cy1, cx0:cx1, :]
            return np.ascontiguousarray(sub).reshape(-1), cx1 - cx0, cy1 - cy0

        print(f"[ANIM] crop rect (bottom-up px): x{cx0}-{cx1} y{cy0}-{cy1} "
              f"= {cx1-cx0}x{cy1-cy0} ({100*(cx1-cx0)*(cy1-cy0)/(W*H):.0f}% of frame)")
        gray = _gray_diffuse_material()
        scenery = [o for o in s.objects if o.type == 'MESH' and o.name not in char_names]
        cached = sum(1 for i in range(nf) if ANIM_RESUME and _anim_frame_cached(adir, i))
        if cached:
            print(f"[ANIM] resume: {cached}/{nf} frames already rendered -- skipping them")
        for i, f in enumerate(range(f0, f1 + 1)):
            fn = f"{i+1:04d}.png"
            if ANIM_RESUME and _anim_frame_cached(adir, i):   # resume: this frame is done
                yield prog(f"frame {i+1}/{nf} background (cached)")
                yield prog(f"frame {i+1}/{nf} foreground (cached)")
                yield prog(f"frame {i+1}/{nf} light (cached)")
                continue
            s.frame_set(f)
            # 1) BACKGROUND: players camera-invisible (still casting -> shadows baked in)
            setup()
            sc = {o: o.visible_camera for o in base_parts}
            for o in base_parts:
                o.visible_camera = False
            bpy.context.view_layer.update()
            bg, rW, rH = _render_combined_array(sess, ILLUM_RES_PCT)
            for o, v in sc.items():
                o.visible_camera = v
            buf = np.ones((bg.shape[0], 4), 'float32')
            buf[:, :3] = _to_display(bg)
            _save_image(buf.reshape(-1), rW, rH, os.path.join(adir, "seq", "bg", fn))
            yield prog(f"frame {i+1}/{nf} background")
            # 2) FOREGROUND: scenery-with-players-holdout, masked to the silhouette
            setup()
            hold = {o: o.is_holdout for o in base_parts}
            for o in base_parts:
                o.is_holdout = True
            bpy.context.view_layer.update()
            C, _, _ = _render_combined_array(sess, ILLUM_RES_PCT, transparent=True)
            for o, v in hold.items():
                o.is_holdout = v
            setup()
            for o in s.objects:
                if o.type == 'MESH' and o.name not in base_names:
                    o.hide_render = True
            bpy.context.view_layer.update()
            D, _, _ = _render_combined_array(sess, ILLUM_RES_PCT, transparent=True)
            ca = np.clip(C[:, 3], 0.0, 1.0)
            straight = np.where(ca[:, None] > 1e-4,
                                C[:, :3] / np.maximum(ca[:, None], 1e-4), 0.0)
            alpha = np.where(D[:, 3] > 0.5, ca, 0.0)
            vis = alpha > 1e-4
            # Foreground split into RGB + a grayscale matte (the alpha) -- two plain videos,
            # no alpha channel, so it decodes on Safari (no VP9-alpha there). RGB is zeroed
            # outside the silhouette (flat -> compresses to nothing).
            fg_rgb = np.zeros((C.shape[0], 4), 'float32'); fg_rgb[:, 3] = 1.0
            fg_rgb[vis, :3] = _to_display(straight[vis])
            # dilate the color outward so the matte's soft/compressed edge blends with real
            # scenery color instead of the zeroed black (which would fringe the silhouette)
            fg_rgb[:, :3] = _anim_dilate_light(fg_rgb[:, :3], vis, rW, rH)
            crgb, cW, cH = _crop(fg_rgb.reshape(-1))
            _save_image(crgb, cW, cH, os.path.join(adir, "seq", "fg", fn))
            matte = np.ones((C.shape[0], 4), 'float32')
            matte[:, 0] = matte[:, 1] = matte[:, 2] = alpha
            cm, _, _ = _crop(matte.reshape(-1))
            _save_image(cm, cW, cH, os.path.join(adir, "seq", "fg_matte", fn),
                        colorspace='Non-Color')
            yield prog(f"frame {i+1}/{nf} foreground")
            # 3) LIGHT: base parts gray, scenery camera-invisible (still lighting)
            setup()
            sv = _swap_materials(base_parts, gray)
            sc = {o: o.visible_camera for o in scenery}
            for o in scenery:
                o.visible_camera = False
            bpy.context.view_layer.update()
            L, _, _ = _render_combined_array(sess, ILLUM_RES_PCT, transparent=True)
            for o, v in sc.items():
                o.visible_camera = v
            _restore_materials(sv)
            la = np.clip(L[:, 3], 0.0, 1.0)
            lst = np.where(la[:, None] > 1e-4,
                           L[:, :3] / np.maximum(la[:, None], 1e-4), 0.0)
            lst = _anim_dilate_light(lst, la > 0.01, rW, rH)
            lbuf = np.ones((L.shape[0], 4), 'float32')
            lbuf[:, :3] = _lin_to_srgb(lst)
            cflat, cW, cH = _crop(lbuf.reshape(-1))
            _save_image(cflat, cW, cH, os.path.join(adir, "seq", "light", fn))
            yield prog(f"frame {i+1}/{nf} light")

        welded, unique, ntris = _anim_write_mesh(os.path.join(adir, "mesh.bin"), st)
        K, V = _anim_write_anim(os.path.join(adir, "anim.bin"), keys,
                                ANIM_QUANT, fps / ANIM_KEYS_STEP)
        manifest = {
            "animated_version": 2,   # 2: NSKA v2 depth, cropped fg/light, foreground matte
            "addon_version": ADDON_VERSION,
            "fps": fps,
            "frames": nf,
            "resolution": [W, H],
            # crop rect for the foreground+light videos, in TOP-LEFT pixels (background is
            # full-frame). The fg quad covers this rect; light is sampled at
            # (fragScreenTopLeft - crop.xy) / crop.wh.
            "crop": {"x": cx0, "y": H - cy1, "w": cx1 - cx0, "h": cy1 - cy0},
            "videos": {"background": "background.webm",
                       "foreground": "foreground.webm",
                       "foreground_matte": "foreground_matte.webm",
                       "light": "light.webm"},
            "mesh": {"file": "mesh.bin", "welded": welded, "unique": unique,
                     "tris": ntris, "players": st["players"],
                     "layout": "NSKM u32x4 header + zlib(uv u16x2/65535, src u16, tris u16x3)"},
            "anim": {"file": "anim.bin", "keys": K, "verts": V, "quant": ANIM_QUANT,
                     "keys_fps": fps / ANIM_KEYS_STEP, "predictor": "delta2",
                     "z_bits": ANIM_Z_BITS,
                     "layout": ("NSKA v3 header (magic,u32 ver/V/K,f32 quant/keys_fps/zmin/"
                                "zmax/zq) + zlib(int16 xyz: abs, delta, then delta-of-delta);"
                                " x,y = px*quant; z = (camDepth-zmin)/(zmax-zmin)*zq for the "
                                "GPU depth test (shared scale, smaller = nearer)")},
            "shader_note": ("draw background.webm; draw the player meshes with a depth "
                            "test on the z attribute and color = skin(uv) * light(screen) "
                            "* 2 (display space); draw foreground.webm on top. Interpolate "
                            "positions between mesh keys."),
        }
        with open(os.path.join(adir, "manifest.json"), "w") as fh:
            json.dump(manifest, fh, indent=2)
        yield prog("mesh.bin / anim.bin / manifest.json")
        if ANIM_ENCODE:
            ok, msg = _anim_encode(adir, fps)
            print("[ANIM] encode:", msg)
            if op is not None and not ok:
                # encode failed/skipped -> the PNG sequences are kept; surface it instead of
                # reporting a clean success with no videos.
                op.report({'WARNING'}, f"NovaSkin animated: {msg}")
            if ok and not ANIM_KEEP_SEQUENCES:
                import shutil as _sh
                _sh.rmtree(os.path.join(adir, "seq"), ignore_errors=True)
        yield prog("encode")
    finally:
        sess.restore()
        s.render.use_simplify, s.render.simplify_subdivision_render = simp
        for a in arms:
            pb = a.pose.bones.get('Main_Properties')
            snap = rig_snap.get(a.name)
            if pb is not None and snap is not None:
                if snap[0] is not None:
                    pb['AntiLag'] = snap[0]
                if snap[1] is not None:
                    pb['Slim main'] = snap[1]
                a.update_tag()
        _restore_selection_props(forced_props)
        s.frame_set(cur_frame)
        bpy.context.view_layer.update()
    return {"frames": nf}


# ----------------------- UI: settings + panel -----------------------
# The CONFIG constants at the top are the defaults/fallback. These scene properties mirror
# the most-used ones so they can be edited in the N-panel; _apply_settings() copies them
# into the module globals at the start of each run (so the rest of the code is unchanged).
class NovaSkinSettings(bpy.types.PropertyGroup):
    out_dir: StringProperty(
        name="Output", default="//novaskin/", subtype='DIR_PATH',
        description="Where to write the export (relative to the .blend with //)")
    uv_format: EnumProperty(
        name="UV Format",
        items=[('PNG', "PNG", "8/16-bit PNG"),
               ('WEBP', "WebP (lossless)",
                "8-bit lossless WebP -- ~60% smaller than PNG, browser-decodable"),
               ('OPEN_EXR', "EXR (float)", "Float EXR -- straight alpha, no quantization")],
        default='PNG')
    exr_half: BoolProperty(name="Half float", default=True,
                           description="16-bit half EXR (smaller); off = 32-bit float")
    exr_codec: EnumProperty(
        name="EXR Codec",
        items=[(c, c, "") for c in ('ZIP', 'ZIPS', 'PIZ', 'PXR24', 'RLE', 'NONE', 'DWAA', 'DWAB')],
        default='ZIP')
    export_mesh: BoolProperty(
        name="Export Mesh (v2)", default=True,
        description="Export the interactive 3D wallpaper: the characters as meshes you can re-skin "
                    "live, with toggleable scenery layers. Turn off for the older flat-image export")
    export_backface_uv: BoolProperty(name="Back Faces", default=True)
    export_illum: BoolProperty(name="Illum", default=True)
    export_shadow: BoolProperty(name="Shadow", default=True)
    composite_base_layer: BoolProperty(name="Base Layer Composite", default=True)
    export_background: BoolProperty(name="Background (no players/layers)", default=True)
    # Per-step toggles for the static "Export Mesh (v2)" export. Turning one OFF skips that (slow)
    # pass and REUSES the asset from the previous export's manifest -- so you can re-render just one
    # step (e.g. only the sprites) without redoing the rest. Geometry + manifest always run.
    static_atlas: BoolProperty(
        name="Light Atlas", default=True,
        description="Export Mesh v2: bake the per-character / per-layer relight atlas. OFF reuses the "
                    "previous export's atlas (re-render other steps without re-baking)")
    static_background: BoolProperty(
        name="Background", default=True,
        description="Export Mesh v2: render the clean background image. OFF reuses the previous export")
    static_foreground: BoolProperty(
        name="Foreground", default=True,
        description="Export Mesh v2: render everything IN FRONT of the players -- the opaque foreground "
                    "AND the transmissive water/glass tint (submerged players). OFF reuses the previous")
    static_shadows: BoolProperty(
        name="Shadows", default=True,
        description="Export Mesh v2: render the per-entity shadow ratios (the slow pass). OFF reuses")
    static_sprites: BoolProperty(
        name="Sprites", default=True,
        description="Export Mesh v2: render the sprite-type scenery layers. OFF reuses the previous")
    static_masks: BoolProperty(
        name="Masks", default=True,
        description="Export Mesh v2: render the per-entity scenery-occlusion masks. OFF reuses")
    fix_2layer_position: BoolProperty(
        name="Fix Hat Position and Scale", default=True,
        description="Snap the hat (2_Layer_Extrusion) onto the head and scale it to the "
                    "Minecraft hat size (a bit bigger than the head)")
    illum_samples: IntProperty(
        name="Samples", default=48, min=1, max=4096,
        description="Cycles render samples for the whole static export -- the background, the "
                    "character light atlas, the shadows and the layer sprites all use the same "
                    "value so they stay consistent. Higher is cleaner but slower; lower for tests")
    lightshadow_format: EnumProperty(   # legacy file format; not shown in the UI (kept PNG)
        name="Illum/Shadow",
        items=[('JPEG', "JPEG", ""), ('WEBP', "WebP", ""), ('PNG', "PNG", "")],
        default='PNG')
    jpeg_quality: IntProperty(name="Quality", default=90, min=1, max=100,
                              description="JPEG/WebP quality")
    atlas_res: EnumProperty(
        name="Atlas res",
        description="How crisp the baked light and shadows look on the characters. Higher is "
                    "sharper but a bit heavier; 512 is a good balance",
        items=[('64', "64", ""), ('128', "128", ""), ('256', "256", ""),
               ('512', "512", ""), ('1024', "1024", "")],
        default='512')
    ui_tab: EnumProperty(
        name="Section",
        items=[('EXPORT', "Export", "Layer options, quality, output and the render buttons",
                'FILE_FOLDER', 0),
               ('LAYERS', "Layers", "Detected rig + optional toggleable layers",
                'RENDERLAYERS', 1),
               ('ANIM', "Animation", "Animated wallpaper export (beta)",
                'RENDER_ANIMATION', 2)],
        default='EXPORT')


def _apply_settings(scene, draft=False):
    """Copy the panel's scene properties into the module globals (no-op if absent).
    draft=True (the 'Render Draft' button) forces low resolution + few samples."""
    st = getattr(scene, "novaskin", None)
    if st is None:
        # still honor draft even without the panel props (e.g. bpy.ops with draft=True)
        g = globals()
        g["DRAFT_MODE"] = draft
        if draft:
            g["MASK_RES_PCT"] = g["ILLUM_RES_PCT"] = DRAFT_RES_PCT
            g["ILLUM_SAMPLES"] = min(DRAFT_SAMPLES, ILLUM_SAMPLES)
        return
    g = globals()
    g["OUT_DIR"] = st.out_dir
    g["UV_FORMAT"] = st.uv_format
    g["EXR_HALF"] = st.exr_half
    g["EXR_CODEC"] = st.exr_codec
    g["EXPORT_BACKFACE_UV"] = st.export_backface_uv
    g["EXPORT_ILLUM"] = st.export_illum
    g["EXPORT_SHADOW"] = st.export_shadow
    g["COMPOSITE_BASE_LAYER"] = st.composite_base_layer
    g["EXPORT_BACKGROUND"] = st.export_background
    g["STATIC_DO_ATLAS"] = st.static_atlas
    g["STATIC_DO_BACKGROUND"] = st.static_background
    g["STATIC_DO_FOREGROUND"] = st.static_foreground
    g["STATIC_DO_SHADOWS"] = st.static_shadows
    g["STATIC_DO_SPRITES"] = st.static_sprites
    g["STATIC_DO_MASKS"] = st.static_masks
    g["FIX_2LAYER_POSITION"] = st.fix_2layer_position
    g["LIGHTSHADOW_FORMAT"] = st.lightshadow_format
    g["JPEG_QUALITY"] = st.jpeg_quality
    g["ATLAS_RES"] = int(st.atlas_res)
    g["UV_EXT"] = {'OPEN_EXR': '.exr', 'WEBP': '.webp'}.get(st.uv_format, '.png')
    g["LIGHTSHADOW_EXT"] = {'JPEG': '.jpg', 'WEBP': '.webp'}.get(st.lightshadow_format,
                                                                 '.png')
    # Draft mode (the 'Render Draft' button): all render resolutions MUST stay equal
    # (UV/mask/illum/shadow/background align pixel-for-pixel), so the percentage is set
    # globally here.
    g["DRAFT_MODE"] = draft
    g["ILLUM_SAMPLES"] = min(DRAFT_SAMPLES, st.illum_samples) if draft else st.illum_samples
    g["MASK_RES_PCT"] = DRAFT_RES_PCT if draft else 100
    g["ILLUM_RES_PCT"] = DRAFT_RES_PCT if draft else 100


# ----------------------- Operator + menu + panel -----------------------
# Live progress for the panel (updated by the modal operator, read by the panel draw).
_PROGRESS = {"running": False, "frac": 0.0, "msg": "", "cancel": False}
# Key in bpy.app.driver_namespace (survives script reloads) holding the running modal
# operator, so unregister()/reload can restore the scene if a batch is mid-render.
_ACTIVE_KEY = "_novaskin_active_op"


def _set_progress_header(text):
    """Show (or clear, if text is None) a progress string in every 3D Viewport header,
    which is far more visible than the bottom status bar."""
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        scr = win.screen
        if not scr:
            continue
        for area in scr.areas:
            if area.type == 'VIEW_3D':
                area.header_text_set(text)
                area.tag_redraw()


class _NovaSkinModalMixin:
    """Shared modal / progress-bar / Esc-cancel plumbing for the three export operators. This is a
    plain mixin -- NOT a `bpy.types.Operator` and NOT registered -- so each operator inherits
    `bpy.types.Operator` directly. Subclassing a *registered* operator (the old design) breaks the
    parent's RNA binding in Blender ("unable to get Python class for RNA struct 'RENDER_OT_...'"),
    which silently kills the parent's button. Each operator provides `_start_msg` and
    `_make_gen(players)` (its work generator); `draft` defaults to False if the operator has none.

    Responsiveness note: the work generator yields its renders as `_RENDER` sentinels; this modal
    runs each as a non-blocking INVOKE_DEFAULT job (tier 2) and resumes on render_complete, so the UI
    stays live and Cycles shows its own sample progress DURING each render. The synchronous execute()
    path (scripts / headless / the modal-start fallback) instead runs each `_RENDER` as a blocking
    render via _drain_render."""
    _start_msg = "starting..."

    def _make_gen(self, players):
        raise NotImplementedError

    def invoke(self, context, event):
        # Interactive launch (menu/panel): run as a modal job with a progress bar.
        _apply_settings(context.scene, draft=getattr(self, "draft", False))
        players = discover_players()
        errors = _preflight(players)
        if errors:
            for e in errors:
                self.report({'ERROR'}, e)
            return {'CANCELLED'}
        self.report({'INFO'}, "NovaSkin: starting export…")   # early feedback in the Info editor
        self._players = players
        try:
            self._gen = self._make_gen(players)
            self._wm = context.window_manager
            self._result = None
            self._render_pending = False     # tier-2: an INVOKE render in flight (see modal())
            self._render_done = self._render_cancelled = False
            self._render_h_done = self._render_h_cancel = None
            self._disp_saved = False
            self._wm.progress_begin(0.0, 1.0)
            # robust window: a panel-button context may not carry context.window in some builds
            win = context.window or (self._wm.windows[0] if self._wm.windows else None)
            self._timer = self._wm.event_timer_add(0.01, window=win)
            self._wm.modal_handler_add(self)
            _PROGRESS.update(running=True, frac=0.0, msg=self._start_msg, cancel=False)
            bpy.app.driver_namespace[_ACTIVE_KEY] = self   # for teardown on reload/unregister
            context.workspace.status_text_set(f"NovaSkin: {self._start_msg} (Esc to cancel)")
            _set_progress_header(f"NovaSkin: {self._start_msg} (Esc to cancel)")
            return {'RUNNING_MODAL'}
        except Exception as ex:
            # The modal couldn't start (context / timer / window). Fall back to a SYNCHRONOUS run so
            # the export still happens (the UI blocks, no progress bar, but it works and writes the
            # output). The warning surfaces why in the Info editor.
            self.report({'WARNING'},
                        f"NovaSkin: modal unavailable ({ex}); running synchronously (UI will block)")
            print(f"[NovaSkin] modal start failed: {ex!r}; falling back to synchronous execute()")
            _PROGRESS.update(running=False, frac=0.0, msg="", cancel=False)
            return self.execute(context)

    def execute(self, context):
        # Non-interactive launch (e.g. bpy.ops.render.novaskin() from a script, or the modal
        # fallback above): run the whole batch synchronously and block until done.
        _apply_settings(context.scene, draft=getattr(self, "draft", False))
        players = discover_players()
        errors = _preflight(players)
        if errors:
            for e in errors:
                self.report({'ERROR'}, e)
            return {'CANCELLED'}
        self._players = players
        try:
            self._result = _drain_render(self._make_gen(players))   # _RENDER -> blocking render here
            self.report({'INFO'}, f"NovaSkin: exported {len(players)} player(s) -> {OUT_DIR}")
            return {'FINISHED'}
        except Exception as ex:
            print(f"NovaSkin failed: {ex!r}")
            self.report({'ERROR'}, f"NovaSkin failed: {ex}")
            return {'CANCELLED'}

    def modal(self, context, event):
        if event.type == 'ESC' and event.value == 'PRESS':
            # confirm before throwing away a long batch: Esc arms, a second Esc within 3 s cancels
            # (the panel's Cancel button stays immediate -- a stray Esc in the viewport is not).
            import time
            if getattr(self, "_esc_armed_until", 0.0) > time.time():
                return self._finish(context, cancelled=True)
            self._esc_armed_until = time.time() + 3.0
            warn = "Press Esc again to CANCEL the export"
            context.workspace.status_text_set(warn)
            _set_progress_header(warn)
            return {'RUNNING_MODAL'}
        if event.type == 'TIMER':
            if getattr(self, "_render_pending", False):
                # An INVOKE_DEFAULT render is in flight. NEVER touch the scene now (restoring it would
                # free the compositor group the render is using -> crash) -- wait for render_complete,
                # THEN honour a pending cancel or resume. To stop the CURRENT render immediately the
                # user presses Esc, which the render's own modal handles (-> render_cancel -> below).
                if not self._render_done:
                    # render_complete is NOT a reliable "done" signal -- some renders finish without
                    # ever firing it (seen with the sprite beauty pass), which would hang us forever.
                    # The robust signal is the render JOB stopping. Use render_complete as a fast path
                    # and is_job_running() as the authority: once the job has run and then stopped,
                    # the render is done (resume; the gen reads the Viewer, retrying if it is empty).
                    import time
                    if bpy.app.is_job_running('RENDER'):
                        self._render_was_running = True
                        self._render_idle_since = None
                    else:
                        if getattr(self, "_render_idle_since", None) is None:
                            self._render_idle_since = time.time()
                        idle = time.time() - self._render_idle_since
                        # was_running: the render JOB stopped, but it is NOT fully released the instant
                        # is_job_running() flips -- resuming immediately makes the NEXT render read the
                        # OLD scene (the back-to-back sprite passes then render the whole scenery, so
                        # the silhouette never cuts the mob). Wait a short grace for the depsgraph /
                        # Viewer to settle. render_complete, when it fires, is immediate + correct.
                        # never ran: a silent INVOKE that started nothing -- give up after a long idle.
                        grace = 2.0 if getattr(self, "_render_was_running", False) else 120.0
                        if idle > grace:
                            self._render_done = True
                if not self._render_done:
                    if _PROGRESS.get("cancel"):
                        msg = "NovaSkin: cancelling after this render finishes  (press Esc to stop it now)"
                        context.workspace.status_text_set(msg)
                        _set_progress_header(msg)
                    return {'RUNNING_MODAL'}        # UI stays live; Cycles shows its sample progress
                self._teardown_render_wait()
                if self._render_cancelled or _PROGRESS.get("cancel"):
                    return self._finish(context, cancelled=True)
                return self._advance(context)       # resume the gen past `yield _RENDER`
            if _PROGRESS.get("cancel"):             # Cancel button in the panel (between renders)
                return self._finish(context, cancelled=True)
            return self._advance(context)
        return {'PASS_THROUGH'}

    def _advance(self, context):
        """Pull the next item from the work generator: a `_RENDER` sentinel means 'render now'
        (launched as a live INVOKE job); a (frac, msg) tuple is a progress update."""
        try:
            x = next(self._gen)
        except StopIteration as e:
            self._result = e.value
            return self._finish(context, cancelled=False)
        except Exception as ex:
            self.report({'ERROR'}, f"NovaSkin failed: {ex}")
            print(f"NovaSkin failed: {ex!r}")
            return self._finish(context, cancelled=True)
        if x is _RENDER:
            self._launch_render(context)
            return {'RUNNING_MODAL'}
        frac, msg = x
        self._wm.progress_update(frac)
        _PROGRESS.update(frac=frac, msg=msg)
        label = f"NovaSkin {frac * 100:.0f}%  -  {msg}  (Esc to cancel)"
        context.workspace.status_text_set(label)
        _set_progress_header(label)
        return {'RUNNING_MODAL'}

    def _launch_render(self, context):
        """Tier 2: run the render as a non-blocking INVOKE_DEFAULT job so the UI stays responsive and
        Cycles shows its own sample progress. A render_complete handler flips `_render_done` and the
        modal resumes the generator next tick (it then reads the Viewer). The result window is
        suppressed (display 'NONE'). Falls back to a blocking render if INVOKE is unavailable."""
        self._render_done = False
        self._render_cancelled = False
        def _on_done(scene, dg=None):
            self._render_done = True
        def _on_cancel(scene, dg=None):
            self._render_done = True
            self._render_cancelled = True
        self._render_h_done = _on_done
        self._render_h_cancel = _on_cancel
        bpy.app.handlers.render_complete.append(_on_done)
        bpy.app.handlers.render_cancel.append(_on_cancel)
        if not getattr(self, "_disp_saved", False):
            try:
                self._disp_prev = context.preferences.view.render_display_type
                context.preferences.view.render_display_type = 'NONE'
                self._disp_saved = True
            except Exception:
                pass
        self._render_pending = True
        self._render_was_running = False    # is_job_running() fallback state (see modal())
        self._render_idle_since = None
        try:
            bpy.ops.render.render('INVOKE_DEFAULT', write_still=False)
        except Exception as ex:
            print(f"[NovaSkin] INVOKE render unavailable ({ex!r}); blocking this render")
            bpy.ops.render.render(write_still=False)   # _on_done fires during this blocking call

    def _teardown_render_wait(self):
        for h, coll in ((getattr(self, "_render_h_done", None), bpy.app.handlers.render_complete),
                        (getattr(self, "_render_h_cancel", None), bpy.app.handlers.render_cancel)):
            if h is not None and h in coll:
                coll.remove(h)
        self._render_h_done = self._render_h_cancel = None
        self._render_pending = False

    def cancel(self, context):
        # Blender ends the modal by itself (file load, area close, add-on reload...). Restore.
        self._finish(context, cancelled=True)

    def _finish(self, context, cancelled):
        self._teardown_render_wait()                 # drop any in-flight render handlers
        if getattr(self, "_disp_saved", False):
            try:
                context.preferences.view.render_display_type = self._disp_prev
            except Exception:
                pass
            self._disp_saved = False
        bpy.app.driver_namespace.pop(_ACTIVE_KEY, None)
        wm = getattr(self, "_wm", None) or context.window_manager
        if getattr(self, "_timer", None) is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        wm.progress_end()
        _PROGRESS.update(running=False, frac=0.0, msg="", cancel=False)
        context.workspace.status_text_set(None)
        _set_progress_header(None)
        if cancelled and getattr(self, "_gen", None) is not None:
            self._gen.close()   # raises GeneratorExit at the yield -> finally -> sess.restore()
        self._gen = None
        if cancelled:
            self.report({'WARNING'}, "NovaSkin: cancelled (scene restored)")
            return {'CANCELLED'}
        self.report({'INFO'},
                    f"NovaSkin: exported {len(self._players)} player(s) -> {OUT_DIR}")
        return {'FINISHED'}


class RENDER_OT_novaskin(_NovaSkinModalMixin, bpy.types.Operator):
    """Export per-part UV + occlusion mask + illum/shadow per player to <blend>/novaskin/"""
    bl_idname = "render.novaskin"
    bl_label = "Render for NovaSkin"
    bl_options = {'REGISTER'}
    _start_msg = "starting..."

    draft: BoolProperty(
        name="Draft", default=False, options={'SKIP_SAVE'},
        description="Fast preview: render everything at 50% resolution with few samples "
                    "(framing/marking iteration -- not for the final export)")

    def _make_gen(self, players):
        return _render_steps(players, op=self)


class RENDER_OT_novaskin_animated(_NovaSkinModalMixin, bpy.types.Operator):
    """Export the ANIMATED wallpaper (scene frame range): background/foreground/light
    videos + the players' base-layer mesh stream (docs/animated-export-plan.md).
    Base layer only; optional-layer marks are ignored (they render as scenery)"""
    bl_idname = "render.novaskin_animated"
    bl_label = "Export Animation (beta)"
    bl_options = {'REGISTER'}
    _start_msg = "animated: starting..."

    draft: BoolProperty(
        name="Draft", default=False, options={'SKIP_SAVE'},
        description="Fast preview: 50% resolution, few samples")

    def _make_gen(self, players):
        return _anim_render_steps(players, op=self)


class RENDER_OT_novaskin_static(_NovaSkinModalMixin, bpy.types.Operator):
    """Export the STATIC wallpaper v2 (current frame): per-player UV light atlas + DENSE mesh
    (mesh.bin/positions.bin) + background/foreground (WebP) + manifest, to <OUT_DIR>/static/.
    Includes base + 2nd-layer overlays; lives alongside the legacy per-part UV export"""
    bl_idname = "render.novaskin_static"
    bl_label = "Export Static (mesh v2, beta)"
    bl_options = {'REGISTER'}
    _start_msg = "static: starting..."

    def _make_gen(self, players):
        return _static_export_steps(players, op=self)


class RENDER_OT_novaskin_cancel(bpy.types.Operator):
    """Cancel the running NovaSkin export (the scene is restored)"""
    bl_idname = "render.novaskin_cancel"
    bl_label = "Cancel NovaSkin export"

    def execute(self, context):
        _PROGRESS["cancel"] = True   # the modal operator picks this up on its next tick
        return {'FINISHED'}


def _is_player_armature(arm):
    """True if the armature is a player rig (Rig_ID) -- never a layer."""
    return (arm.type == 'ARMATURE' and RIG_ID_PROP in arm.keys()
            and (RIG_ID_VALUE is None or arm.get(RIG_ID_PROP) == RIG_ID_VALUE))


class OBJECT_OT_novaskin_layer_toggle(bpy.types.Operator):
    """Mark/unmark the selection as an optional NovaSkin layer. With objects SELECTED: an
    armature (or any mesh bound to one) marks the WHOLE rig as one layer, standalone meshes
    mark individually. With NOTHING selected: marks the ACTIVE collection (all its meshes =
    one layer). Each layer exports as an independent toggleable group (beauty + shadow,
    + UV/light for single-mesh layers)"""
    bl_idname = "object.novaskin_layer_toggle"
    bl_label = "Mark as Optional Layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if any(o.type in {'MESH', 'ARMATURE'} for o in context.selected_objects):
            return True
        c = context.collection
        return c is not None and any(o.type == 'MESH' for o in c.all_objects)

    def execute(self, context):
        sel = [o for o in context.selected_objects if o.type in {'MESH', 'ARMATURE'}]
        if not sel:
            return self._toggle_collection(context)

        # Resolve the selection to mark-TARGETS: an armature (whole rig) for any
        # armature/rigged-mesh, else the standalone mesh. Dedup so selecting a rig + its
        # meshes marks the rig once.
        targets, seen = [], set()
        for o in sel:
            tgt = o if o.type == 'ARMATURE' else (_mesh_rig_armature(o) or o)
            if tgt.name not in seen:
                seen.add(tgt.name)
                targets.append(tgt)

        marked = unmarked = skipped = 0
        for tgt in targets:
            if _is_player_armature(tgt):
                skipped += 1
                print(f"[LAYER] '{tgt.name}' is a player rig -- not marked.")
            elif LAYER_ID_PROP in tgt.keys():
                del tgt[LAYER_ID_PROP]
                unmarked += 1
            else:
                tgt[LAYER_ID_PROP] = 1
                marked += 1
                print(f"[LAYER] marked {'rig' if tgt.type == 'ARMATURE' else 'mesh'} "
                      f"'{tgt.name}'")
        parts = ([f"{marked} marked"] if marked else []) \
            + ([f"{unmarked} unmarked"] if unmarked else []) \
            + ([f"{skipped} skipped (player rig)"] if skipped else [])
        self.report({'WARNING'} if skipped else {'INFO'},
                    "NovaSkin layers: " + (", ".join(parts) or "nothing selectable"))
        return {'FINISHED'}

    def _toggle_collection(self, context):
        """Nothing selected -> mark/unmark the active collection (all its meshes = 1 layer)."""
        coll = context.collection
        if coll is None or not any(o.type == 'MESH' for o in coll.all_objects):
            self.report({'WARNING'},
                        "NovaSkin layers: nothing selected and the active collection is empty")
            return {'CANCELLED'}
        if any(_is_player_armature(o) for o in coll.all_objects):
            self.report({'ERROR'},
                        f"'{coll.name}' contains a player rig -- not marked as a layer.")
            return {'CANCELLED'}
        if LAYER_ID_PROP in coll.keys():
            del coll[LAYER_ID_PROP]
            self.report({'INFO'}, f"NovaSkin layers: unmarked collection '{coll.name}'")
        else:
            coll[LAYER_ID_PROP] = 1
            n = sum(1 for o in coll.all_objects if o.type == 'MESH')
            self.report({'INFO'},
                        f"NovaSkin layers: marked collection '{coll.name}' ({n} mesh)")
        return {'FINISHED'}


class OBJECT_OT_novaskin_layer_remove(bpy.types.Operator):
    """Remove this entry from the optional layers (unmark it)"""
    bl_idname = "object.novaskin_layer_remove"
    bl_label = "Remove Optional Layer"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    target: StringProperty()
    is_collection: BoolProperty(default=False)

    def execute(self, context):
        coll = bpy.data.collections if self.is_collection else bpy.data.objects
        db = coll.get(self.target)
        if db is None or LAYER_ID_PROP not in db.keys():
            self.report({'WARNING'}, f"NovaSkin: '{self.target}' is not a marked layer")
            return {'CANCELLED'}
        del db[LAYER_ID_PROP]
        self.report({'INFO'}, f"NovaSkin layers: removed '{self.target}'")
        return {'FINISHED'}


def _menu_draw(self, context):
    self.layout.operator(RENDER_OT_novaskin.bl_idname, icon='RENDER_STILL').draft = False
    self.layout.operator(RENDER_OT_novaskin.bl_idname, text="Render Draft (NovaSkin preview)",
                         icon='MOD_FLUID').draft = True


class VIEW3D_PT_novaskin(bpy.types.Panel):
    """NovaSkin export options + run button, in the 3D Viewport sidebar (press N)."""
    bl_label = "NovaSkin Export"
    bl_idname = "VIEW3D_PT_novaskin"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "NovaSkin"

    def draw(self, context):
        layout = self.layout
        st = context.scene.novaskin
        running = _PROGRESS["running"]

        # Live progress + cancel stay visible on every tab while a render runs.
        if running:
            col = layout.column()
            pct = _PROGRESS["frac"] * 100.0
            try:
                col.progress(factor=_PROGRESS["frac"],
                             text=f"{pct:.0f}%  -  {_PROGRESS['msg']}", type='BAR')
            except (AttributeError, TypeError):   # older Blender without UILayout.progress
                col.label(text=f"NovaSkin  {pct:.0f}%  -  {_PROGRESS['msg']}")
            row = col.row()
            row.scale_y = 1.3
            row.operator("render.novaskin_cancel", text="Cancel", icon='CANCEL')
            col.label(text="(or Esc in the viewport)")

        # No-rig warning stays visible on every tab (the detail + link live in the Layers tab).
        if not _player_armatures():
            layout.label(text="No Thomas rig found — see the Layers tab", icon='ERROR')

        layout.row().prop(st, "ui_tab", expand=True)
        tab = st.ui_tab

        if tab == 'EXPORT':
            box = layout.box()
            box.label(text="Layer Options", icon='RENDERLAYERS')
            box.prop(st, "export_mesh")           # mesh v2 (new) vs legacy per-part
            if st.export_mesh:
                box.prop(st, "atlas_res")         # static (mesh v2) UV light-atlas size
                col = box.column(align=True)
                col.label(text="Steps (off = reuse previous export):")
                col.prop(st, "static_background")
                col.prop(st, "static_foreground")     # also covers the water/glass tint
                col.prop(st, "static_atlas")
                col.prop(st, "static_shadows")
                col.prop(st, "static_sprites")
                col.prop(st, "static_masks")
            else:
                box.prop(st, "export_backface_uv")
                box.prop(st, "export_illum")
                box.prop(st, "export_shadow")
                box.prop(st, "composite_base_layer")
                box.prop(st, "export_background")

            box = layout.box()
            box.label(text="Quality", icon='SETTINGS')
            box.prop(st, "illum_samples")
            box.label(text="render samples for the whole export", icon='LIGHT')

            box = layout.box()
            box.label(text="Output", icon='FILE_FOLDER')
            box.prop(st, "out_dir")
            if os.path.isdir(_abs(st.out_dir)):
                box.operator("wm.path_open", text="Open Output Folder",
                             icon='FOLDER_REDIRECT').filepath = _abs(st.out_dir)

            if not running:               # while running, the top progress bar is the indicator
                box = layout.box()
                box.label(text="Render", icon='RENDER_STILL')
                col = box.column()
                col.scale_y = 1.4
                if st.export_mesh:
                    col.operator("render.novaskin_static", text="Render", icon='MESH_DATA')
                else:
                    col.operator("render.novaskin", text="Render",
                                 icon='RENDER_STILL').draft = False
                    box.operator("render.novaskin", text="Render Draft (test only)",
                                 icon='MOD_FLUID').draft = True

            if WALLPAPER_TOOL_URL:
                # web links honor the "Allow Online Access" preference (extension guideline)
                row = layout.row()
                row.enabled = bpy.app.online_access
                row.operator("wm.url_open", text="Open Wallpaper Tool",
                             icon='URL').url = WALLPAPER_TOOL_URL
                if not bpy.app.online_access:
                    layout.label(text="(enable Allow Online Access for web links)",
                                 icon='INFO')

        elif tab == 'LAYERS':
            box = layout.box()
            box.label(text="Rig", icon='ARMATURE_DATA')
            box.prop(st, "fix_2layer_position")
            arms = _player_armatures()
            if arms:
                box.label(text=f"{len(arms)} player(s) detected:",
                          icon='OUTLINER_OB_ARMATURE')
                col = box.column(align=True)
                for a in arms:
                    col.label(text=a.name, icon='DOT')
            else:
                box.label(text=f"no '{RIG_ID_VALUE or RIG_ID_PROP}' rig found", icon='ERROR')
                box.label(text="This exporter needs the Thomas Rig Legacy.")
                row = box.row()
                row.enabled = bpy.app.online_access
                row.operator("wm.url_open", text="Get the rig (extensions.blender.org)",
                             icon='URL').url = RIG_SOURCE_URL

            box = layout.box()
            box.label(text="Optional Layers", icon='OUTLINER_OB_MESH')
            box.operator("object.novaskin_layer_toggle", icon='PINNED')
            if not any(o.type in {'MESH', 'ARMATURE'} for o in context.selected_objects):
                c = context.collection
                box.label(text=f"(active collection: {c.name})" if c else "(nothing selected)",
                          icon='OUTLINER_COLLECTION')
            groups = discover_layers()
            if groups:
                col = box.column(align=True)
                icons = {"collection": 'OUTLINER_COLLECTION', "armature": 'ARMATURE_DATA',
                         "mesh": 'LAYER_ACTIVE'}
                for g in groups:
                    n = len(g["meshes"])
                    txt = f"{g['object']}  ({n} mesh)" if n > 1 else g["object"]
                    is_coll = (g["kind"] == "collection")
                    holder = (bpy.data.collections.get(g["object"]) if is_coll
                              else context.scene.objects.get(g["object"]))
                    row = col.row(align=True)
                    row.label(text=txt, icon=icons.get(g["kind"], 'LAYER_ACTIVE'))
                    if holder is not None:           # per-layer mode: Sprite / Texture / Player
                        sub = row.row(align=True)
                        sub.ui_units_x = 5.0         # fixed -> the name keeps the rest of the row
                        sub.prop(holder, LAYER_MODE_PROP, text="")
                    rm = row.operator("object.novaskin_layer_remove", text="", icon='X',
                                      emboss=False)
                    rm.target = g["object"]
                    rm.is_collection = is_coll
                box.label(text="(Sprite = flat image; Texture/Player = re-skinnable mesh)",
                          icon='INFO')
            else:
                box.label(text="none marked", icon='LAYER_USED')

        elif tab == 'ANIM':
            box = layout.box()
            box.label(text="Animated (beta)", icon='RENDER_ANIMATION')
            if not running:               # while running, the top progress bar is the indicator
                box.operator("render.novaskin_animated", icon='RENDER_ANIMATION').draft = False
                box.operator("render.novaskin_animated",
                             text=f"Export Animation Draft (~{ANIM_DRAFT_SECONDS}s)",
                             icon='MOD_FLUID').draft = True
            box.label(text=f"frames {context.scene.frame_start}-{context.scene.frame_end}"
                           f" @ {context.scene.render.fps} fps, base layer only")
            box.label(text=f"(draft renders only the first ~{ANIM_DRAFT_SECONDS}s)",
                      icon='INFO')


_classes = (NovaSkinSettings, RENDER_OT_novaskin, RENDER_OT_novaskin_animated,
            RENDER_OT_novaskin_static, RENDER_OT_novaskin_cancel,
            OBJECT_OT_novaskin_layer_toggle, OBJECT_OT_novaskin_layer_remove,
            VIEW3D_PT_novaskin)


def _teardown_active_batch():
    """If a modal batch is mid-render, finish it (restoring the scene) before unregistering
    -- otherwise a reload would orphan the modal operator and leave the scene with the temp
    materials/visibility/render settings. Stored in driver_namespace, so it survives reloads."""
    op = bpy.app.driver_namespace.pop(_ACTIVE_KEY, None)
    if op is None:
        return
    try:
        print("NovaSkin: a batch was running -- restoring the scene before reload.")
        op._finish(bpy.context, cancelled=True)
    except Exception as e:
        print("NovaSkin: teardown on unregister failed:", repr(e))


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.novaskin = PointerProperty(type=NovaSkinSettings)
    # per-layer export mode (Sprite / Texture / Player), set on the marked object or collection
    for T in (bpy.types.Object, bpy.types.Collection):
        setattr(T, LAYER_MODE_PROP, EnumProperty(
            name="Layer", items=LAYER_MODE_ITEMS, default='SPRITE',
            description="How this optional layer is exported"))
    bpy.types.TOPBAR_MT_render.append(_menu_draw)


def unregister():
    _teardown_active_batch()
    bpy.types.TOPBAR_MT_render.remove(_menu_draw)
    for T in (bpy.types.Object, bpy.types.Collection):
        if hasattr(T, LAYER_MODE_PROP):
            delattr(T, LAYER_MODE_PROP)
    if hasattr(bpy.types.Scene, "novaskin"):
        del bpy.types.Scene.novaskin
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
    print("NovaSkin: registered. Run it from  Render > Render for NovaSkin  "
          "(or bpy.ops.render.novaskin()).")
