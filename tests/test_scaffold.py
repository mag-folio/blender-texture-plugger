"""Pure-Python tests for texture_plugger/scaffold.py (no Blender needed).

Run:  python tests/test_scaffold.py      (or: python -m unittest discover tests)
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
SC_PATH = os.path.join(PKG, "scaffold.py")
DET_PATH = os.path.join(PKG, "detection.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sc = _load("tp_scaffold", SC_PATH)
det = _load("tp_detection", DET_PATH)

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def decode_png(path):
    """Minimal decoder for the 8-bit RGB, filter-0 PNGs scaffold writes.

    Returns (width, height, bit_depth, colour_type, rows) with rows a list of
    bytes (one per scanline, without the filter byte). Verifies every CRC.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    assert data[:8] == PNG_SIG, "bad PNG signature"
    pos = 8
    idat = b""
    ihdr = None
    kinds = []
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        (crc,) = struct.unpack(">I", data[pos + 8 + ln:pos + 12 + ln])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, f"bad CRC in {kind!r}"
        kinds.append(kind)
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + ln
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND", kinds
    w, h, depth, ctype, comp, filt, interlace = ihdr
    raw = zlib.decompress(idat)
    stride = 1 + 3 * w
    assert len(raw) == h * stride, (len(raw), w, h)
    rows = []
    for y in range(h):
        line = raw[y * stride:(y + 1) * stride]
        assert line[0] == 0, "unexpected filter type"
        rows.append(line[1:])
    return w, h, depth, ctype, rows


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tp_scaffold_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestNoBpy(unittest.TestCase):
    def test_module_does_not_import_bpy(self):
        with open(SC_PATH, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("import bpy", src)
        self.assertNotIn("from bpy", src)
        self.assertNotIn("bpy", sys.modules)


class TestNames(unittest.TestCase):
    def test_sanitize(self):
        s = sc.sanitize_name
        self.assertEqual(s("My Crate"), "My_Crate")
        self.assertEqual(s("Material.001"), "Material_001")
        self.assertEqual(s("  a   b  "), "a_b")
        self.assertEqual(s("a..b//c"), "a_b_c")
        self.assertEqual(s("keep-this_name"), "keep-this_name")
        self.assertEqual(s("Ström Kiste"), "Str_m_Kiste")
        self.assertEqual(s("木箱"), "TextureSet")

    def test_sanitize_fallback(self):
        self.assertEqual(sc.sanitize_name(""), "TextureSet")
        self.assertEqual(sc.sanitize_name(None), "TextureSet")
        self.assertEqual(sc.sanitize_name("***"), "TextureSet")
        self.assertEqual(sc.sanitize_name("", fallback="X"), "X")

    def test_texture_filename(self):
        for spec in sc.MAPS:
            self.assertEqual(sc.texture_filename("Crate", spec), f"Crate_{spec.suffix}.png")
        self.assertEqual(sc.texture_filename("Crate", sc.MAPS_BY_ID["base_color"]),
                         "Crate_BaseColor.png")

    def test_set_paths(self):
        p = sc.set_paths(os.path.join("root", "dir"), "Crate")
        self.assertEqual(p.folder, os.path.join("root", "dir", "Crate"))
        self.assertEqual(p.textures, os.path.join("root", "dir", "Crate", "textures"))
        self.assertEqual(p.mesh_dir, os.path.join("root", "dir", "Crate", "mesh"))
        self.assertEqual(p.mesh, os.path.join("root", "dir", "Crate", "mesh", "Crate.fbx"))

    def test_maps_table(self):
        self.assertEqual(len({m.id for m in sc.MAPS}), len(sc.MAPS))
        self.assertEqual(len({m.suffix for m in sc.MAPS}), len(sc.MAPS))
        self.assertEqual([m.id for m in sc.MAPS if m.default],
                         ["base_color", "normal", "metallic", "roughness"])


class TestWritePng(TempDirCase):
    def test_decodes_to_size_and_colour(self):
        path = os.path.join(self.tmp, "a.png")
        sc.write_png(path, 7, 5, (10, 20, 30))
        w, h, depth, ctype, rows = decode_png(path)
        self.assertEqual((w, h, depth, ctype), (7, 5, 8, 2))
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertEqual(row, bytes((10, 20, 30)) * 7)

    def test_ihdr_fields(self):
        path = os.path.join(self.tmp, "b.png")
        sc.write_png(path, 3, 2, (0, 0, 0))
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertEqual(data[:8], PNG_SIG)
        self.assertEqual(data[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">IIBBBBB", data[16:29]), (3, 2, 8, 2, 0, 0, 0))

    def test_large_multi_chunk(self):
        path = os.path.join(self.tmp, "c.png")
        # Noise-free solid colour compresses tiny; just check it decodes.
        sc.write_png(path, 300, 400, (255, 128, 1))
        w, h, _, _, rows = decode_png(path)
        self.assertEqual((w, h), (300, 400))
        self.assertTrue(all(r == bytes((255, 128, 1)) * 300 for r in rows))

    def test_colour_clamped(self):
        path = os.path.join(self.tmp, "d.png")
        sc.write_png(path, 1, 1, (-5, 999, 7))
        self.assertEqual(decode_png(path)[4][0], bytes((0, 255, 7)))

    def test_refuses_to_overwrite(self):
        path = os.path.join(self.tmp, "e.png")
        sc.write_png(path, 2, 2, (1, 2, 3))
        before = read_bytes(path)
        with self.assertRaises(FileExistsError):
            sc.write_png(path, 4, 4, (9, 9, 9))
        self.assertEqual(read_bytes(path), before)

    def test_bad_size(self):
        path = os.path.join(self.tmp, "f.png")
        with self.assertRaises(ValueError):
            sc.write_png(path, 0, 4, (0, 0, 0))
        self.assertFalse(os.path.exists(path))


class TestCreateFiles(TempDirCase):
    ALL = [m.id for m in sc.MAPS]

    def test_creates_folders_and_order(self):
        # Ids passed in reverse order: result is still in MAPS order.
        ids = list(reversed(self.ALL))
        res = sc.create_files(self.tmp, "Crate", ids, 4)
        paths = sc.set_paths(self.tmp, "Crate")
        self.assertTrue(os.path.isdir(paths.textures))
        self.assertTrue(os.path.isdir(paths.mesh_dir))
        self.assertEqual([spec.id for spec, _, _ in res], self.ALL)
        for spec, path, created in res:
            self.assertTrue(created)
            self.assertEqual(path, os.path.join(paths.textures, f"Crate_{spec.suffix}.png"))
            w, h, _, _, rows = decode_png(path)
            self.assertEqual((w, h), (4, 4))
            self.assertEqual(rows[0][:3], bytes(spec.rgb))

    def test_subset_and_unknown_ids(self):
        res = sc.create_files(self.tmp, "Crate", ["roughness", "base_color", "bogus"], 2)
        self.assertEqual([s.id for s, _, _ in res], ["base_color", "roughness"])
        self.assertEqual(sorted(os.listdir(sc.set_paths(self.tmp, "Crate").textures)),
                         ["Crate_BaseColor.png", "Crate_Roughness.png"])

    def test_no_maps(self):
        self.assertEqual(sc.create_files(self.tmp, "Crate", [], 4), [])

    def test_second_run_keeps_modified_file(self):
        res = sc.create_files(self.tmp, "Crate", ["base_color", "normal"], 4)
        target = res[0][1]
        os.remove(target)
        sc.write_png(target, 9, 3, (1, 2, 3))      # the "artist's" version
        before = read_bytes(target)
        res2 = sc.create_files(self.tmp, "Crate", ["base_color", "normal"], 4)
        self.assertEqual([c for _, _, c in res2], [False, False])
        self.assertEqual(read_bytes(target), before)

    def test_second_run_creates_only_missing(self):
        res = sc.create_files(self.tmp, "Crate", ["base_color", "normal"], 4)
        os.remove(res[1][1])
        res2 = sc.create_files(self.tmp, "Crate", ["base_color", "normal"], 4)
        self.assertEqual([c for _, _, c in res2], [False, True])
        self.assertTrue(os.path.isfile(res2[1][1]))


class TestFindProject(TempDirCase):
    def test_none_when_missing(self):
        self.assertIsNone(sc.find_project(self.tmp))
        self.assertIsNone(sc.find_project(os.path.join(self.tmp, "nope")))
        self.assertIsNone(sc.find_project(""))
        self.assertIsNone(sc.find_project(None))
        open(os.path.join(self.tmp, "notes.txt"), "w").close()
        os.makedirs(os.path.join(self.tmp, "dir.spp"))      # a folder is not a project
        self.assertIsNone(sc.find_project(self.tmp))

    def test_newest_wins(self):
        old = os.path.join(self.tmp, "old.spp")
        new = os.path.join(self.tmp, "new.spp")
        for p in (old, new):
            open(p, "wb").close()
        now = time.time()
        os.utime(old, (now - 1000, now - 1000))
        os.utime(new, (now, now))
        self.assertEqual(sc.find_project(self.tmp), new)
        os.utime(old, (now + 1000, now + 1000))
        self.assertEqual(sc.find_project(self.tmp), old)

    def test_folder_with_glob_characters(self):
        folder = os.path.join(self.tmp, "set [1]")
        os.makedirs(folder)
        spp = os.path.join(folder, "a.spp")
        open(spp, "wb").close()
        self.assertEqual(sc.find_project(folder), spp)

    def test_only_direct_children(self):
        sub = os.path.join(self.tmp, "sub")
        os.makedirs(sub)
        open(os.path.join(sub, "deep.spp"), "wb").close()
        self.assertIsNone(sc.find_project(self.tmp))


class TestPainter(TempDirCase):
    def test_find_painter_explicit(self):
        exe = os.path.join(self.tmp, "Painter.exe")
        open(exe, "wb").close()
        self.assertEqual(sc.find_painter(exe), exe)

    def test_find_painter_bogus_explicit(self):
        bogus = os.path.join(self.tmp, "missing.exe")
        result = sc.find_painter(bogus)
        self.assertNotEqual(result, bogus)
        auto = [c for c in sc.painter_candidates() if os.path.isfile(c)]
        # Do not assert on this machine's install: only that the fallback is
        # consistent with what auto-detection can see.
        self.assertEqual(result, auto[0] if auto else "")

    def test_find_painter_empty_is_string(self):
        self.assertIsInstance(sc.find_painter(""), str)

    def test_command_mesh_form(self):
        self.assertEqual(sc.painter_command("P.exe", "tex", mesh="m.fbx"),
                         ["P.exe", "--mesh", "m.fbx", "--export-path", "tex"])

    def test_command_project_form(self):
        self.assertEqual(sc.painter_command("P.exe", "tex", mesh="m.fbx", project="a.spp"),
                         ["P.exe", "--export-path", "tex", "a.spp"])
        self.assertEqual(sc.painter_command("P.exe", "tex", project="a.spp"),
                         ["P.exe", "--export-path", "tex", "a.spp"])


class TestDetectionRoundTrip(TempDirCase):
    NAMES = ("Crate", "Metal_Crate", "Rock_Normal", "Wall_2k", "Prop_1001", "Material_001")

    def test_every_generated_file_classifies(self):
        for set_name in self.NAMES:
            for spec in sc.MAPS:
                fn = sc.texture_filename(set_name, spec)
                with self.subTest(set=set_name, file=fn):
                    self.assertEqual(det.classify(fn).map_type, spec.map_type)

    def test_all_files_form_one_set(self):
        for set_name in self.NAMES:
            with self.subTest(set=set_name):
                res = sc.create_files(self.tmp, set_name, [m.id for m in sc.MAPS], 2)
                paths = [p for _, p, _ in res]
                analysis = det.analyze(paths)
                self.assertEqual(len(analysis.sets), 1, [s.name for s in analysis.sets])
                self.assertEqual(analysis.unassigned, [])
                ts = analysis.sets[0]
                self.assertEqual(sorted(ts.choices), sorted(m.map_type for m in sc.MAPS))
                for spec, path, _ in res:
                    self.assertEqual(os.path.normcase(ts.choices[spec.map_type].detection.path),
                                     os.path.normcase(path))
                    self.assertTrue(ts.choices[spec.map_type].enabled)


if __name__ == "__main__":
    unittest.main()
