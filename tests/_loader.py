"""Import render_uv_mask.py OUTSIDE Blender by installing a minimal fake `bpy`.

The add-on does `import bpy` and `from bpy.props import (...)` at module top, and defines
operator/panel/property-group classes whose bases are `bpy.types.*` and whose bodies call
`bpy.props.*Property(...)`. None of that touches a live Blender at *import* time -- only when the
functions/operators actually run. So a stub `bpy` that provides reusable base classes and
no-op property factories is enough to import the module and exercise its PURE helpers
(binary-format writers, colour math, label mapping, image dilation).

Run the tests with Blender's bundled Python (it has numpy, matching the runtime):
    <Blender.app>/Contents/Resources/<ver>/python/bin/python3.x -m unittest discover -s tests
or just `tests/run_tests.sh`, which locates it.
"""
import importlib.util
import os
import sys
import types

_PROP_NAMES = (
    "BoolProperty", "IntProperty", "FloatProperty", "EnumProperty", "StringProperty",
    "PointerProperty", "CollectionProperty", "FloatVectorProperty", "IntVectorProperty",
    "BoolVectorProperty", "RemoveProperty",
)


class _FakeTypes(types.ModuleType):
    """`bpy.types`: any attribute access returns a reusable empty base class, so
    `class X(bpy.types.Operator)` (or Panel/PropertyGroup/Menu/...) works for any name."""

    def __init__(self):
        super().__init__("bpy.types")
        self._cache = {}

    def __getattr__(self, name):
        cls = self._cache.get(name)
        if cls is None:
            cls = type(name, (), {})
            self._cache[name] = cls
        return cls


def _install_fake_bpy():
    if "bpy" in sys.modules and getattr(sys.modules["bpy"], "_novaskin_fake", False):
        return
    bpy = types.ModuleType("bpy")
    bpy._novaskin_fake = True
    bpy.types = _FakeTypes()

    props = types.ModuleType("bpy.props")

    def _factory(_name):
        def _prop(*args, **kwargs):           # Blender returns a property descriptor; tests don't care
            return (_name, args, kwargs)
        return _prop

    for n in _PROP_NAMES:
        setattr(props, n, _factory(n))
    bpy.props = props

    # Surfaces only used at register()/run time -- present but inert so an accidental touch
    # fails loudly rather than importing a real Blender.
    bpy.context = types.SimpleNamespace()
    bpy.data = types.SimpleNamespace()
    bpy.ops = types.SimpleNamespace()
    bpy.app = types.SimpleNamespace(version=(5, 1, 0))
    bpy.utils = types.SimpleNamespace(
        register_class=lambda *a, **k: None,
        unregister_class=lambda *a, **k: None,
    )
    bpy.path = types.SimpleNamespace(abspath=lambda p: p)

    sys.modules["bpy"] = bpy
    sys.modules["bpy.props"] = props
    sys.modules["bpy.types"] = bpy.types
    # imported inside a few function bodies (never at module import), stub to be safe
    sys.modules.setdefault("bmesh", types.ModuleType("bmesh"))
    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = lambda *a, **k: None
    mathutils.Matrix = lambda *a, **k: None
    sys.modules.setdefault("mathutils", mathutils)


def load_module():
    """Import (once) and return the render_uv_mask module with the fake bpy installed."""
    if "render_uv_mask" in sys.modules:
        return sys.modules["render_uv_mask"]
    _install_fake_bpy()
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", "render_uv_mask.py"))
    spec = importlib.util.spec_from_file_location("render_uv_mask", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["render_uv_mask"] = mod
    spec.loader.exec_module(mod)
    return mod
