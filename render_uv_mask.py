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
ANIM_QUANT = 8.0            # vertex quantization: 1/8 px (int16)
ANIM_CRF = {"bg": 32, "fg": 32, "light": 38}   # VP9 quality per stream
ANIM_ENCODE = True          # run ffmpeg after rendering (else keep PNG sequences + script)
ANIM_KEEP_SEQUENCES = False  # keep seq/ PNGs after a successful encode
ANIM_BASE_LABELS = ["head", "body", "arm_left_classic", "arm_right_classic",
                    "leg_left", "leg_right"]

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
    scene_meshes = {o.name for o in scene.objects if o.type == 'MESH'}
    groups, claimed = [], set()

    def _add(name, source_name, kind, meshes):
        meshes = [m for m in meshes if m.name in scene_meshes and m.name not in claimed]
        if meshes:
            groups.append({"name": _layer_safe_name(name), "object": source_name,
                           "kind": kind, "meshes": meshes})
            claimed.update(m.name for m in meshes)

    for coll in bpy.data.collections:               # 1) marked collections
        if LAYER_ID_PROP in coll.keys():
            _add(coll.name, coll.name, "collection",
                 [o for o in coll.all_objects if o.type == 'MESH'])
    for o in scene.objects:                          # 2) marked armatures (whole rig)
        if o.type == 'ARMATURE' and LAYER_ID_PROP in o.keys():
            _add(o.name, o.name, "armature", _rig_meshes(o))
    for o in scene.objects:                          # 3) standalone marked meshes
        if o.type == 'MESH' and LAYER_ID_PROP in o.keys():
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

    def render_pass(self, socket_name, engine, samples, res_pct):
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
        s.render.engine = engine
        if hasattr(s, 'cycles'):
            s.cycles.samples = samples
            s.cycles.use_denoising = False   # data pass (UV/Depth/ObjectIndex): NEVER denoise
        s.render.resolution_percentage = res_pct
        s.render.film_transparent = True
        s.render.use_compositing = True
        bpy.context.view_layer.update()
        bpy.ops.render.render(write_still=False)
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


def _render_combined_array(sess, res_pct, transparent=False, use_scene_settings=False):
    """Render the Combined pass (beauty, with denoise) -> linear RGBA array, read via the
    Viewer. transparent=True renders over a transparent film (alpha = coverage).
    use_scene_settings=True renders with the USER's engine/samples/denoise (from the
    _Session snapshot) -- for final-look images (layer beauty), like background.png."""
    s = sess.s
    ng = sess.ng
    vin = sess.viewer.inputs[0]
    for l in list(vin.links):
        ng.links.remove(l)
    img_sock = sess.rl.outputs.get('Image')
    ng.links.new(img_sock, vin)
    if sess.out_node:
        sess.out_node.mute = True
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
    bpy.ops.render.render(write_still=False)
    vimg = bpy.data.images.get('Viewer Node')
    if not vimg or len(vimg.pixels) == 0:
        # transient empty Viewer (same hiccup the depth probe retries) -- retry once
        print("[render] empty Viewer node on the combined render -- retrying once")
        bpy.ops.render.render(write_still=False)
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
    """anim.bin v2: 'NSKA' header + zlib payload of int16 (x, y, z) per vertex for K mesh
    keys -- key 0 absolute, key 1 delta, keys 2+ delta-of-delta (linear motion predictor).
    x/y are pixels x `quant` (1/8 px); z is camera depth normalized to [zmin, zmax] from
    the header x 32767 -- one scale across all players, for the GPU depth test."""
    import struct, zlib
    K, V = len(keys), keys[0].shape[0]
    zmin = float(min(k[:, 2].min() for k in keys))
    zmax = float(max(k[:, 2].max() for k in keys))
    if zmax - zmin < 1e-6:
        zmax = zmin + 1e-6
    keys_q = []
    for k in keys:
        q = np.empty((V, 3), '<i2')
        q[:, :2] = np.round(k[:, :2] * quant)
        q[:, 2] = np.round((k[:, 2] - zmin) / (zmax - zmin) * 32767.0)
        keys_q.append(q)
    stream = [keys_q[0].tobytes()]
    if K > 1:
        deltas = [(b - a) for a, b in zip(keys_q, keys_q[1:])]
        stream.append(deltas[0].tobytes())
        stream += [(b - a).tobytes() for a, b in zip(deltas, deltas[1:])]
    payload = zlib.compress(b"".join(stream), 9)
    with open(path, "wb") as f:
        f.write(struct.pack('<4sIIIffff', b'NSKA', 2, V, K,
                            float(quant), float(keys_fps), zmin, zmax))
        f.write(payload)
    return K, V


def _anim_encode(adir, fps):
    """Encode the PNG sequences to WebM/VP9 with ffmpeg (foreground keeps alpha). Returns
    (ok, message); if ffmpeg is missing, writes encode.sh with the commands instead."""
    import shutil as _sh, subprocess
    seq = os.path.join(adir, "seq")
    jobs = [("bg", "background.webm", ["-pix_fmt", "yuv420p"]),
            ("fg", "foreground.webm", ["-pix_fmt", "yuva420p", "-auto-alt-ref", "0"]),
            ("light", "light.webm", ["-pix_fmt", "yuv420p"])]
    # GUI Blender on macOS doesn't inherit the shell PATH -- look in the usual spots too.
    ff = _sh.which("ffmpeg") or next(
        (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                     "/usr/bin/ffmpeg") if os.path.exists(p)), None)
    def cmd(name, out, extra):
        return [ff or "ffmpeg", "-y", "-framerate", str(fps),
                "-i", os.path.join(seq, name, "%04d.png"),
                "-c:v", "libvpx-vp9", "-crf", str(ANIM_CRF[name]), "-b:v", "0",
                "-row-mt", "1", "-cpu-used", "4", *extra, os.path.join(adir, out)]
    if ff is None:
        sh = os.path.join(adir, "encode.sh")
        with open(sh, "w") as f:
            f.write("#!/bin/sh\n# ffmpeg not found at export time -- run this manually\n")
            for name, out, extra in jobs:
                f.write(" ".join(cmd(name, out, extra)) + "\n")
        os.chmod(sh, 0o755)
        return False, "ffmpeg not found -- wrote encode.sh"
    for name, out, extra in jobs:
        r = subprocess.run(cmd(name, out, extra), capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"ffmpeg failed on {out}: {r.stderr[-400:]}"
    return True, "encoded background/foreground/light .webm"


def _anim_render_steps(players, op=None, frame_start=None, frame_end=None):
    """Generator for the animated export (one yield per unit of work, like _render_steps).
    Per frame: background (players camera-invisible, shadows baked), foreground (per-pixel:
    scenery-holdout AND player silhouette), combined light; plus mesh keys every
    ANIM_KEYS_STEP frames. Writes mesh.bin/anim.bin/manifest.json and encodes the videos."""
    s = bpy.context.scene
    f0 = s.frame_start if frame_start is None else frame_start
    f1 = s.frame_end if frame_end is None else frame_end
    nf = f1 - f0 + 1
    fps = s.render.fps
    W = s.render.resolution_x * MASK_RES_PCT // 100
    H = s.render.resolution_y * MASK_RES_PCT // 100
    adir = os.path.join(_abs(OUT_DIR), ANIM_OUT_SUBDIR)
    for k in ("bg", "fg", "light"):
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

        keys, key_frames = [], []
        gray = _gray_diffuse_material()
        scenery = [o for o in s.objects if o.type == 'MESH' and o.name not in char_names]
        for i, f in enumerate(range(f0, f1 + 1)):
            s.frame_set(f)
            if i % ANIM_KEYS_STEP == 0 or f == f1:
                setup()
                keys.append(_anim_frame_positions(st, W, H))
                key_frames.append(i)
            fn = f"{i+1:04d}.png"
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
            fgb = np.zeros((C.shape[0], 4), 'float32')
            fgb[:, :3] = _to_display(straight)
            fgb[:, 3] = np.where(D[:, 3] > 0.5, ca, 0.0)
            _save_image(fgb.reshape(-1), rW, rH, os.path.join(adir, "seq", "fg", fn))
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
            lbuf = np.ones((L.shape[0], 4), 'float32')
            lbuf[:, :3] = _lin_to_srgb(lst)
            _save_image(lbuf.reshape(-1), rW, rH, os.path.join(adir, "seq", "light", fn))
            yield prog(f"frame {i+1}/{nf} light")

        welded, unique, ntris = _anim_write_mesh(os.path.join(adir, "mesh.bin"), st)
        K, V = _anim_write_anim(os.path.join(adir, "anim.bin"), keys,
                                ANIM_QUANT, fps / ANIM_KEYS_STEP)
        manifest = {
            "animated_version": 1,
            "addon_version": ADDON_VERSION,
            "fps": fps,
            "frames": nf,
            "resolution": [W, H],
            "videos": {"background": "background.webm",
                       "foreground": "foreground.webm",
                       "light": "light.webm"},
            "mesh": {"file": "mesh.bin", "welded": welded, "unique": unique,
                     "tris": ntris, "players": st["players"],
                     "layout": "NSKM u32x4 header + zlib(uv u16x2/65535, src u16, tris u16x3)"},
            "anim": {"file": "anim.bin", "keys": K, "verts": V, "quant": ANIM_QUANT,
                     "keys_fps": fps / ANIM_KEYS_STEP, "predictor": "delta2",
                     "layout": ("NSKA v2 header (+zmin/zmax) + zlib(int16 xyz: abs, "
                                "delta, then delta-of-delta); z = camera depth for the "
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
    export_backface_uv: BoolProperty(name="Back Faces", default=True)
    export_illum: BoolProperty(name="Illum", default=True)
    export_shadow: BoolProperty(name="Shadow", default=True)
    composite_base_layer: BoolProperty(name="Base Layer Composite", default=True)
    export_background: BoolProperty(name="Background (no players/layers)", default=True)
    fix_2layer_position: BoolProperty(
        name="Fix Hat Position and Scale", default=True,
        description="Snap the hat (2_Layer_Extrusion) onto the head and scale it to the "
                    "Minecraft hat size (a bit bigger than the head)")
    illum_samples: IntProperty(name="Illum samples", default=48, min=1, max=4096)
    lightshadow_format: EnumProperty(
        name="Illum/Shadow",
        items=[('JPEG', "JPEG", ""),
               ('WEBP', "WebP", "Smaller than JPEG at the same quality; browser-friendly"),
               ('PNG', "PNG", "")],
        default='JPEG')
    jpeg_quality: IntProperty(name="Quality", default=90, min=1, max=100,
                              description="JPEG/WebP quality")


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
    g["FIX_2LAYER_POSITION"] = st.fix_2layer_position
    g["LIGHTSHADOW_FORMAT"] = st.lightshadow_format
    g["JPEG_QUALITY"] = st.jpeg_quality
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


class RENDER_OT_novaskin(bpy.types.Operator):
    """Export per-part UV + occlusion mask + illum/shadow per player to <blend>/novaskin/"""
    bl_idname = "render.novaskin"
    bl_label = "Render for NovaSkin"
    bl_options = {'REGISTER'}

    draft: BoolProperty(
        name="Draft", default=False, options={'SKIP_SAVE'},
        description="Fast preview: render everything at 50% resolution with few samples "
                    "(framing/marking iteration -- not for the final export)")

    # Note on responsiveness: bpy is single-threaded and not thread-safe, so an actual
    # render call always blocks the main thread for its duration. The modal/timer below
    # runs the work in many small chunks and redraws + updates progress between them, and
    # lets the user cancel with Esc -- the UI is responsive between steps (each step still
    # blocks briefly while its render runs). True non-blocking rendering is not possible.

    def invoke(self, context, event):
        # Interactive launch (from the menu/panel): run as a modal job with a progress bar.
        _apply_settings(context.scene, draft=self.draft)
        players = discover_players()
        errors = _preflight(players)
        if errors:
            for e in errors:
                self.report({'ERROR'}, e)
            return {'CANCELLED'}
        self._players = players
        self._gen = _render_steps(players, op=self)
        self._wm = context.window_manager
        self._result = None
        self._wm.progress_begin(0.0, 1.0)
        self._timer = self._wm.event_timer_add(0.001, window=context.window)
        self._wm.modal_handler_add(self)
        _PROGRESS.update(running=True, frac=0.0, msg="starting...", cancel=False)
        bpy.app.driver_namespace[_ACTIVE_KEY] = self   # for teardown on reload/unregister
        context.workspace.status_text_set("NovaSkin: starting... (Esc to cancel)")
        _set_progress_header("NovaSkin: starting... (Esc to cancel)")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC' and event.value == 'PRESS':
            # confirm before throwing away a long batch: Esc arms, a second Esc within
            # 3 s cancels (the panel's Cancel button stays immediate -- clicking it is
            # deliberate; a stray Esc with the mouse in the viewport is not).
            import time
            if getattr(self, "_esc_armed_until", 0.0) > time.time():
                return self._finish(context, cancelled=True)
            self._esc_armed_until = time.time() + 3.0
            warn = "Press Esc again to CANCEL the export"
            context.workspace.status_text_set(warn)
            _set_progress_header(warn)
            return {'RUNNING_MODAL'}
        if event.type == 'TIMER':
            if _PROGRESS.get("cancel"):           # Cancel button in the panel
                return self._finish(context, cancelled=True)
            try:
                frac, msg = next(self._gen)
            except StopIteration as e:
                self._result = e.value
                return self._finish(context, cancelled=False)
            except Exception as ex:
                self.report({'ERROR'}, f"NovaSkin failed: {ex}")
                print(f"NovaSkin failed: {ex!r}")
                return self._finish(context, cancelled=True)
            self._wm.progress_update(frac)
            _PROGRESS.update(frac=frac, msg=msg)
            label = f"NovaSkin {frac * 100:.0f}%  -  {msg}  (Esc to cancel)"
            context.workspace.status_text_set(label)
            _set_progress_header(label)
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def cancel(self, context):
        # Blender ends the modal by itself (file load, area close, add-on reload...). Restore.
        self._finish(context, cancelled=True)

    def _finish(self, context, cancelled):
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

    def execute(self, context):
        # Non-interactive launch (e.g. bpy.ops.render.novaskin() from a script): run the
        # whole batch synchronously and block until done.
        return {'FINISHED'} if render_all(op=self, draft=self.draft) is not None \
            else {'CANCELLED'}


class RENDER_OT_novaskin_animated(RENDER_OT_novaskin):
    """Export the ANIMATED wallpaper (scene frame range): background/foreground/light
    videos + the players' base-layer mesh stream (docs/animated-export-plan.md).
    Base layer only; optional-layer marks are ignored (they render as scenery)"""
    bl_idname = "render.novaskin_animated"
    bl_label = "Export Animation (beta)"
    bl_options = {'REGISTER'}

    draft: BoolProperty(
        name="Draft", default=False, options={'SKIP_SAVE'},
        description="Fast preview: 50% resolution, few samples")

    def invoke(self, context, event):
        _apply_settings(context.scene, draft=self.draft)
        players = discover_players()
        errors = _preflight(players)
        if errors:
            for e in errors:
                self.report({'ERROR'}, e)
            return {'CANCELLED'}
        self._players = players
        self._gen = _anim_render_steps(players, op=self)
        self._wm = context.window_manager
        self._result = None
        self._wm.progress_begin(0.0, 1.0)
        self._timer = self._wm.event_timer_add(0.001, window=context.window)
        self._wm.modal_handler_add(self)
        _PROGRESS.update(running=True, frac=0.0, msg="animated: starting...", cancel=False)
        bpy.app.driver_namespace[_ACTIVE_KEY] = self
        context.workspace.status_text_set("NovaSkin animated: starting... (Esc to cancel)")
        _set_progress_header("NovaSkin animated: starting... (Esc to cancel)")
        return {'RUNNING_MODAL'}

    def execute(self, context):
        # synchronous run (scripts): drain the generator
        _apply_settings(context.scene, draft=self.draft)
        players = discover_players()
        errors = _preflight(players)
        if errors:
            for e in errors:
                self.report({'ERROR'}, e)
            return {'CANCELLED'}
        gen = _anim_render_steps(players, op=self)
        try:
            while True:
                next(gen)
        except StopIteration:
            return {'FINISHED'}
        except Exception as ex:
            self.report({'ERROR'}, f"NovaSkin animated failed: {ex}")
            return {'CANCELLED'}


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

        if _PROGRESS["running"]:
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
        else:
            col = layout.column()
            col.scale_y = 1.5
            col.operator("render.novaskin", icon='RENDER_STILL').draft = False
            row = layout.row()
            row.operator("render.novaskin", text="Render Draft (fast preview)",
                         icon='MOD_FLUID').draft = True

        if WALLPAPER_TOOL_URL:
            # Web links honor the "Allow Online Access" preference (bpy.app.online_access):
            # disabled in offline mode, as the extension guidelines require.
            row = layout.row()
            row.enabled = bpy.app.online_access
            row.operator("wm.url_open", text="Open Wallpaper Tool",
                         icon='URL').url = WALLPAPER_TOOL_URL
            if not bpy.app.online_access:
                layout.label(text="(enable Allow Online Access for web links)",
                             icon='INFO')

        box = layout.box()
        box.label(text="Output", icon='FILE_FOLDER')
        box.prop(st, "out_dir")
        if os.path.isdir(_abs(st.out_dir)):
            box.operator("wm.path_open", text="Open Output Folder",
                         icon='FOLDER_REDIRECT').filepath = _abs(st.out_dir)
        box.prop(st, "uv_format")
        if st.uv_format == 'OPEN_EXR':
            row = box.row(align=True)
            row.prop(st, "exr_half", toggle=True)
            row.prop(st, "exr_codec", text="")
            box.label(text="+ light layer (when illum on)", icon='LIGHT')

        box = layout.box()
        box.label(text="Layers", icon='RENDERLAYERS')
        box.prop(st, "export_backface_uv")
        box.prop(st, "export_illum")
        box.prop(st, "export_shadow")
        box.prop(st, "composite_base_layer")
        box.prop(st, "export_background")

        box = layout.box()
        box.label(text="Quality", icon='SETTINGS')
        box.prop(st, "illum_samples")
        row = box.row(align=True)
        row.prop(st, "lightshadow_format", text="")
        if st.lightshadow_format in {'JPEG', 'WEBP'}:
            row.prop(st, "jpeg_quality", text="Q")

        box = layout.box()
        box.label(text="Optional Layers", icon='OUTLINER_OB_MESH')
        box.operator("object.novaskin_layer_toggle", icon='PINNED')
        # hint: which source the one button will act on right now
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
                row = col.row(align=True)
                row.label(text=txt, icon=icons.get(g["kind"], 'LAYER_ACTIVE'))
                rm = row.operator("object.novaskin_layer_remove", text="", icon='X',
                                  emboss=False)
                rm.target = g["object"]
                rm.is_collection = (g["kind"] == "collection")
        else:
            box.label(text="none marked", icon='LAYER_USED')

        box = layout.box()
        box.label(text="Animated (beta)", icon='RENDER_ANIMATION')
        box.operator("render.novaskin_animated", icon='RENDER_ANIMATION').draft = False
        box.operator("render.novaskin_animated", text="Export Animation Draft",
                     icon='MOD_FLUID').draft = True
        box.label(text=f"frames {context.scene.frame_start}-{context.scene.frame_end}"
                       f" @ {context.scene.render.fps} fps, base layer only")

        box = layout.box()
        box.label(text="Rig", icon='ARMATURE_DATA')
        box.prop(st, "fix_2layer_position")
        arms = _player_armatures()
        if arms:
            box.label(text=f"{len(arms)} player(s) detected:", icon='OUTLINER_OB_ARMATURE')
            col = box.column(align=True)
            for a in arms:
                col.label(text=a.name, icon='DOT')
        else:
            box.label(text=f"no '{RIG_ID_VALUE or RIG_ID_PROP}' rig found", icon='ERROR')
            box.label(text="This exporter needs the Thomas Rig Legacy.")
            row = box.row()
            row.enabled = bpy.app.online_access   # honor offline mode (extension guideline)
            row.operator("wm.url_open", text="Get the rig (extensions.blender.org)",
                         icon='URL').url = RIG_SOURCE_URL


_classes = (NovaSkinSettings, RENDER_OT_novaskin, RENDER_OT_novaskin_animated,
            RENDER_OT_novaskin_cancel, OBJECT_OT_novaskin_layer_toggle,
            OBJECT_OT_novaskin_layer_remove, VIEW3D_PT_novaskin)


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
    bpy.types.TOPBAR_MT_render.append(_menu_draw)


def unregister():
    _teardown_active_batch()
    bpy.types.TOPBAR_MT_render.remove(_menu_draw)
    if hasattr(bpy.types.Scene, "novaskin"):
        del bpy.types.Scene.novaskin
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    # Re-running from the Text Editor: refresh the registration cleanly.
    try:
        unregister()
    except Exception:
        pass
    register()
    print("NovaSkin: registered. Run it from  Render > Render for NovaSkin  "
          "(or bpy.ops.render.novaskin()).")
