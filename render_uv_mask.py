"""Export per-part UV + (occlusion) mask per player, for N players in the scene.

Outputs (one subfolder per player):
  - <OUT_DIR>/<armature>/<part>_UV.png        (PNG, RAW/Non-Color; 8-bit by default)
        R=U, G=V, B=depth(per character), A=coverage (1 inside the part, 0 outside).
        As EXR (UV_FORMAT='OPEN_EXR') with the illum pass on, it is "<part>_UVDL.exr" with
        the same RGBA plus a "light" layer (light.R/G/B = sRGB illum color).
        <part> is a Minecraft-style label (head/hat, body/jacket, arm/sleeve, leg/pant,
        with _left/_right and _classic/_slim where relevant). See MC_PART_MAP; the manifest
        records the label -> object-name mapping.
  - <OUT_DIR>/<armature>/base_layer_<classic|slim>.png  (base parts composited per arm
        variant, nearest pixel wins)
  - <OUT_DIR>/<armature>/mask_<classic|slim>.png

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
    "version": (1, 0, 0),
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
OUT_DIR = "//novaskin/"

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
# Opaque material override during the mask (the skin has alpha -> it would punch holes
# in the mask). Object Index ignores color; it only needs to be opaque. Gray ~#808080.
MASK_OVERRIDE_RGBA = (0.5, 0.5, 0.5, 1.0)

# Also export the UV of the BACK FACES (a material that only renders back faces):
# produces <part>_UV_back.png in addition to <part>_UV.png (front faces).
EXPORT_BACKFACE_UV = True

# Channels of the exported UV: R=U, G=V always. If True, B = DEPTH normalized (0..1)
# by the CHARACTER's depth range (camera Z); otherwise B stays 1.0.
UV_DEPTH_IN_BLUE = True

# Per-PLAYER illum + shadow (solves occlusion between players). Renders the scene with
# only 1 player in gray at a time (others hidden), per arm variant; from the SAME render
# we extract the body's illumination (masked by the player's mask) AND the shadow it casts
# on the scenery (ratio vs the clean scene). Replaces the global illum.
EXPORT_PLAYER_ILLUM_SHADOW = True
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

# Render of the FULL scene WITHOUT the players (empty scene) -> background_no_players.png.
# Caveat: it also hides the shadows the players would cast (refine later with holdout).
EXPORT_BACKGROUND_NO_PLAYERS = True

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
# UV + base_layer format: 'PNG' (8/16-bit) or 'OPEN_EXR' (float). EXR avoids the 2D-canvas
# premultiply problem (when alpha carries the light) AND removes 8-bit quantization, but the
# files are bigger and need a float EXR loader to read (browsers can't decode EXR via canvas).
UV_FORMAT = 'PNG'                  # 'PNG' or 'OPEN_EXR'
EXR_HALF = True                    # half-float (16-bit) EXR -> smaller; else 32-bit float
EXR_CODEC = 'ZIP'                  # lossless: ZIP/ZIPS/PIZ/PXR24/RLE/NONE; lossy: DWAA/DWAB/B44A
UV_EXT = '.exr' if UV_FORMAT == 'OPEN_EXR' else '.png'
# Illum (light) + shadow do not need PNG; JPEG keeps the files much smaller.
LIGHTSHADOW_FORMAT = 'JPEG'        # 'JPEG' or 'PNG'
JPEG_QUALITY = 90                  # 0..100 (JPEG only)
LIGHTSHADOW_EXT = '.jpg' if LIGHTSHADOW_FORMAT == 'JPEG' else '.png'

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

# Web wallpaper tool (the panel has a button that opens this URL in the browser).
WALLPAPER_TOOL_URL = "https://minecraft.novaskin.me/wallpapers/tools/blender/"
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


def _assign_part_labels(parts):
    """Return {object_name: unique_label} for a player's parts, disambiguating collisions
    (e.g. duplicate meshes) with a "_2", "_3"... suffix. Deterministic (sorted by name)."""
    used, out = {}, {}
    for o in sorted(parts, key=lambda x: x.name):
        lab = _mc_part_label(o.name)
        if lab in used:
            used[lab] += 1
            lab = f"{lab}_{used[lab]}"
        else:
            used[lab] = 1
        out[o.name] = lab
    return out


def _lin_to_srgb(x):
    """Encode a linear value/array to sRGB (display) in [0, 1]."""
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1.0 / 2.4) - 0.055)


def _light_for_label(light_maps, player_label, part_label):
    """Pick the per-pixel light map for a part: arm parts use their own variant's illum,
    everything else uses the first variant's. Returns None if no light is available."""
    if not light_maps:
        return None
    if "classic" in part_label:
        vname = "classic"
    elif "slim" in part_label:
        vname = "slim"
    else:
        vname = MASK_ARM_VARIANTS[0][0]
    return light_maps.get((player_label, vname))


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


def _force_player_parts_visible(players):
    """Clear MANUAL (non-driven) hide_render on the players' meshes so the masks/illum
    actually render them (some duplicate rigs leave whole parts -- body/legs -- with
    hide_render=True). Driver-hidden parts (fingers/3x3) are left alone. Must run BEFORE the
    _Session snapshot so the change sticks across restore_visibility(). Returns the meshes
    changed, so the caller can put the original hide_render back at the very end."""
    forced = []
    for p in players:
        for o in p["char_all"]:
            if o.hide_render and not _has_hide_render_driver(o):
                o.hide_render = False
                forced.append(o)
    if forced:
        bpy.context.view_layer.update()
        print(f"[VISIBLE] force-rendered {len(forced)} manually-hidden part(s): "
              f"{[o.name for o in forced]}")
    return forced


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

    # A MANUAL (non-driven) hide_render on a real part must NOT exclude it -- only the
    # driver/prop-based hiding (fingers/3x3) should. Some duplicate rigs leave body/legs
    # hide_render=True; temporarily clear that so they are selected.
    manual_hidden = [o for o in meshes if o.hide_render and not _has_hide_render_driver(o)]
    for o in manual_hidden:
        o.hide_render = False

    # force fingers/3x3 off (preserving the value type)
    saved = {}
    for bn, k in SELECTION_FORCE_OFF:
        pb = arm.pose.bones.get(bn)
        if pb is not None and k in pb.keys():
            saved[(bn, k)] = pb[k]
            pb[k] = type(pb[k])(0)

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
        for o in manual_hidden:
            o.hide_render = True
        _upd()
    return [o for o in meshes if o.name in visible]


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
        players.append({"label": arm.name, "rig_id": arm[RIG_ID_PROP], "arm": arm,
                        "char_all": char_all, "uv_parts": uv_parts})
    return players


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


def _save_image(arr_flat, W, H, path, colorspace='Non-Color',
                file_format='PNG', bit_depth=None, quality=None):
    """Save flat RGBA pixels (scene-linear float) to an image file. No view transform is
    applied; the colorspace controls encoding: 'Non-Color' = raw linear values (UV/Depth/
    shadow), 'sRGB' = sRGB curve on encode (illum -> display look).
    PNG honors bit_depth (default PNG_BIT_DEPTH; 8 or 16). JPEG is always 8-bit and drops
    the alpha channel; quality defaults to JPEG_QUALITY. OPEN_EXR is float (half if
    EXR_HALF) and preserves the exact values -- no premultiply/quantization."""
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
    elif file_format == 'JPEG':
        img.save(quality=JPEG_QUALITY if quality is None else quality)
    else:
        img.save()
    bpy.data.images.remove(img)


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

    def mute_drivers(self, mute):
        for d in self._drivers:
            d.mute = mute

    def restore_visibility(self):
        """Restore the original visibility (drivers active)."""
        self.mute_drivers(False)
        for o in self.s.objects:
            if o.name in self.hide:
                o.hide_render = self.hide[o.name]
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
    normalization -- acceptable."""
    s = sess.s
    parts = list(player["uv_parts"])
    if not parts:
        return None
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
    if uv is None or dep is None:
        return None
    geo = dep[uv[:, 2] > 0.5, 0]   # reliable coverage (opaque: front face = surface)
    if geo.size == 0:
        return None
    vmin, vmax = float(geo.min()), float(geo.max())
    if vmax - vmin < 1e-6:
        vmax = vmin + 1e-6
    return (vmin, vmax)


def export_part_uv(part, sess, out_subdir, tag="_UV", depth_range=None, label=None,
                   light_map=None):
    """Render one part's UV and save it. The file is RGBA = (R=U, G=V, B=depth, A=coverage);
    if light_map (N,3 sRGB) is given AND UV_FORMAT is EXR, the light is embedded as an extra
    'light' layer. Returns (path, out(N,4), cov, light(N,3) or None, W, H) for the base-layer
    composite -- or None if the part is empty."""
    stem = label if label is not None else part.name
    s = sess.s
    sess.mute_drivers(True)
    for o in s.objects:
        o.hide_render = True
    part.hide_render = False
    sess.vl.use_pass_uv = True
    print(f"[UV] {out_subdir} / {stem}{tag}  ({part.name})")
    uv, W, H = sess.render_pass('UV', 'CYCLES', UV_SAMPLES, 100)
    if uv is None:
        return None
    out = uv.copy()                       # R=U, G=V, B=1.0 (coverage), A=1
    cov = uv[:, 2] > 0.5                   # coverage: UV pass B = 1 on geometry
    if UV_DEPTH_IN_BLUE and depth_range is not None:
        sess.vl.use_pass_z = True
        dep, _, _ = sess.render_pass('Depth', 'CYCLES', UV_SAMPLES, 100)
        if dep is not None:
            zmin, zmax = depth_range
            z = dep[:, 0]
            b = np.zeros(z.shape, dtype='float32')
            b[cov] = np.clip((z[cov] - zmin) / (zmax - zmin), 0.0, 1.0)
            out[:, 2] = b                 # B = normalized depth (0=near, 1=far)
    out[:, 3] = cov.astype('float32')        # A = coverage (1 inside the part, 0 outside)
    # RGB light to embed (EXR only): valid (N,3) light map, masked to covered pixels.
    light = None
    if (UV_FORMAT == 'OPEN_EXR' and light_map is not None
            and getattr(light_map, "ndim", 0) == 2 and light_map.shape == (out.shape[0], 3)):
        light = np.zeros((out.shape[0], 3), dtype='float32')
        light[cov] = np.clip(light_map[cov], 0.0, 1.0)
    path = os.path.join(_abs(OUT_DIR), out_subdir, stem + tag + UV_EXT)
    if light is not None:
        _save_exr_uvdl(out, light, W, H, path)
    else:
        _save_image(out.reshape(-1), W, H, path, file_format=UV_FORMAT)
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
            return None
        m = (arr[:, 0] >= 0.5).astype('float32')
        out = np.zeros((m.size, 4), dtype='float32')
        out[:, 0] = out[:, 1] = out[:, 2] = m
        out[:, 3] = 1.0
        path = os.path.join(_abs(OUT_DIR), player["label"],
                            f"mask_{vname}.png")
        _save_image(out.reshape(-1), W, H, path)
        return path
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
        if LIGHTSHADOW_FORMAT == 'JPEG':
            s.render.image_settings.quality = JPEG_QUALITY
        path = os.path.join(_abs(OUT_DIR), "illum_" + vname + LIGHTSHADOW_EXT)
        s.render.filepath = path[:-4]
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


def _render_background_no_players(players, sess):
    """Render the FULL scene with the players HIDDEN (real scenery materials).
    Saves background_no_players.png. (Without the players, their shadows are gone.)"""
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
        s.render.engine = 'CYCLES'
        if hasattr(s, 'cycles'):
            s.cycles.samples = ILLUM_SAMPLES
            s.cycles.use_denoising = True
        s.render.resolution_percentage = ILLUM_RES_PCT
        s.render.film_transparent = False
        s.render.use_compositing = False
        if sess.out_node:
            sess.out_node.mute = True
        s.render.image_settings.file_format = 'PNG'
        s.render.image_settings.color_depth = str(PNG_BIT_DEPTH)   # '8' or '16'
        path = os.path.join(_abs(OUT_DIR), "background_no_players.png")
        s.render.filepath = path[:-4]
        bpy.context.view_layer.update()
        print("[BG] full scene without players")
        bpy.ops.render.render(write_still=True)
        return path
    finally:
        s.render.filepath = saved_fp
        s.render.image_settings.file_format = saved_fmt
        s.render.image_settings.color_depth = saved_depth
        sess.restore_visibility()   # unmute drivers + original visibility


def _render_combined_array(sess, res_pct):
    """Render the Combined pass (beauty, with denoise) -> linear RGBA array, read via the
    Viewer."""
    s = sess.s
    ng = sess.ng
    vin = sess.viewer.inputs[0]
    for l in list(vin.links):
        ng.links.remove(l)
    img_sock = sess.rl.outputs.get('Image')
    ng.links.new(img_sock, vin)
    if sess.out_node:
        sess.out_node.mute = True
    s.render.engine = 'CYCLES'
    if hasattr(s, 'cycles'):
        s.cycles.samples = ILLUM_SAMPLES
        s.cycles.use_denoising = True
    s.render.resolution_percentage = res_pct
    s.render.film_transparent = False
    s.render.use_compositing = True
    bpy.context.view_layer.update()
    bpy.ops.render.render(write_still=False)
    vimg = bpy.data.images.get('Viewer Node')
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

    # 1) CLEAN scene (all players hidden) -- basis for the shadow ratio
    sess.restore_visibility()
    sess.mute_drivers(True)
    for o in s.objects:
        if o.name in char_names:
            o.hide_render = True
    bpy.context.view_layer.update()
    clean, W, H = _render_combined_array(sess, ILLUM_RES_PCT)
    yield prog("Illum/shadow: clean scene")

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
                bpy.context.view_layer.update()
                try:
                    print(f"[ILLUM+SHADOW] {p['label']} / {vname}")
                    comb, _, _ = _render_combined_array(sess, ILLUM_RES_PCT)
                finally:
                    for o in p["char_all"]:
                        if o.name in saved_vd:
                            o.visible_diffuse = saved_vd[o.name]
                    for d in muted:
                        d.mute = False
                    _restore_arm_style(arm, p, saved_arm)

                mpath = os.path.join(_abs(OUT_DIR), p["label"],
                                     f"mask_{vname}.png")
                mask = _load_gray_channel(mpath, W, H)
                body = (mask > 0.5) if mask is not None else np.zeros(W * H, dtype=bool)

                # illum = lit body (masked by the player's mask)
                illum = comb.copy()
                illum[~body] = 0.0
                illum[:, 3] = 1.0
                _save_image(illum.reshape(-1), W, H,
                            os.path.join(_abs(OUT_DIR), p["label"],
                                         f"illum_{vname}{LIGHTSHADOW_EXT}"),
                            colorspace=ILLUM_COLORSPACE, file_format=LIGHTSHADOW_FORMAT)

                # keep the sRGB RGB light map (embedded as a 'light' layer in EXR UVs)
                if light_maps is not None:
                    light_maps[(p["label"], vname)] = \
                        _lin_to_srgb(illum[:, :3]).astype('float32')   # (N, 3)

                # shadow = ratio (multiply); 1.0 where the body is (it gets composited on top)
                ratio = np.clip(comb[:, :3] / np.clip(clean[:, :3], 1e-4, None), 0.0, 1.0)
                ratio[body] = 1.0
                shadow = np.empty((ratio.shape[0], 4), dtype='float32')
                shadow[:, :3] = ratio
                shadow[:, 3] = 1.0
                _save_image(shadow.reshape(-1), W, H,
                            os.path.join(_abs(OUT_DIR), p["label"],
                                         f"shadow_{vname}{LIGHTSHADOW_EXT}"),
                            file_format=LIGHTSHADOW_FORMAT)
                yield prog(f"Illum+shadow: {p['label']} / {vname}")
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


def _write_manifest(players, out_path):
    s = bpy.context.scene
    ordered = sorted((p for p in players if p.get("camera_depth") is not None),
                     key=lambda p: p["camera_depth"], reverse=True)  # back -> front
    manifest = {
        "render": {
            "resolution": [s.render.resolution_x, s.render.resolution_y],
            "engine": s.render.engine,
            "uv_samples": UV_SAMPLES,
            "mask_samples": MASK_SAMPLES,
            "mask_res_pct": MASK_RES_PCT,
            "uv_depth_in_blue": UV_DEPTH_IN_BLUE,
            "export_backface_uv": EXPORT_BACKFACE_UV,
            "uv_file_suffix": ("_UVDL" if (UV_FORMAT == 'OPEN_EXR'
                                           and EXPORT_PLAYER_ILLUM_SHADOW) else "_UV"),
            "uv_format": UV_FORMAT,
            "uv_ext": UV_EXT,
            "uv_channels": (
                ("R=U, G=V, B=depth(0=near,1=far) normalized per character"
                 if UV_DEPTH_IN_BLUE else "R=U, G=V, B=1.0")
                + ", A=coverage (1 in part, 0 outside)"
                + (" + light layer (light.R/G/B, sRGB)"
                   if (UV_FORMAT == 'OPEN_EXR' and EXPORT_PLAYER_ILLUM_SHADOW) else "")),
            "base_layer": ([COMPOSITE_OUTPUT_NAME.format(variant=v) + UV_EXT
                            for v, _ in MASK_ARM_VARIANTS] if COMPOSITE_BASE_LAYER else None),
            "base_layer_parts": (COMPOSITE_BASE_LABELS if COMPOSITE_BASE_LAYER else None),
            "png_bit_depth": PNG_BIT_DEPTH,
            "lightshadow_format": LIGHTSHADOW_FORMAT,
            "illum_backgrounds": ([f"illum_{v}{LIGHTSHADOW_EXT}" for v, _ in MASK_ARM_VARIANTS]
                                  if EXPORT_ILLUM_BACKGROUND else None),
            "background_no_players": ("background_no_players.png"
                                      if EXPORT_BACKGROUND_NO_PLAYERS else None),
            "player_illum_shadow": (
                {"illum": [f"illum_{v}{LIGHTSHADOW_EXT}" for v, _ in MASK_ARM_VARIANTS],
                 "shadow": [f"shadow_{v}{LIGHTSHADOW_EXT}" for v, _ in MASK_ARM_VARIANTS],
                 "note": "per player (in the subfolder); shadow is multiply (1=no shadow)"}
                if EXPORT_PLAYER_ILLUM_SHADOW else None),
        },
        "players": [
            {
                "label": p["label"],
                "rig_id": p.get("rig_id"),
                "folder": p["label"],
                "camera_depth": round(p["camera_depth"], 4) if p.get("camera_depth") is not None else None,
                "depth_range_viewer": ([round(x, 5) for x in p["depth_range"]]
                                       if p.get("depth_range") else None),
                "uv_parts": {lab: obj for obj, lab in
                             sorted((p.get("uv_labels") or {o.name: o.name for o in p["uv_parts"]}).items(),
                                    key=lambda kv: kv[1])},
                "base_layer": ([COMPOSITE_OUTPUT_NAME.format(variant=v) + UV_EXT
                                for v, _ in MASK_ARM_VARIANTS] if COMPOSITE_BASE_LAYER else None),
                "masks": [v for v, _ in MASK_ARM_VARIANTS],
            }
            for p in players
        ],
        "draw_order_back_to_front": [p["label"] for p in ordered],
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
    # Make manually render-hidden parts visible BEFORE the _Session snapshot, so masks/illum
    # render them; their original hide_render is put back in the finally below.
    forced_visible = _force_player_parts_visible(players)
    for p in players:
        os.makedirs(os.path.join(_abs(OUT_DIR), p["label"]), exist_ok=True)

    sess = _Session()
    results = {p["label"]: {"uv": {}, "masks": {}} for p in players}

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
             + ((1 + len(players) * n_variants) if EXPORT_PLAYER_ILLUM_SHADOW
                else (n_variants if EXPORT_ILLUM_BACKGROUND else 0))  # illum/shadow
             + n_parts                                             # front UVs
             + (len(players) * n_variants if COMPOSITE_BASE_LAYER else 0)  # base composites
             + (n_parts if EXPORT_BACKFACE_UV else 0)              # back UVs
             + (1 if EXPORT_BACKGROUND_NO_PLAYERS else 0)          # background
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
    embed_light = (UV_FORMAT == 'OPEN_EXR' and EXPORT_PLAYER_ILLUM_SHADOW)
    front_tag = "_UVDL" if embed_light else "_UV"

    try:
        # 0) Depth range per player (for depth in the B channel), in the Viewer's scale
        for p in players:
            p["depth_range"] = (_rendered_depth_range(p, sess, mask_mat, back_mat)
                                if UV_DEPTH_IN_BLUE else None)
            p["uv_labels"] = _assign_part_labels(p["uv_parts"])
            yield prog(f"Depth range: {p['label']}")
        # 1) Character MASKS (opaque override) -- generated BEFORE illum (the shadow/illum
        #    step reads them to mask the body).
        saved_mats = _swap_materials(all_char, mask_mat)
        try:
            for p in players:
                for vname, vval in MASK_ARM_VARIANTS:
                    results[p["label"]]["masks"][vname] = \
                        export_character_mask_variant(p, vname, vval, sess, players)
                    yield prog(f"Mask: {p['label']} / {vname}")
        finally:
            _restore_materials(saved_mats)
        # 2) ILLUM (light) + shadow FIRST, so the light can be packed into the UV alpha.
        if EXPORT_PLAYER_ILLUM_SHADOW:
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
                            if embed_light else None)
                    res = export_part_uv(part, sess, p["label"], tag=front_tag, label=lab,
                                         depth_range=p["depth_range"], light_map=lmap)
                    if res is None:
                        yield prog(f"UV front: {p['label']} / {lab} (empty)")
                        continue
                    path, out, cov, light, cW, cH = res
                    results[p["label"]]["uv"][part.name] = path
                    if COMPOSITE_BASE_LAYER:
                        for v in variant_names:
                            if lab not in base_sets[v]:
                                continue
                            c = comps.get(v)
                            if c is None:
                                c = [np.zeros((cW * cH, 4), dtype='float32'),
                                     np.full(cW * cH, np.inf, dtype='float32'),
                                     (np.zeros((cW * cH, 3), dtype='float32')
                                      if embed_light else None)]
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
                        cpath = os.path.join(_abs(OUT_DIR), p["label"],
                                             COMPOSITE_OUTPUT_NAME.format(variant=v) + UV_EXT)
                        if clight is not None:
                            _save_exr_uvdl(comp, clight, cW, cH, cpath)
                        else:
                            _save_image(comp.reshape(-1), cW, cH, cpath, file_format=UV_FORMAT)
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
        # 5) background of the scene without players (empty scene)
        if EXPORT_BACKGROUND_NO_PLAYERS:
            _render_background_no_players(players, sess)
            yield prog("Background (no players)")
        # 6) manifest with export details + average depth to order the rigs
        for p in players:
            p["camera_depth"] = _player_camera_depth(p)
        _write_manifest(players, os.path.join(_abs(OUT_DIR), "manifest.json"))
        yield prog("Manifest")
    finally:
        sess.restore()
        for o in forced_visible:   # put back the original manual hide_render
            o.hide_render = True
    return results


def render_all(op=None):
    """Synchronous full run (UI is blocked while it works). Returns the results dict, or
    None if the preflight aborts. The menu uses the modal operator instead (see invoke)."""
    _apply_settings(bpy.context.scene)
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
    print("Done. Players:",
          {k: {"uv": len(v["uv"]), "masks": list(v["masks"].keys())} for k, v in results.items()})
    if op is not None:
        op.report({'INFO'}, f"NovaSkin: exported {len(players)} player(s) -> {OUT_DIR}")
    return results


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
               ('OPEN_EXR', "EXR (float)", "Float EXR -- straight alpha, no quantization")],
        default='PNG')
    png_bit_depth: EnumProperty(
        name="PNG Depth", items=[('8', "8-bit", ""), ('16', "16-bit", "")], default='8')
    exr_half: BoolProperty(name="Half float", default=True,
                           description="16-bit half EXR (smaller); off = 32-bit float")
    exr_codec: EnumProperty(
        name="EXR Codec",
        items=[(c, c, "") for c in ('ZIP', 'ZIPS', 'PIZ', 'PXR24', 'RLE', 'NONE', 'DWAA', 'DWAB')],
        default='ZIP')
    export_backface_uv: BoolProperty(name="Back-face UVs", default=True)
    export_player_illum_shadow: BoolProperty(name="Illum + shadow", default=True)
    composite_base_layer: BoolProperty(name="base_layer composite", default=True)
    export_background_no_players: BoolProperty(name="Background (no players)", default=True)
    fix_2layer_position: BoolProperty(
        name="Fix hat position", default=True,
        description="Snap the hat (2_Layer_Extrusion) onto the head and scale it to the "
                    "Minecraft hat size (a bit bigger than the head)")
    illum_samples: IntProperty(name="Illum samples", default=48, min=1, max=4096)
    lightshadow_format: EnumProperty(
        name="Illum/Shadow", items=[('JPEG', "JPEG", ""), ('PNG', "PNG", "")], default='JPEG')
    jpeg_quality: IntProperty(name="JPEG quality", default=90, min=1, max=100)


def _apply_settings(scene):
    """Copy the panel's scene properties into the module globals (no-op if absent)."""
    st = getattr(scene, "novaskin", None)
    if st is None:
        return
    g = globals()
    g["OUT_DIR"] = st.out_dir
    g["UV_FORMAT"] = st.uv_format
    g["PNG_BIT_DEPTH"] = int(st.png_bit_depth)
    g["EXR_HALF"] = st.exr_half
    g["EXR_CODEC"] = st.exr_codec
    g["EXPORT_BACKFACE_UV"] = st.export_backface_uv
    g["EXPORT_PLAYER_ILLUM_SHADOW"] = st.export_player_illum_shadow
    g["COMPOSITE_BASE_LAYER"] = st.composite_base_layer
    g["EXPORT_BACKGROUND_NO_PLAYERS"] = st.export_background_no_players
    g["FIX_2LAYER_POSITION"] = st.fix_2layer_position
    g["ILLUM_SAMPLES"] = st.illum_samples
    g["LIGHTSHADOW_FORMAT"] = st.lightshadow_format
    g["JPEG_QUALITY"] = st.jpeg_quality
    g["UV_EXT"] = '.exr' if st.uv_format == 'OPEN_EXR' else '.png'
    g["LIGHTSHADOW_EXT"] = '.jpg' if st.lightshadow_format == 'JPEG' else '.png'


# ----------------------- Operator + menu + panel -----------------------
# Live progress for the panel (updated by the modal operator, read by the panel draw).
_PROGRESS = {"running": False, "frac": 0.0, "msg": ""}


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

    # Note on responsiveness: bpy is single-threaded and not thread-safe, so an actual
    # render call always blocks the main thread for its duration. The modal/timer below
    # runs the work in many small chunks and redraws + updates progress between them, and
    # lets the user cancel with Esc -- the UI is responsive between steps (each step still
    # blocks briefly while its render runs). True non-blocking rendering is not possible.

    def invoke(self, context, event):
        # Interactive launch (from the menu/panel): run as a modal job with a progress bar.
        _apply_settings(context.scene)
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
        _PROGRESS.update(running=True, frac=0.0, msg="starting...")
        context.workspace.status_text_set("NovaSkin: starting... (Esc to cancel)")
        _set_progress_header("NovaSkin: starting... (Esc to cancel)")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            return self._finish(context, cancelled=True)
        if event.type == 'TIMER':
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

    def _finish(self, context, cancelled):
        if getattr(self, "_timer", None) is not None:
            self._wm.event_timer_remove(self._timer)
            self._timer = None
        self._wm.progress_end()
        _PROGRESS.update(running=False, frac=0.0, msg="")
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
        return {'FINISHED'} if render_all(op=self) is not None else {'CANCELLED'}


def _menu_draw(self, context):
    self.layout.operator(RENDER_OT_novaskin.bl_idname, icon='RENDER_STILL')


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
            col.label(text="Esc (in the viewport) to cancel", icon='CANCEL')
        else:
            col = layout.column()
            col.scale_y = 1.5
            col.operator("render.novaskin", icon='RENDER_STILL')

        if WALLPAPER_TOOL_URL:
            layout.operator("wm.url_open", text="Open Wallpaper Tool",
                            icon='URL').url = WALLPAPER_TOOL_URL

        box = layout.box()
        box.label(text="Output", icon='FILE_FOLDER')
        box.prop(st, "out_dir")
        box.prop(st, "uv_format")
        if st.uv_format == 'OPEN_EXR':
            row = box.row(align=True)
            row.prop(st, "exr_half", toggle=True)
            row.prop(st, "exr_codec", text="")
            box.label(text="+ light layer (when illum on)", icon='LIGHT')
        else:
            box.prop(st, "png_bit_depth")

        box = layout.box()
        box.label(text="Layers", icon='RENDERLAYERS')
        box.prop(st, "export_backface_uv")
        box.prop(st, "export_player_illum_shadow")
        box.prop(st, "composite_base_layer")
        box.prop(st, "export_background_no_players")

        box = layout.box()
        box.label(text="Quality", icon='SETTINGS')
        box.prop(st, "illum_samples")
        row = box.row(align=True)
        row.prop(st, "lightshadow_format", text="")
        if st.lightshadow_format == 'JPEG':
            row.prop(st, "jpeg_quality", text="Q")

        box = layout.box()
        box.label(text="Rig", icon='ARMATURE_DATA')
        box.prop(st, "fix_2layer_position")


_classes = (NovaSkinSettings, RENDER_OT_novaskin, VIEW3D_PT_novaskin)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.novaskin = PointerProperty(type=NovaSkinSettings)
    bpy.types.TOPBAR_MT_render.append(_menu_draw)


def unregister():
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
