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


if __name__ == "__main__":
    unittest.main(verbosity=2)
