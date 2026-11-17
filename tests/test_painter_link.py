"""Pure-Python tests for the Blender <-> Painter link (no Blender or Painter needed).

Covers ``texture_plugger/painter_link.py`` (Blender side) and the maths at the
top of ``texture_plugger/painter_plugin/texture_plugger_bridge.py`` (Painter
side), including the contract between them: the link folder, the file format
and the camera conventions.

Run:  python tests/test_painter_link.py      (or: python -m unittest discover tests)
"""

import importlib.util
import json
import math
import os
import random
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, os.pardir, "texture_plugger")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pl = _load("tp_painter_link", os.path.join(PKG, "painter_link.py"))
br = _load("tp_bridge", os.path.join(PKG, "painter_plugin", "texture_plugger_bridge.py"))


def close(a, b, tol=1e-6):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class Contract(unittest.TestCase):
    def test_same_folder_and_file_names(self):
        self.assertEqual(pl.LINK_DIR_NAME, br.LINK_DIR_NAME)
        self.assertEqual(pl.LINK_FILE, br.LINK_FILE)

    def test_plugin_finds_the_folder_blender_writes(self):
        # Blender installs the plugin to <Painter user>/python/startup/ and writes
        # the link to <Painter user>/texture_plugger_link/.
        root = os.path.join(tempfile.gettempdir(), "Adobe Substance 3D Painter")
        plugin = os.path.join(root, "python", "startup", "texture_plugger_bridge.py")
        old = os.environ.pop("TP_LINK_DIR", None)
        try:
            self.assertEqual(os.path.normcase(br.default_link_dir(plugin)),
                             os.path.normcase(os.path.join(root, pl.LINK_DIR_NAME)))
            os.environ["TP_LINK_DIR"] = "X:/somewhere"
            self.assertEqual(br.default_link_dir(plugin), "X:/somewhere")
        finally:
            os.environ.pop("TP_LINK_DIR", None)
            if old is not None:
                os.environ["TP_LINK_DIR"] = old

    def test_plugin_reads_what_blender_writes(self):
        d = tempfile.mkdtemp(prefix="tp_link_")
        try:
            view = pl.view_payload((0, -5, 1), (0, 1, 0), (0, 0, 1), 60, 40)
            path = pl.write_link(d, {"Crate": {"min": [-1, -1, -1], "max": [1, 1, 1],
                                               "objects": ["Crate"]}},
                                 active="Crate", environment="env_1.exr", exposure=0.5,
                                 view=view, blend_name="scene.blend")
            data = br.read_link(path)
            self.assertEqual(data["version"], pl.FORMAT_VERSION)
            self.assertEqual(data["sets"]["Crate"]["environment"], "env_1.exr")
            self.assertEqual(data["environment"], "env_1.exr")
            self.assertEqual(data["view"]["fov_v"], 40.0)
            self.assertFalse(os.path.exists(path + ".tmp"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_tone_mapping_matches_blender_view_transform(self):
        self.assertEqual(pl.tone_mapping_for("AgX"), "ACES")
        self.assertEqual(pl.tone_mapping_for("Filmic"), "ACES")
        self.assertEqual(pl.tone_mapping_for("Khronos PBR Neutral"), "ACES")
        self.assertEqual(pl.tone_mapping_for("Standard"), "Linear")
        self.assertEqual(pl.tone_mapping_for(None), "Linear")
        d = tempfile.mkdtemp(prefix="tp_link_")
        try:
            pl.write_link(d, {}, tone_mapping="ACES")
            data = pl.read_link(pl.write_link(d, {}))             # a later send keeps it
            self.assertEqual(data["tone_mapping"], "ACES")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_plugin_picks_render_or_preview_lighting(self):
        data = {"environment": "env_scene.exr",
                "preview": {"environment": "env_preview.exr", "hdri": "forest.exr"}}
        own = {"environment": "env_own.exr"}
        self.assertEqual(br.pick_environment(data, own, "render"), ("env_own.exr", "render"))
        self.assertEqual(br.pick_environment(data, None, "render"), ("env_scene.exr", "render"))
        self.assertEqual(br.pick_environment(data, own, "preview"), ("env_preview.exr", "preview"))
        no_preview = {"environment": "env_scene.exr"}
        self.assertEqual(br.pick_environment(no_preview, own, "preview"), ("env_own.exr", "render"))
        self.assertEqual(br.pick_environment(None, None, "preview"), (None, "render"))

    def test_plugin_finds_the_folder_blender_watches(self):
        d = tempfile.mkdtemp(prefix="tp_set_")
        try:
            tex = os.path.join(d, "Crate", "textures")
            os.makedirs(tex)
            os.makedirs(os.path.join(d, "Crate", "mesh"))
            spp = os.path.join(d, "Crate", "Crate.spp")
            fbx = os.path.join(d, "Crate", "mesh", "Crate.fbx")
            self.assertEqual(br.blender_textures_dir(spp, ""), tex)          # saved project
            self.assertEqual(br.blender_textures_dir("", fbx), tex)          # new from mesh
            self.assertEqual(br.blender_textures_dir("", "", tex), tex)      # --export-path
            self.assertEqual(br.blender_textures_dir("", "", d), "")         # not a textures folder
            self.assertEqual(br.blender_textures_dir(os.path.join(d, "x.spp"), ""), "")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_export_config_is_blender_template_as_png(self):
        cfg = br.blender_export_config("C:/t/textures", ["Crate", "Barrel"], "resource://x")
        self.assertEqual(cfg["exportPath"], "C:/t/textures")
        self.assertEqual(cfg["exportList"], [{"rootPath": "Crate"}, {"rootPath": "Barrel"}])
        self.assertEqual(cfg["exportParameters"][0]["parameters"]["fileFormat"], "png")
        self.assertEqual(cfg["exportParameters"][0]["parameters"]["paddingAlgorithm"], "infinite")
        self.assertEqual(br.BLENDER_PRESET, "Blender (Principled BSDF)")

    def test_broken_link_file_is_ignored(self):
        d = tempfile.mkdtemp(prefix="tp_link_")
        try:
            p = os.path.join(d, pl.LINK_FILE)
            with open(p, "w") as fh:
                fh.write("{half written")
            self.assertIsNone(br.read_link(p))
            with open(p, "w") as fh:
                json.dump([1, 2], fh)
            self.assertIsNone(br.read_link(p))
            self.assertIsNone(br.read_link(os.path.join(d, "missing.json")))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class LinkFile(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tp_link_")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def box(self, *objs):
        return {"min": [0, 0, 0], "max": [1, 1, 1], "objects": list(objs)}

    def test_view_only_send_keeps_lighting(self):
        pl.write_link(self.d, {"A": self.box("a")}, active="A", environment="env_1.exr")
        path = pl.write_link(self.d, {"A": self.box("a")}, active="A",
                             view=pl.view_payload((0, 0, 0), (0, 1, 0), (0, 0, 1), 50, 30))
        data = pl.read_link(path)
        self.assertEqual(data["environment"], "env_1.exr")
        self.assertEqual(data["sets"]["A"]["environment"], "env_1.exr")
        self.assertIn("view", data)

    def test_each_set_keeps_its_own_lighting(self):
        pl.write_link(self.d, {"A": self.box("a"), "B": self.box("b")}, active="A",
                      environment="env_a.exr")
        path = pl.write_link(self.d, {"A": self.box("a"), "B": self.box("b")}, active="B",
                             environment="env_b.exr")
        data = pl.read_link(path)
        self.assertEqual(data["sets"]["A"]["environment"], "env_a.exr")
        self.assertEqual(data["sets"]["B"]["environment"], "env_b.exr")
        self.assertEqual(data["environment"], "env_b.exr")
        self.assertEqual(pl.referenced_environments(data), {"env_a.exr", "env_b.exr"})

    def test_same_lighting_keeps_each_sets_own_map(self):
        two = {"A": self.box("a"), "B": self.box("b")}
        pl.write_link(self.d, two, active="A", environment="env_a.exr", lighting_key="k1")
        data = pl.read_link(pl.write_link(self.d, two, active="B", environment="env_b.exr",
                                          lighting_key="k1"))
        self.assertEqual(data["sets"]["A"]["environment"], "env_a.exr")
        self.assertEqual(data["sets"]["B"]["environment"], "env_b.exr")

    def test_changed_lighting_drops_old_maps(self):
        two = {"A": self.box("a"), "B": self.box("b")}
        pl.write_link(self.d, two, active="A", environment="env_a.exr", lighting_key="k1")
        path = pl.write_link(self.d, two, active="B", environment="env_b.exr", lighting_key="k2")
        data = pl.read_link(path)
        self.assertNotIn("environment", data["sets"]["A"])       # falls back to the new map
        self.assertEqual(data["sets"]["B"]["environment"], "env_b.exr")
        self.assertEqual(data["environment"], "env_b.exr")
        self.assertEqual(pl.referenced_environments(data), {"env_b.exr"})

    def test_view_only_send_keeps_lighting_key(self):
        pl.write_link(self.d, {"A": self.box("a")}, active="A", environment="env_a.exr",
                      lighting_key="k1")
        data = pl.read_link(pl.write_link(self.d, {"A": self.box("a")}, active="A"))
        self.assertEqual(data["sets"]["A"]["lighting_key"], "k1")
        self.assertEqual(data["sets"]["A"]["environment"], "env_a.exr")

    def test_preview_lighting_is_kept_and_referenced(self):
        prev = {"environment": "env_preview_1.exr", "exposure": 0.0, "hdri": "forest.exr"}
        pl.write_link(self.d, {"A": self.box("a")}, active="A", environment="env_a.exr",
                      preview=prev)
        data = pl.read_link(pl.write_link(self.d, {"A": self.box("a")}, active="A"))
        self.assertEqual(data["preview"]["environment"], "env_preview_1.exr")   # view-only keeps it
        self.assertEqual(pl.referenced_environments(data), {"env_a.exr", "env_preview_1.exr"})
        data = pl.read_link(pl.write_link(self.d, {"A": self.box("a")}, active="A", preview={}))
        self.assertNotIn("preview", data)

    def test_removed_sets_are_dropped(self):
        pl.write_link(self.d, {"A": self.box("a"), "B": self.box("b")})
        data = pl.read_link(pl.write_link(self.d, {"A": self.box("a")}))
        self.assertEqual(set(data["sets"]), {"A"})
        data = pl.read_link(pl.write_link(self.d, {"C": self.box("c")}, replace_sets=False))
        self.assertEqual(set(data["sets"]), {"A", "C"})

    def test_old_format_is_replaced(self):
        with open(pl.link_file(self.d), "w") as fh:
            json.dump({"version": 0, "sets": {"Old": {}}, "environment": "x.exr"}, fh)
        data = pl.read_link(pl.write_link(self.d, {"A": self.box("a")}))
        self.assertEqual(set(data["sets"]), {"A"})
        self.assertNotIn("environment", data)

    def test_prune_keeps_referenced_maps(self):
        for n in ("env_1.exr", "env_2.exr", "env_3.hdr", "other.exr", "env_4.txt"):
            open(os.path.join(self.d, n), "w").close()
        removed = pl.prune_environments(self.d, {"env_2.exr"})
        self.assertEqual(sorted(removed), ["env_1.exr", "env_3.hdr"])
        self.assertEqual(sorted(os.listdir(self.d)), ["env_2.exr", "env_4.txt", "other.exr"])


class Panorama(unittest.TestCase):
    def test_uv_round_trip(self):
        rng = random.Random(1)
        for _ in range(500):
            d = pl.normalize((rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1)))
            u, v = pl.dir_to_uv(d)
            self.assertTrue(0 <= u < 1 and 0 <= v <= 1)
            self.assertTrue(close(pl.uv_to_dir(u, v), d, 1e-9))

    def test_known_directions(self):
        # camera looks along +Y (centre), +X to the right, +Z at the top
        self.assertTrue(close(pl.dir_to_uv((0, 1, 0)), (0.5, 0.5)))
        self.assertTrue(close(pl.dir_to_uv((1, 0, 0)), (0.75, 0.5)))
        self.assertTrue(close(pl.dir_to_uv((-1, 0, 0)), (0.25, 0.5)))
        self.assertAlmostEqual(pl.dir_to_uv((0, 0, 1))[1], 1.0)

    def test_pixel_solid_angles_cover_the_sphere(self):
        w, h = 64, 32
        total = sum(pl.pixel_solid_angle(r, h, w) for r in range(h)) * w
        self.assertAlmostEqual(total, 4 * math.pi, places=9)

    def test_disc_matches_cone_solid_angle(self):
        w, h = 1024, 512
        for d in ((0, 1, 0), (1, 0.3, 0.2), (0, 0, 1), (0.1, -0.2, -0.97)):
            for deg in (5.0, 12.0):
                r = math.radians(deg)
                pix = pl.disc_pixels(w, h, d, r)
                omega = sum(p[2] for p in pix)
                cone = 2 * math.pi * (1 - math.cos(r))
                self.assertAlmostEqual(omega / cone, 1.0, delta=0.08,
                                       msg=f"dir {d} radius {deg}")
                self.assertEqual(len(pix), len({(p[0], p[1]) for p in pix}), "duplicate pixels")

    def test_disc_wraps_the_seam(self):
        w, h = 256, 128
        pix = pl.disc_pixels(w, h, (0, -1, 0), math.radians(8))   # u = 0 / 1 seam
        cols = {p[1] for p in pix}
        self.assertTrue(any(c < 5 for c in cols) and any(c > w - 6 for c in cols))

    def test_tiny_light_is_never_lost(self):
        pix = pl.disc_pixels(64, 32, (0.3, 0.4, 0.5), 1e-6)
        self.assertEqual(len(pix), 1)

    def test_disc_delivers_the_measured_irradiance(self):
        w, h = 512, 256
        d = pl.normalize((0.2, 0.9, 0.4))
        pix = pl.disc_pixels(w, h, d, math.radians(3))
        rad = pl.light_radiance((2.0, 1.0, 0.5), pix)
        delivered = [sum(rad[c] * p[2] for p in pix) for c in range(3)]
        self.assertTrue(close(delivered, (2.0, 1.0, 0.5), 1e-9))
        self.assertEqual(pl.light_radiance((1, 1, 1), []), (0.0, 0.0, 0.0))

    def test_angular_radius(self):
        mn = math.radians(2)
        self.assertAlmostEqual(pl.angular_radius("SUN", math.inf, 0, math.radians(10), mn),
                               math.radians(5))
        self.assertAlmostEqual(pl.angular_radius("POINT", 10.0, 2.0, 0, mn), math.atan(0.1))
        self.assertEqual(pl.angular_radius("POINT", 100.0, 0.0, 0, mn), mn)       # clamped up
        self.assertAlmostEqual(pl.angular_radius("AREA", 0.1, 50.0, 0, mn), math.radians(25))
        self.assertEqual(pl.angular_radius("POINT", 0.0, 1.0, 0, mn), mn)


class Axes(unittest.TestCase):
    def test_fbx_axes(self):
        # Blender +X right, +Y forward (away), +Z up -> Painter +X, -Z, +Y
        self.assertEqual(pl.to_painter((1, 2, 3)), (1.0, 3.0, -2.0))

    def test_aabb(self):
        lo, hi = pl.painter_aabb([(0, 0, 0), (1, 2, 3), (-1, -2, 0.5)])
        self.assertEqual(lo, [-1.0, 0.0, -2.0])
        self.assertEqual(hi, [1.0, 3.0, 2.0])
        self.assertEqual(pl.merge_aabb(None, (lo, hi)), (lo, hi))
        self.assertEqual(pl.merge_aabb(([0, 0, 0], [1, 1, 1]), ([-1, 2, 0], [0, 3, 0.5])),
                         ([-1, 0, 0], [1, 3, 1]))


class Camera(unittest.TestCase):
    def test_painter_rotation_measurements(self):
        # Read back from Painter 12.2: these pairs are the same orientation.
        for a, b in (([45, -90, 0], [0, -90, -45]), ([-30, 45, 0], [150, 135, 180])):
            fa, ua = br.basis_from_euler(a)
            fb, ub = br.basis_from_euler(b)
            self.assertTrue(close(fa, fb, 1e-9) and close(ua, ub, 1e-9), (a, b))

    def test_identity_looks_down_minus_z(self):
        f, u = br.basis_from_euler([0, 0, 0])
        self.assertTrue(close(f, (0, 0, -1)) and close(u, (0, 1, 0)))

    def test_euler_round_trip(self):
        rng = random.Random(7)
        for _ in range(500):
            rot = [rng.uniform(-180, 180), rng.uniform(-89, 89), rng.uniform(-180, 180)]
            f, u = br.basis_from_euler(rot)
            f2, u2 = br.basis_from_euler(br.euler_from_basis(f, u))
            self.assertTrue(close(f, f2, 1e-9) and close(u, u2, 1e-9), rot)

    def test_straight_up_and_down(self):
        for fwd in ((0, 1, 0), (0, -1, 0)):
            f, u = br.basis_from_euler(br.euler_from_basis(fwd, (0, 1, 0)))
            self.assertTrue(close(f, fwd, 1e-9))
            self.assertAlmostEqual(dot(f, u), 0.0, places=9)

    def test_blender_view_survives_the_trip(self):
        # A Blender viewport looking from the front-right, slightly down.
        fwd = pl.normalize((-1, 1, -0.4))
        right = pl.normalize((fwd[1], -fwd[0], 0))
        up = (right[1] * fwd[2] - right[2] * fwd[1], right[2] * fwd[0] - right[0] * fwd[2],
              right[0] * fwd[1] - right[1] * fwd[0])
        view = pl.view_payload((5, -5, 3), fwd, up, 60, 36)
        f, u = br.basis_from_euler(br.euler_from_basis(view["forward"], view["up"]))
        self.assertTrue(close(f, view["forward"], 1e-9))
        self.assertTrue(close(u, view["up"], 1e-9))
        self.assertGreater(u[1], 0.5)            # Blender up (+Z) is Painter up (+Y)

    def test_fov(self):
        self.assertAlmostEqual(pl.fov_from_lens(36, 72), 90.0)
        h, v = pl.split_fov(90.0, 2.0)
        self.assertEqual(h, 90.0)
        self.assertAlmostEqual(math.tan(math.radians(v) / 2), 0.5)
        h, v = pl.split_fov(90.0, 0.5)
        self.assertEqual(v, 90.0)
        self.assertAlmostEqual(math.tan(math.radians(h) / 2), 0.5)


class Framing(unittest.TestCase):
    def test_sphere_just_fits(self):
        for aspect in (0.5, 1.0, 1.8):
            pos, dist = br.frame_position([1, 2, 3], 2.0, [0, 0, -1], 40.0, aspect, margin=1.0)
            fv = math.radians(40)
            fh = 2 * math.atan(math.tan(fv / 2) * aspect)
            half = min(fv, fh) / 2
            self.assertAlmostEqual(2.0 / dist, math.sin(half))      # tangent to the frustum
            self.assertTrue(close(pos, [1, 2, 3 + dist]))

    def test_fit_maps_blender_metres_to_painter_centimetres(self):
        # Crate (1 m cube at origin) and Barrel (0.5 m cube at Blender x = 4)
        lo, hi = pl.painter_aabb([(-1, -1, -1), (1, 1, 1), (3.5, -0.5, -0.5), (4.5, 0.5, 0.5)])
        p_lo = [v * 100 for v in lo]
        p_hi = [v * 100 for v in hi]
        centre = [(a + b) / 2 for a, b in zip(p_lo, p_hi)]
        dims = [b - a for a, b in zip(p_lo, p_hi)]
        fit = br.fit_transform(lo, hi, centre, dims)
        self.assertAlmostEqual(fit[0], 100.0)
        barrel = br.apply_fit(fit, list(pl.to_painter((4, 0, 0))))
        self.assertTrue(close(barrel, [400, 0, 0], 1e-6))

    def test_fit_degenerate(self):
        fit = br.fit_transform([0, 0, 0], [0, 0, 0], [5, 5, 5], [0, 0, 0])
        self.assertEqual(fit[0], 100.0)

    def test_union_box_skips_unknown_sets(self):
        sets = {"A": {"min": [0, 0, 0], "max": [1, 1, 1]},
                "B": {"min": [-2, 0, 0], "max": [0, 3, 1]},
                "C": {"min": None, "max": None}, "D": "junk"}
        self.assertEqual(br.union_box(sets, ["A", "B", "C", "D", "Z"]), ([-2, 0, 0], [1, 3, 1]))
        self.assertEqual(br.union_box(sets, ["C", "Z"]), (None, None))

    def test_match_sets(self):
        link = {"Crate": {}, "Material.001": {}, "metal_A": {}, "Metal.A": {}, "Bad": "x"}
        got = br.match_sets(["Crate", "Material_001", "MetalA", "Bad", "Missing"], link)
        # exact, punctuation-only rename, ambiguous (two candidates) left out, junk ignored
        self.assertEqual(got, {"Crate": "Crate", "Material_001": "Material.001"})

    def test_box_center_radius(self):
        c, r = br.box_center_radius([0, 0, 0], [2, 2, 2])
        self.assertEqual(c, [1, 1, 1])
        self.assertAlmostEqual(r, math.sqrt(3))
        self.assertGreater(br.box_center_radius([1, 1, 1], [1, 1, 1])[1], 0)


class Lightmaps(unittest.TestCase):
    """Cycles UV lightmaps: Blender's file and the plugin's choice of map."""

    def test_names_and_merge(self):
        self.assertEqual(pl.lightmap_name("Mat.001 a/b", "render", 12.7),
                         "lightmap_render_Mat.001_a_b_12.exr")
        old = {"sets": {"A": {"render": "lightmap_render_A_1.exr", "preview": "lightmap_preview_A_1.exr"},
                        "B": {"render": "lightmap_render_B_1.exr"}}}
        new = pl.merge_lightmaps(old, {"A": {"render": "lightmap_render_A_2.exr", "preview": None}})
        self.assertEqual(new["sets"]["A"], {"render": "lightmap_render_A_2.exr"})   # old preview dropped
        self.assertEqual(new["sets"]["B"]["render"], "lightmap_render_B_1.exr")
        self.assertEqual(pl.referenced_lightmaps(new),
                         {"lightmap_render_A_2.exr", "lightmap_render_B_1.exr"})
        self.assertEqual(pl.LIGHTMAPS_FILE, br.LIGHTMAPS_FILE)
        self.assertEqual(pl.LIGHTMAP_PREFIX, br.LIGHTMAP_PREFIX)

    def test_write_prunes_unused(self):
        d = tempfile.mkdtemp(prefix="tp_lm_")
        try:
            for n in ("lightmap_render_A_1.exr", "lightmap_render_A_2.exr", "env_1.exr"):
                open(os.path.join(d, n), "wb").close()
            # while a bake runs: listed incrementally, nothing deleted
            pl.write_lightmaps(d, {"A": {"render": "lightmap_render_A_2.exr"}}, prune=False)
            self.assertEqual(len([n for n in os.listdir(d) if n.endswith(".exr")]), 3)
            pl.write_lightmaps(d, {"B": {"render": "lightmap_render_B_1.exr"}}, prune=False)
            self.assertEqual(set(pl.read_link(os.path.join(d, pl.LIGHTMAPS_FILE))["sets"]), {"A", "B"})
            pl.write_lightmaps(d, {})          # at the end: unused maps go
            self.assertEqual(sorted(os.listdir(d)),
                             [pl.LIGHTMAPS_FILE, "env_1.exr", "lightmap_render_A_2.exr"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_pick_lightmap(self):
        data = {"sets": {"Crate": {"render": "lightmap_render_Crate_1.exr",
                                   "preview": "lightmap_preview_Crate_1.exr"},
                         "Wall": {"render": "lightmap_render_Wall_1.exr"}}}
        self.assertEqual(br.pick_lightmap(data, "Crate", "preview"), "lightmap_preview_Crate_1.exr")
        self.assertEqual(br.pick_lightmap(data, "crate", "render"), "lightmap_render_Crate_1.exr")
        self.assertIsNone(br.pick_lightmap(data, "Wall", "preview"))   # filter + preview map instead
        self.assertIsNone(br.pick_lightmap(data, "Floor"))
        self.assertIsNone(br.pick_lightmap(None, "Crate"))
        self.assertTrue(br.is_lightmap("lightmap_render_Crate_1.exr"))
        entry = {"render": "lightmap_render_Crate_1.exr", "lighting_key": "k1", "time": 100.0}
        self.assertFalse(br.lightmap_outdated(entry, {"lighting_key": "k1", "stamp": 200.0}))
        self.assertTrue(br.lightmap_outdated(entry, {"lighting_key": "k2", "stamp": 200.0}))
        self.assertFalse(br.lightmap_outdated(entry, {"lighting_key": "k2", "stamp": 50.0}))  # bake is newer
        self.assertFalse(br.lightmap_outdated(entry, None))
        self.assertFalse(br.lightmap_outdated({"render": "x"}, {"lighting_key": "k2", "stamp": 200.0}))
        self.assertFalse(br.is_lightmap("env_123.exr"))


class BakedLighting(unittest.TestCase):
    """The Painter plugin's "Bake Lighting" bookkeeping and the marker for Blender."""

    def test_file_stem(self):
        for path, want in (("C:\\t\\Crate_BaseColor.png", "crate_basecolor"),
                           ("/t/Crate_BaseColor.1001.png", "crate_basecolor"),
                           ("Crate_BaseColor.<UDIM>.png", "crate_basecolor"),
                           ("Crate_BaseColor_sRGB.png", "crate_basecolor_srgb"),
                           ("Wall_2024_BaseColor.png", "wall_2024_basecolor"),
                           ("Material.001_BaseColor.png", "material.001_basecolor")):
            self.assertEqual(br.file_stem(path), want, path)

    def test_base_color_stem(self):
        files = ["/t/Crate_Normal.png", "/t/Crate_BaseColor.png", "/t/Crate_Roughness.png"]
        self.assertEqual(br.base_color_stem("Crate", files), "crate_basecolor")
        self.assertEqual(br.base_color_stem("Crate", ["/t/Crate_BaseColor.1001.png"]), "crate_basecolor")
        self.assertEqual(br.base_color_stem("Crate", []), "crate_basecolor")    # usual name

    def test_merge_marker_keeps_other_sets(self):
        old = {"version": 1, "sets": {"A": {"lit": True, "base_color": "a_basecolor"},
                                      "B": {"lit": True, "base_color": "b_basecolor"}}}
        new = br.merge_lit_marker(old, {"B": {"lit": False, "base_color": "b_basecolor"},
                                        "C": {"lit": True, "base_color": "c_basecolor"}})
        self.assertEqual(new["sets"]["A"]["lit"], True)        # not exported: unchanged
        self.assertEqual(new["sets"]["B"]["lit"], False)       # exported without lighting
        self.assertEqual(new["sets"]["C"]["lit"], True)
        self.assertEqual(br.merge_lit_marker(None, {})["sets"], {})
        self.assertEqual(br.merge_lit_marker({"sets": "junk"}, {})["sets"], {})

    def test_lit_updates(self):
        rec = {"A": "env_1.exr", "B": "env_2.exr", "C": "env_3.exr"}
        wanted = {"A": "env_1.exr", "B": "env_9.exr", "C": None}
        self.assertEqual(br.lit_updates(rec, wanted), ["B"])
        self.assertEqual(br.lit_updates({}, wanted), [])

    def test_marker_round_trip(self):
        d = tempfile.mkdtemp(prefix="tp_lit_")
        try:
            path = os.path.join(d, br.LIT_MARKER)
            br.write_json(path, br.merge_lit_marker(None, {"Crate": {
                "lit": True, "base_color": br.base_color_stem("Crate", ["Crate_BaseColor.png"])}}))
            data = br.read_link(path)
            self.assertEqual(data["sets"]["Crate"], {"lit": True, "base_color": "crate_basecolor"})
            self.assertFalse(os.path.exists(path + ".tmp"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
