"""In-Blender tests for the geometry/UV helpers that need a real `bpy` (and a real mesh).

These run inside Blender (headless), NOT against the user's live GUI session:
    blender --background --python tests/blender/test_geometry_blender.py
or via  tests/run_blender_tests.sh  (which locates the Blender binary).

They build a tiny throwaway mesh in a fresh datablock and exercise _expand_pixel_uvs /
_mesh_uv_handedness directly -- so the pixel-UV cell snapping and the degenerate-face (collapsed
axis) reconstruction (the part that previously could only be eyeballed in the browser) are checked
exactly. Exit code is non-zero on any failure, so a CI / runner detects it.
"""
import os
import sys

import bpy
import bmesh
import numpy as np

# import the add-on module by path (real bpy; no registration needed to call helpers)
_REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, _REPO)
import novaskin_export as R   # noqa: E402

_FAILURES = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (("  -- " + extra) if (extra and not cond) else ""))
    if not cond:
        _FAILURES.append(name)


def _make_object(verts, faces, uvs_per_face, name="pixtest"):
    """Build a mesh object. `uvs_per_face[f]` = list of (u, v) for that face's corners, in the same
    order as `faces[f]`. Returns the object (linked to the scene)."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bverts = [bm.verts.new(v) for v in verts]
    bm.verts.ensure_lookup_table()
    uvl = bm.loops.layers.uv.new("UVMap")
    for fi, f in enumerate(faces):
        face = bm.faces.new([bverts[i] for i in f])
        for corner, loop in enumerate(face.loops):
            loop[uvl].uv = uvs_per_face[fi][corner]
    bm.to_mesh(me)
    bm.free()
    obj = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _uv_by_vertex(obj, poly_index):
    """{vertex_index: (u, v)} for one polygon's loops (rotation-robust)."""
    me = obj.data
    poly = me.polygons[poly_index]
    out = {}
    for li in poly.loop_indices:
        vi = me.loops[li].vertex_index
        uv = me.uv_layers.active.data[li].uv
        out[vi] = (round(uv[0], 6), round(uv[1], 6))
    return out


def test_expand_pixel_uvs():
    """skin_px=4 -> cell=0.25. Face A: an INSET quad in cell (col0,row0) -> grows to the full cell,
    and serves as the non-degenerate reference for the UV handedness. Face B: a V-COLLAPSED quad
    (all loops share V) in cell (col2,row1) whose 3D geometry spans +Y -> the collapsed V axis is
    rebuilt from geometry so the +Y side lands on the cell TOP (upright, not mirrored)."""
    # Face A: square in XY at y in [0,1]; UVs inset inside cell0 [0,0.25]
    A_verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    A_uv = [(0.06, 0.06), (0.19, 0.06), (0.19, 0.19), (0.06, 0.19)]
    # Face B: square in XY at y in [5,6] (so +Y is unambiguous); V collapsed at 0.30, U in [0.55,0.70]
    B_verts = [(0, 5, 0), (1, 5, 0), (1, 6, 0), (0, 6, 0)]
    B_uv = [(0.55, 0.30), (0.70, 0.30), (0.70, 0.30), (0.55, 0.30)]
    obj = _make_object(A_verts + B_verts,
                       [(0, 1, 2, 3), (4, 5, 6, 7)],
                       [A_uv, B_uv])

    # handedness from the non-degenerate face A: +X=+U, +Y=+V, CCW normal +Z -> +1
    vco = np.array(A_verts + B_verts, "f")
    check("uv_handedness is +1 for the aligned reference quad",
          R._mesh_uv_handedness(obj.data, vco) == 1.0)

    restore = R._expand_pixel_uvs([obj], skin_px=4)
    try:
        # Face A: inset -> snapped to the full cell0 corners, by each loop's side
        a = _uv_by_vertex(obj, 0)
        exp_a = {0: (0.0, 0.0), 1: (0.25, 0.0), 2: (0.25, 0.25), 3: (0.0, 0.25)}
        check("inset face grows to the full cell", a == exp_a, f"{a} != {exp_a}")

        # Face B: U fine (snaps by side), V rebuilt upright -> +Y verts (6,7) on the cell TOP (v1=0.5)
        b = _uv_by_vertex(obj, 1)
        exp_b = {4: (0.5, 0.25), 5: (0.75, 0.25), 6: (0.75, 0.5), 7: (0.5, 0.5)}
        check("V-collapsed face is rebuilt UPRIGHT (not flipped)", b == exp_b, f"{b} != {exp_b}")
        # the discriminating assertion: the +Y geometry side is the HIGHER V, not the lower
        check("degenerate face top follows +Y geometry",
              b[6][1] > b[4][1] and b[7][1] > b[5][1], f"{b}")
    finally:
        restore()

    # restore() puts the original (inset/collapsed) UVs back
    a_restored = _uv_by_vertex(obj, 0)
    check("restore() reverts the UVs",
          abs(a_restored[0][0] - 0.06) < 1e-5 and abs(a_restored[0][1] - 0.06) < 1e-5,
          f"{a_restored}")

    bpy.data.objects.remove(obj, do_unlink=True)


def test_handedness_mirrored():
    """A quad whose UV V axis runs OPPOSITE to +Y geometry must report the flipped handedness sign,
    so the degenerate reconstruction would mirror with it (the sign actually does its job)."""
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    # U=+X as before, but V DECREASES with +Y -> mirrored frame -> sign -1
    uv = [(0.06, 0.19), (0.19, 0.19), (0.19, 0.06), (0.06, 0.06)]
    obj = _make_object(verts, [(0, 1, 2, 3)], [uv], name="mirror")
    vco = np.array(verts, "f")
    check("uv_handedness is -1 for a V-mirrored quad",
          R._mesh_uv_handedness(obj.data, vco) == -1.0)
    bpy.data.objects.remove(obj, do_unlink=True)


def main():
    test_expand_pixel_uvs()
    test_handedness_mirrored()
    print("\n%d failure(s)" % len(_FAILURES))
    # Blender ignores sys.exit code in --background for --python; write a marker the runner reads.
    status = os.environ.get("NSK_TEST_STATUS")
    if status:
        with open(status, "w") as f:
            f.write("FAIL " + ",".join(_FAILURES) if _FAILURES else "PASS")
    sys.exit(1 if _FAILURES else 0)


if __name__ == "__main__":
    main()
