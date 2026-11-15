"""Pure-Python tests for texture_plugger/scene_plan.py and the placeholder PNG writer.

Run:  python tests/test_scene_plan.py      (or: python -m unittest discover tests)
"""

import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, os.pardir, "texture_plugger")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sp = _load("tp_scene_plan", os.path.join(PKG, "scene_plan.py"))
sc = _load("tp_scaffold_plan", os.path.join(PKG, "scaffold.py"))
GB = 1024 ** 3


class Resolution(unittest.TestCase):
    def test_small_scene_keeps_the_size_asked_for(self):
        self.assertEqual(sp.choose_resolution(3, 4, 2048, 12 * GB), 2048)

    def test_maggies_cubicles_fit_a_31_gb_pc(self):
        # 1033 texture sets, 4 maps, 2K asked, 40 % of 31 GB
        budget = int(31 * GB * 0.4)
        res = sp.choose_resolution(1033, 4, 2048, budget)
        self.assertLess(1033 * sp.texture_bytes(res, 4), budget)
        self.assertEqual(res, 512)

    def test_never_below_the_minimum_and_power_of_two(self):
        self.assertEqual(sp.choose_resolution(100000, 7, 4096, 1), sp.MIN_RESOLUTION)
        self.assertEqual(sp.choose_resolution(1, 4, 3000, 64 * GB), 2048)
        self.assertEqual(sp.choose_resolution(1, 4, 128, 1), 128)    # asked below the minimum

    def test_memory_estimate(self):
        # 2K RGBA8 in RAM (16 MiB) + on the GPU with mipmaps (~21.3 MiB) per map
        self.assertAlmostEqual(sp.texture_bytes(2048, 1) / 1024 ** 2, 16 * (1 + 4 / 3), places=3)
        self.assertEqual(sp.texture_bytes(2048, 0), 0)

    def test_system_memory_is_known(self):
        self.assertGreater(sp.system_memory_bytes(), 512 * 1024 ** 2)

    def test_placeholders_stay_small(self):
        self.assertEqual(sp.placeholder_size(2048), sp.PLACEHOLDER_MAX)
        self.assertEqual(sp.placeholder_size(16), 16)


class Plan(unittest.TestCase):
    RAM, GPU = int(31.1 * GB), int(11.9 * GB)            # Maggie's PC: 31 GB, RTX 5070 12 GB

    def test_cubicles_open_as_one_painter_project(self):
        weights = [1] * 897 + [2] * 68                    # 965 objects, 1033 texture sets
        size, parts = sp.plan_scene(weights, 4, 1024, self.RAM, self.GPU)
        self.assertEqual(len(parts), 1)
        self.assertEqual(len(parts[0]), 965)
        self.assertEqual(size, 256)
        # and it really fits: Blender's textures in RAM and on the card, Painter's project
        self.assertTrue(sp.blender_fits(1033, 4, size, self.RAM * 0.4, self.GPU * sp.BLENDER_GPU_SHARE))
        self.assertGreaterEqual(sp.painter_capacity(size, self.RAM, self.GPU), 1033)

    def test_measured_painter_whole_scene_would_not_fit_at_512(self):
        # 964 sets at 512 px with one layer used ~5.4 GB of the card's 12 GB in Painter alone
        self.assertLess(sp.painter_capacity(512, self.RAM, self.GPU), 1033)

    def test_small_scene_keeps_the_size_and_one_part(self):
        size, parts = sp.plan_scene([1, 1, 2], 4, 2048, self.RAM, self.GPU)
        self.assertEqual((size, len(parts)), (2048, 1))

    def test_too_big_even_at_the_minimum_is_split(self):
        weights = [1] * 20000
        size, parts = sp.plan_scene(weights, 4, 2048, self.RAM, self.GPU)
        self.assertEqual(size, sp.MIN_RESOLUTION)
        cap = sp.painter_capacity(size, self.RAM, self.GPU)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= cap for p in parts))

    def test_a_small_graphics_card_lowers_the_size(self):
        size_big, _ = sp.plan_scene([1] * 60, 4, 2048, self.RAM, 24 * GB)
        size_small, _ = sp.plan_scene([1] * 60, 4, 2048, self.RAM, 2 * GB)
        self.assertLess(size_small, size_big)

    def test_preference_limit_splits_parts(self):
        _size, parts = sp.plan_scene([1] * 12, 4, 1024, self.RAM, self.GPU, max_sets=5)
        self.assertEqual([len(p) for p in parts], [4, 4, 4])

    def test_unknown_graphics_card_uses_ram_only(self):
        size, parts = sp.plan_scene([1] * 10, 4, 1024, self.RAM, None)
        self.assertEqual((size, len(parts)), (1024, 1))

    def test_graphics_memory_is_read_on_windows(self):
        if sys.platform == "win32":
            g = sp.gpu_memory_bytes()
            self.assertTrue(g is None or g > 256 * 1024 ** 2, g)


class Parts(unittest.TestCase):
    def test_small_scene_is_one_part(self):
        self.assertEqual(sp.pack_parts([1, 1, 1], 50), [[0, 1, 2]])

    def test_big_scene_parts_respect_the_limit_and_keep_order(self):
        weights = [1] * 965 + [2] * 34
        parts = sp.pack_parts(weights, 50)
        flat = [i for p in parts for i in p]
        self.assertEqual(flat, list(range(len(weights))))          # every object once, in order
        for p in parts:
            self.assertLessEqual(sum(weights[i] for i in p), 50)
        self.assertEqual(len(parts), -(-sum(weights) // 50))       # no more parts than needed
        sizes = [sum(weights[i] for i in p) for p in parts]
        self.assertLessEqual(max(sizes) - min(sizes), 3)           # evened out, no tiny leftover

    def test_object_heavier_than_the_limit_gets_its_own_part(self):
        parts = sp.pack_parts([1, 9, 1], 4)
        self.assertIn([1], parts)
        self.assertEqual(sorted(i for p in parts for i in p), [0, 1, 2])

    def test_empty(self):
        self.assertEqual(sp.pack_parts([], 10), [])
        self.assertEqual(sp.split_parts([], 10), [])

    def test_split_parts(self):
        parts = sp.split_parts(list(range(10)), 4)
        self.assertEqual([len(p) for p in parts], [4, 3, 3])

    def test_part_names(self):
        self.assertEqual(sp.part_name("Office_scene", 0, 1), "Office_scene")
        self.assertEqual(sp.part_name("Office_scene", 2, 21), "Office_scene_part03")
        self.assertEqual(sp.part_name("Office_scene", 99, 120), "Office_scene_part100")


def read_png(path):
    with open(path, "rb") as fh:
        data = fh.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        crc = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0]
        assert crc == zlib.crc32(body, zlib.crc32(kind)) & 0xFFFFFFFF
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    return ihdr, zlib.decompress(idat)


class Placeholders(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tp_png_")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_png_is_valid_for_odd_sizes(self):
        for w, h in ((1, 1), (3, 7), (64, 64), (1000, 3)):
            p = os.path.join(self.d, f"t_{w}_{h}.png")
            sc.write_png(p, w, h, (10, 200, 30))
            ihdr, raw = read_png(p)
            self.assertEqual(ihdr[:2], (w, h))
            self.assertEqual(len(raw), h * (1 + 3 * w))
            self.assertEqual(raw[:4], b"\x00\x0a\xc8\x1e")

    def test_big_placeholder_is_fast_and_correct(self):
        t = time.perf_counter()
        p = os.path.join(self.d, "big.png")
        sc.write_png(p, 4096, 4096, (128, 128, 255))
        self.assertLess(time.perf_counter() - t, 3.0)
        ihdr, raw = read_png(p)
        self.assertEqual(ihdr[:2], (4096, 4096))
        self.assertEqual(raw, (b"\x00" + bytes((128, 128, 255)) * 4096) * 4096)

    def test_many_files_reuse_the_encoding(self):
        t = time.perf_counter()
        for i in range(400):
            sc.write_png(os.path.join(self.d, f"s{i}.png"), 2048, 2048, (204, 204, 204))
        self.assertLess(time.perf_counter() - t, 10.0)    # was ~0.07 s each before

    def test_old_big_placeholder_is_shrunk_but_painted_is_kept(self):
        spec = sc.MAPS_BY_ID["base_color"]
        tex = os.path.join(self.d, "tex")
        os.makedirs(tex)
        old = os.path.join(tex, "Desk_BaseColor.png")
        with open(old, "wb") as fh:                       # what 1.2.0 wrote before the fix
            fh.write(sc._encode_png_rowwise(2048, 2048, spec.rgb))
        painted = os.path.join(tex, "Chair_BaseColor.png")
        data = bytearray(sc._encode_png(2048, 2048, (10, 20, 30)))
        with open(painted, "wb") as fh:
            fh.write(data)
        self.assertTrue(sc.is_untouched_placeholder(old, spec.rgb))
        self.assertFalse(sc.is_untouched_placeholder(painted, spec.rgb))
        sc.create_files(self.d, "Desk", ["base_color"], 64, textures_dir=tex, shrink=True)
        sc.create_files(self.d, "Chair", ["base_color"], 64, textures_dir=tex, shrink=True)
        self.assertEqual(sc.png_size(old), (64, 64))
        self.assertEqual(sc.png_size(painted), (2048, 2048))     # painted texture untouched
        with open(painted, "rb") as fh:
            self.assertEqual(fh.read(), bytes(data))
        # without shrink (single-object sets) nothing existing is ever replaced
        with open(old, "wb") as fh:
            fh.write(sc._encode_png_rowwise(512, 512, spec.rgb))
        sc.create_files(self.d, "Desk", ["base_color"], 64, textures_dir=tex)
        self.assertEqual(sc.png_size(old), (512, 512))

    def test_never_overwrites(self):
        p = os.path.join(self.d, "x.png")
        sc.write_png(p, 4, 4, (1, 2, 3))
        with self.assertRaises(FileExistsError):
            sc.write_png(p, 4, 4, (9, 9, 9))


if __name__ == "__main__":
    unittest.main(verbosity=2)
