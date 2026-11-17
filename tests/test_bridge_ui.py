"""Painter plugin: the safety checks behind the panel's confirmations.
Plain Python: ``python tests/test_bridge_ui.py``.
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "tp_bridge_ui", os.path.join(ROOT, "texture_plugger", "painter_plugin",
                                 "texture_plugger_bridge.py"))
br = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(br)


class KindSwaps(unittest.TestCase):
    def test_only_changes_of_kind_count(self):
        rec = {"A": "env_1.exr", "B": "lightmap_render_B_1.exr", "C": "env_1.exr",
               "D": "lightmap_render_D_1.exr"}
        wanted = {"A": "env_2.exr",                       # same kind: safe update
                  "B": "env_2.exr",                       # lightmap -> filter: swap
                  "C": "lightmap_render_C_1.exr",         # filter -> lightmap: swap
                  "D": "lightmap_render_D_1.exr"}         # unchanged
        self.assertEqual(br.lit_updates(rec, wanted), ["A", "B", "C"])
        self.assertEqual(br.lit_kind_swaps(rec, wanted), ["B", "C"])

    def test_no_wanted_map_is_no_swap(self):
        self.assertEqual(br.lit_kind_swaps({"A": "lightmap_render_A.exr"}, {"A": None}), [])


class LoadCover(unittest.TestCase):
    def test_user_layers_exclude_plugin_layers(self):
        self.assertEqual(br.user_layers(["Lit Paint", "Wall paint", "Blender Lighting",
                                         "From Blender", "Base"]), ["Wall paint", "Base"])

    def test_new_layer_over_paint_is_flagged(self):
        self.assertTrue(br.load_would_cover(["Lit Paint", "Blender Lighting", "Wall paint"]))

    def test_existing_from_blender_layer_is_only_updated(self):
        self.assertFalse(br.load_would_cover(["From Blender", "Wall paint"]))

    def test_empty_set_is_safe(self):
        self.assertFalse(br.load_would_cover([]))
        self.assertFalse(br.load_would_cover(["Lit Paint", "Blender Lighting"]))


class Older(unittest.TestCase):
    def test_older_than_project_save(self):
        self.assertEqual(br.older_than({"A": 100.0, "B": 200.0, "C": 199.0}, 200.0), ["A"])

    def test_never_saved_project_flags_nothing(self):
        self.assertEqual(br.older_than({"A": 1.0}, 0), [])


class Labels(unittest.TestCase):
    def test_short_list(self):
        self.assertEqual(br.short_list(["a", "b"]), "a, b")
        self.assertEqual(br.short_list(list("abcdef"), 4), "a, b, c, d and 2 more")

    def test_every_action_has_a_direction_and_kind(self):
        for action, (direction, kind) in br.ACTIONS.items():
            self.assertIn(direction, br.DIRECTION_WORDS, action)
            self.assertIn(kind, ("light", "texture", "view"), action)
        self.assertEqual(br.ACTIONS["send_all"][0], "p2b")
        self.assertEqual(br.ACTIONS["get_all"][0], "b2p")


class Placeholders(unittest.TestCase):
    def _png(self, w, h):
        import struct, tempfile, zlib
        raw = b"".join(b"\x00" + b"\x80\x80\x80" * w for _ in range(h))
        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        fd, path = tempfile.mkstemp(suffix=".png")
        os.write(fd, data)
        os.close(fd)
        self.addCleanup(os.remove, path)
        return path

    def test_blender_stand_in_is_a_placeholder(self):
        self.assertTrue(br.is_placeholder_png(self._png(64, 64)))

    def test_painter_export_is_not(self):
        self.assertFalse(br.is_placeholder_png(self._png(512, 512)))
        self.assertFalse(br.is_placeholder_png(self._png(128, 128)))

    def test_unreadable_file_is_not(self):
        self.assertFalse(br.is_placeholder_png(os.path.join(ROOT, "README.md")))
        self.assertFalse(br.is_placeholder_png(os.path.join(ROOT, "no_such.png")))


class MissingLightingFiles(unittest.TestCase):
    """Painter re-reads imported lighting maps on save; gone files block the save."""

    def test_stem_from_resource_name(self):
        self.assertEqual(br.lighting_stem("Blender lighting lightmap_render_Wall_17"),
                         "lightmap_render_Wall_17")
        self.assertIsNone(br.lighting_stem("From Blender Wall_BaseColor 12"))

    def test_set_from_lightmap_stem(self):
        self.assertEqual(br.lightmap_set_from_stem("lightmap_render_B_zierCurve_1794880495359"),
                         "B_zierCurve")
        self.assertEqual(br.lightmap_set_from_stem("lightmap_preview_cubicle_2_151_2_99"),
                         "cubicle_2_151_2")
        self.assertIsNone(br.lightmap_set_from_stem("env_1794880703743"))

    def test_stand_in_is_the_current_map(self):
        lm = {"sets": {"B_zierCurve": {"render": "lightmap_render_B_zierCurve_2.exr",
                                       "preview": "lightmap_preview_B_zierCurve_2.exr"}}}
        link = {"environment": "env_9.exr", "preview": {"environment": "env_preview_9.exr"}}
        self.assertEqual(br.replacement_for("lightmap_render_B_zierCurve_1", lm, link),
                         "lightmap_render_B_zierCurve_2.exr")
        self.assertEqual(br.replacement_for("lightmap_preview_B_zierCurve_1", lm, link),
                         "lightmap_preview_B_zierCurve_2.exr")
        self.assertEqual(br.replacement_for("env_1", lm, link), "env_9.exr")
        self.assertEqual(br.replacement_for("env_preview_1", lm, link), "env_preview_9.exr")
        self.assertIsNone(br.replacement_for("lightmap_render_Gone_1", lm, link))


if __name__ == "__main__":
    unittest.main()
