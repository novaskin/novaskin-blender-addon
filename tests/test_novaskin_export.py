"""Unit tests for the PURE (no live-Blender) helpers in novaskin_export.py.

Run with Blender's bundled Python (has numpy):  tests/run_tests.sh
or:  <blender-python> -m unittest discover -s tests
"""
import os
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from _loader import load_module

m = load_module()


class TestAnimWriteAnim(unittest.TestCase):
    """positions.bin / anim.bin = 'NSKA' v3 header + zlib(int16 xyz). Round-trip the encoder."""

    HEADER = "<4sIIIfffff"   # magic, ver, V, K, quant, keys_fps, zmin, zmax, zq

    def _read(self, path):
        with open(path, "rb") as f:
            raw = f.read()
        hsize = struct.calcsize(self.HEADER)
        magic, ver, V, K, quant, fps, zmin, zmax, zq = struct.unpack(self.HEADER, raw[:hsize])
        flat = np.frombuffer(zlib.decompress(raw[hsize:]), dtype="<i2")
        keys_q = flat.reshape(K, V, 3).astype(np.int64)
        return dict(magic=magic, ver=ver, V=V, K=K, quant=quant, fps=fps,
                    zmin=zmin, zmax=zmax, zq=zq), keys_q

    def test_header_and_single_key_roundtrip(self):
        quant, fps = m.ANIM_QUANT, 0.0
        key = np.array([[10.0, 20.0, -1.0],
                        [12.5, 7.25, -2.0],
                        [0.0, 0.0, -0.5]], dtype="float32")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "positions.bin")
            K, V = m._anim_write_anim(path, [key], quant, fps)
            self.assertEqual((K, V), (1, 3))
            hdr, keys_q = self._read(path)

        self.assertEqual(hdr["magic"], b"NSKA")
        self.assertEqual(hdr["ver"], 3)
        self.assertEqual((hdr["V"], hdr["K"]), (3, 1))
        self.assertAlmostEqual(hdr["quant"], float(quant), places=5)
        self.assertAlmostEqual(hdr["zmin"], float(key[:, 2].min()), places=5)
        self.assertAlmostEqual(hdr["zmax"], float(key[:, 2].max()), places=5)
        self.assertAlmostEqual(hdr["zq"], float((1 << m.ANIM_Z_BITS) - 1), places=3)

        # x/y are px*quant rounded; z is normalized to [zmin,zmax]*zq rounded
        exp_xy = np.round(key[:, :2] * quant).astype(np.int64)
        np.testing.assert_array_equal(keys_q[0, :, :2], exp_xy)
        zmin, zmax, zq = hdr["zmin"], hdr["zmax"], hdr["zq"]
        exp_z = np.round((key[:, 2] - zmin) / (zmax - zmin) * zq).astype(np.int64)
        np.testing.assert_array_equal(keys_q[0, :, 2], exp_z)

        # decode back to world-ish values within one quant/level
        dec_xy = keys_q[0, :, :2] / quant
        np.testing.assert_allclose(dec_xy, key[:, :2], atol=1.0 / quant)
        dec_z = zmin + keys_q[0, :, 2] / zq * (zmax - zmin)
        np.testing.assert_allclose(dec_z, key[:, 2], atol=(zmax - zmin) / zq + 1e-6)

    def test_degenerate_z_range_does_not_divide_by_zero(self):
        # all-equal z -> the writer widens zmax by 1e-6 instead of dividing by zero
        key = np.array([[1.0, 1.0, -3.0], [2.0, 2.0, -3.0]], dtype="float32")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.bin")
            m._anim_write_anim(path, [key], m.ANIM_QUANT, 0.0)
            hdr, keys_q = self._read(path)
        self.assertGreater(hdr["zmax"], hdr["zmin"])
        self.assertTrue(np.isfinite(keys_q).all())

    def test_multikey_delta_of_delta_roundtrip(self):
        # key0 absolute, key1 delta, key2+ delta-of-delta -- reconstruct and compare
        quant = m.ANIM_QUANT
        k0 = np.array([[0.0, 0.0, -1.0], [4.0, 0.0, -1.5]], dtype="float32")
        k1 = np.array([[1.0, 1.0, -1.1], [5.0, 1.0, -1.4]], dtype="float32")
        k2 = np.array([[2.0, 2.0, -1.2], [6.0, 2.5, -1.3]], dtype="float32")
        keys = [k0, k1, k2]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.bin")
            K, V = m._anim_write_anim(path, keys, quant, 24.0)
            self.assertEqual((K, V), (3, 2))
            hdr, dec = self._read(path)   # dec holds [abs, delta, delta-of-delta]

        # quantize each key the same way the writer does (shared zmin/zmax/zq)
        zmin, zmax, zq = hdr["zmin"], hdr["zmax"], hdr["zq"]
        exp_q = []
        for k in keys:
            q = np.empty((V, 3), np.int64)
            q[:, :2] = np.round(k[:, :2] * quant)
            q[:, 2] = np.round((k[:, 2] - zmin) / (zmax - zmin) * zq)
            exp_q.append(q)
        # reconstruct absolute keys from the abs/delta/delta-of-delta stream
        abs0, d0, dd0 = dec[0], dec[1], dec[2]
        rec0 = abs0
        rec1 = rec0 + d0
        rec2 = rec1 + (d0 + dd0)
        np.testing.assert_array_equal(rec0, exp_q[0])
        np.testing.assert_array_equal(rec1, exp_q[1])
        np.testing.assert_array_equal(rec2, exp_q[2])


class TestLinToSrgb(unittest.TestCase):
    def test_known_values_and_clamping(self):
        out = m._lin_to_srgb(np.array([0.0, 0.0031308, 0.5, 1.0, 2.0, -1.0]))
        self.assertAlmostEqual(out[0], 0.0, places=6)
        self.assertAlmostEqual(out[1], 0.0031308 * 12.92, places=6)        # linear toe
        self.assertAlmostEqual(out[2], 1.055 * 0.5 ** (1 / 2.4) - 0.055, places=6)
        self.assertAlmostEqual(out[3], 1.0, places=6)
        self.assertAlmostEqual(out[4], 1.0, places=6)                      # clamp high
        self.assertAlmostEqual(out[5], 0.0, places=6)                      # clamp low

    def test_monotonic(self):
        x = np.linspace(0, 1, 50)
        y = m._lin_to_srgb(x)
        self.assertTrue(np.all(np.diff(y) >= -1e-9))


class TestMcPartLabel(unittest.TestCase):
    def test_known_mappings(self):
        self.assertEqual(m._mc_part_label("Body_secondlayer"), "jacket")
        self.assertEqual(m._mc_part_label("2_Layer_Extrusion"), "hat")
        self.assertEqual(m._mc_part_label("R.leg_2ndLayer"), "pant_right")
        self.assertEqual(m._mc_part_label("L.Steve_arm"), "arm_left_classic")
        self.assertEqual(m._mc_part_label("R.alex_arm_2ndLayer"), "sleeve_right_slim")

    def test_strips_duplicate_suffix(self):
        self.assertEqual(m._mc_part_label("Body_secondlayer.001"), "jacket")
        self.assertEqual(m._mc_part_label("L.Steve_arm.042"), "arm_left_classic")

    def test_unmapped_falls_back_to_sanitized(self):
        self.assertEqual(m._mc_part_label("Some Random Mesh!"), "some_random_mesh")
        self.assertEqual(m._mc_part_label("boat.rig.003"), "boat_rig")


class _Obj:
    def __init__(self, name):
        self.name = name


class TestAssignPartLabels(unittest.TestCase):
    def test_no_collision(self):
        parts = [_Obj("NoFace_Head"), _Obj("Body"), _Obj("L.Steve_arm")]
        out = m._assign_part_labels(parts)
        self.assertEqual(out["NoFace_Head"], "head")
        self.assertEqual(out["Body"], "body")
        self.assertEqual(out["L.Steve_arm"], "arm_left_classic")

    def test_collision_disambiguated_deterministically(self):
        # the real sleeve_left_classic / _2 duplicate-mesh case
        parts = [_Obj("L.Steve_arm_2ndLayer.002"), _Obj("L.Steve_arm_2ndLayer")]
        out = m._assign_part_labels(parts)
        # sorted by name: ".002" sorts AFTER bare, so bare gets the base label
        self.assertEqual(out["L.Steve_arm_2ndLayer"], "sleeve_left_classic")
        self.assertEqual(out["L.Steve_arm_2ndLayer.002"], "sleeve_left_classic_2")

    def test_collision_is_stable_regardless_of_input_order(self):
        a = m._assign_part_labels([_Obj("L.Steve_arm_2ndLayer"), _Obj("L.Steve_arm_2ndLayer.002")])
        b = m._assign_part_labels([_Obj("L.Steve_arm_2ndLayer.002"), _Obj("L.Steve_arm_2ndLayer")])
        self.assertEqual(a, b)


class _Inp:
    def __init__(self, default=0.0, linked=False):
        self.default_value = default
        self.is_linked = linked


class _Node:
    def __init__(self, ntype, inputs=None, node_tree=None):
        self.type = ntype
        self.inputs = inputs if inputs is not None else {}
        self.node_tree = node_tree


class _NodeTree:
    def __init__(self, nodes):
        self.nodes = nodes


class _Mat:
    def __init__(self, nodes=None, use_nodes=True, diffuse_color=(0, 0, 0, 1)):
        self.use_nodes = use_nodes
        self.node_tree = _NodeTree(nodes or [])
        self.diffuse_color = diffuse_color


class _MSlot:
    def __init__(self, mat):
        self.material = mat


class _TObj:
    def __init__(self, mats, override=None):
        self.material_slots = [_MSlot(m) for m in mats]
        self._ov = override

    def get(self, key, default=None):
        return self._ov if (key == "nsk_transmissive" and self._ov is not None) else default


def _principled(transmission=0.0, alpha=1.0, tr_linked=False, al_linked=False):
    return _Node('BSDF_PRINCIPLED', inputs={
        'Transmission Weight': _Inp(transmission, tr_linked),
        'Alpha': _Inp(alpha, al_linked),
    })


class TestTransmissive(unittest.TestCase):
    """`_material_is_transmissive` / `_obj_is_transmissive`: decide tint-vs-cut by the actual material
    transmission (general), NOT by an object/material name (which only fit one scene)."""

    def test_glass_and_refraction_bsdf(self):
        self.assertTrue(m._material_is_transmissive(_Mat(nodes=[_Node('BSDF_GLASS')])))
        self.assertTrue(m._material_is_transmissive(_Mat(nodes=[_Node('BSDF_REFRACTION')])))

    def test_bare_transparent_bsdf_is_a_cutout_not_seethrough(self):
        # the Minecraft cutout idiom: a SOLID prop keyed by texture alpha (leaf, held spear/bucket,
        # particle) -- must stay opaque so it occludes/holds out, not be treated as water/glass
        self.assertFalse(m._material_is_transmissive(_Mat(nodes=[_Node('BSDF_TRANSPARENT')])))

    def test_principled_transmission_or_low_alpha(self):
        self.assertTrue(m._material_is_transmissive(_Mat(nodes=[_principled(transmission=1.0)])))
        self.assertTrue(m._material_is_transmissive(_Mat(nodes=[_principled(alpha=0.4)])))

    def test_opaque_principled_is_not(self):
        self.assertFalse(m._material_is_transmissive(_Mat(nodes=[_principled()])))

    def test_linked_alpha_cutout_stays_opaque(self):
        # a leaf cutout (Alpha driven by a texture) must NOT count as a see-through tint
        self.assertFalse(m._material_is_transmissive(_Mat(nodes=[_principled(al_linked=True)])))

    def test_non_node_material_uses_alpha(self):
        self.assertTrue(m._material_is_transmissive(_Mat(use_nodes=False, diffuse_color=(1, 1, 1, 0.3))))
        self.assertFalse(m._material_is_transmissive(_Mat(use_nodes=False, diffuse_color=(1, 1, 1, 1))))

    def test_transmissive_inside_node_group(self):
        grp = _Node('GROUP', node_tree=_NodeTree([_Node('BSDF_GLASS')]))
        self.assertTrue(m._material_is_transmissive(_Mat(nodes=[grp])))

    def test_override_property_wins_either_way(self):
        self.assertFalse(m._obj_is_transmissive(_TObj([_Mat(nodes=[_principled()])])))
        self.assertTrue(m._obj_is_transmissive(_TObj([_Mat(nodes=[_principled()])], override=True)))
        self.assertFalse(m._obj_is_transmissive(_TObj([_Mat(nodes=[_Node('BSDF_GLASS')])], override=False)))

    def test_obj_is_transmissive_if_ANY_material_seethrough(self):
        glass = _Mat(nodes=[_Node('BSDF_GLASS')])
        opaque = _Mat(nodes=[_principled()])
        self.assertTrue(m._obj_is_transmissive(_TObj([glass])))
        # a VOXEL TERRAIN shares one mesh between water blocks and dirt/grass -> any -> transmissive
        self.assertTrue(m._obj_is_transmissive(_TObj([opaque, glass, opaque])))
        self.assertFalse(m._obj_is_transmissive(_TObj([opaque, opaque])))
        self.assertFalse(m._obj_is_transmissive(_TObj([])))                 # no materials -> opaque


class TestCloseMask(unittest.TestCase):
    """Morphological close that fills the thin interior index-0 cracks in the occlusion mask."""

    def test_fills_thin_interior_cracks_keeps_silhouette(self):
        a = np.ones((20, 20), "float32")
        for i in range(3, 15):
            a[i, i] = 0.0                      # 1px diagonal crack
        a[5, 5:9] = 0.0                        # 1px horizontal crack
        out = m._close_mask(a.copy(), iterations=2)
        self.assertEqual(int((out[2:-2, 2:-2] < 0.5).sum()), 0)   # interior cracks gone
        self.assertEqual(float(out[0, 0]), 1.0)                   # silhouette preserved
        self.assertEqual(float(out[-1, -1]), 1.0)

    def test_does_not_fill_a_real_hole(self):
        a = np.ones((30, 30), "float32")
        a[10:20, 10:20] = 0.0                 # a big 10x10 hole -> must NOT be closed over
        out = m._close_mask(a.copy(), iterations=2)
        self.assertGreater(int((out < 0.5).sum()), 50)            # most of the hole survives


class TestBoxBlurRgb(unittest.TestCase):
    def test_smooths_a_spike_conserves_mean(self):
        a = np.zeros((9, 9, 3), "float32")
        a[4, 4] = 1.0                                  # a single bright spike
        out = m._box_blur_rgb(a, 1)
        self.assertLess(float(out[4, 4, 0]), 0.5)      # spike spread out
        self.assertGreater(float(out[3, 4, 0]), 0.0)   # bled into neighbours
        self.assertAlmostEqual(float(out.sum()), float(a.sum()), places=4)   # box blur conserves total

    def test_flat_field_unchanged(self):
        a = np.full((6, 6, 3), 0.4, "float32")
        out = m._box_blur_rgb(a, 3)
        self.assertTrue(np.allclose(out, 0.4, atol=1e-5))


class TestLayerSafeName(unittest.TestCase):
    def test_sanitizes(self):
        self.assertEqual(m._layer_safe_name("boat.rig"), "boat_rig")
        self.assertEqual(m._layer_safe_name("Leg Armor"), "Leg_Armor")
        self.assertEqual(m._layer_safe_name("__a..b__"), "a_b")

    def test_empty_falls_back(self):
        self.assertEqual(m._layer_safe_name(""), "layer")
        self.assertEqual(m._layer_safe_name("!!!"), "layer")


class TestAnimDilateLight(unittest.TestCase):
    def test_passes_zero_is_identity(self):
        rgb = np.random.RandomState(0).rand(9, 3).astype("float32")
        cov = np.zeros(9, bool)
        cov[4] = True
        out = m._anim_dilate_light(rgb, cov, 3, 3, passes=0)
        np.testing.assert_array_equal(out, rgb)

    def test_single_color_fills_whole_grid(self):
        # one covered pixel, one colour -> enough passes fill every pixel with that colour
        c = np.array([0.2, 0.4, 0.6], "float32")
        rgb = np.zeros((9, 3), "float32")
        cov = np.zeros(9, bool)
        rgb[4] = c
        cov[4] = True
        out = m._anim_dilate_light(rgb, cov, 3, 3, passes=5)
        for i in range(9):
            np.testing.assert_allclose(out[i], c, atol=1e-7, err_msg=f"pixel {i}")

    def test_covered_pixels_are_preserved(self):
        rgb = np.zeros((9, 3), "float32")
        cov = np.zeros(9, bool)
        rgb[0] = [1.0, 0.0, 0.0]
        rgb[8] = [0.0, 0.0, 1.0]
        cov[0] = cov[8] = True
        out = m._anim_dilate_light(rgb.copy(), cov, 3, 3, passes=3)
        np.testing.assert_array_equal(out[0], [1.0, 0.0, 0.0])
        np.testing.assert_array_equal(out[8], [0.0, 0.0, 1.0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
