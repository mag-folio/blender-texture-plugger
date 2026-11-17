"""Painter plugin: which exported files Load from Blender takes back, and where a
never-saved project is saved on export.  Plain Python: ``python tests/test_bridge_sync.py``.
"""

import importlib.util
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "tp_bridge_sync", os.path.join(ROOT, "texture_plugger", "painter_plugin",
                                   "texture_plugger_bridge.py"))
br = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(br)


class LoadFiles(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tp_tex_")
        for n in ("Crate_BaseColor.png", "Crate_Roughness.png", "Crate_Metallic.png",
                  "Crate_Normal.png", "Crate_Displacement.png", "crate_emission.png",
                  "Crate.001_BaseColor.png", "Barrel_BaseColor.png", "Crate_BaseColor.jpg"):
            open(os.path.join(self.d, n), "wb").close()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_takes_colour_maps_not_normal_or_height(self):
        got = br.blender_texture_files(self.d, "Crate")
        self.assertEqual(set(got), {"BaseColor", "Roughness", "Metallic", "Emission"})
        self.assertEqual(os.path.basename(got["BaseColor"]), "Crate_BaseColor.png")
        self.assertEqual(os.path.basename(got["Emission"]), "crate_emission.png")  # any case

    def test_set_names_do_not_bleed_into_each_other(self):
        self.assertEqual(set(br.blender_texture_files(self.d, "Crate.001")), {"BaseColor"})
        self.assertEqual(set(br.blender_texture_files(self.d, "Barrel")), {"BaseColor"})
        self.assertEqual(br.blender_texture_files(self.d, "Lamp"), {})

    def test_missing_folder(self):
        self.assertEqual(br.blender_texture_files(os.path.join(self.d, "nope"), "Crate"), {})


class FakeChannel:
    """Painter's ChannelType: legacy names are aliases of the OpenPBR members."""
    BaseColor = "base_color"
    SpecularRoughness = Roughness = "roughness"
    BaseMetalness = Metallic = "metalness"
    EmissionColor = Emissive = "emission"
    Opacity = "opacity"
    Normal = "normal"
    Height = "height"


class Channels(unittest.TestCase):
    FILES = {"BaseColor": "b.png", "Roughness": "r.png", "Metallic": "m.png", "Emission": "e.png"}

    def test_openpbr_project_gets_roughness_and_metallic(self):
        # what Painter 12.2's default project reports from all_channels()
        stack = ["base_color", "height", "roughness", "metalness", "normal"]
        got = br.channels_for_files(self.FILES, FakeChannel, stack)
        self.assertEqual(got, {"base_color": "b.png", "roughness": "r.png", "metalness": "m.png"})

    def test_channels_the_stack_lacks_are_skipped(self):
        got = br.channels_for_files(self.FILES, FakeChannel, ["base_color"])
        self.assertEqual(got, {"base_color": "b.png"})

    def test_emission_and_opacity_when_present(self):
        got = br.channels_for_files({"Emission": "e.png", "Alpha": "a.png"}, FakeChannel,
                                    ["emission", "opacity"])
        self.assertEqual(got, {"emission": "e.png", "opacity": "a.png"})


class SavePath(unittest.TestCase):
    def test_named_after_the_set_folder(self):
        p = br.project_save_path(os.path.join("C:", os.sep, "t", "Crate"))
        self.assertEqual(os.path.basename(p), "Crate.spp")
        self.assertEqual(os.path.basename(os.path.dirname(p)), "Crate")

    def test_never_overwrites_another_project(self):
        p = br.project_save_path("C:/t/Scene", ["Scene.spp", "scene_1.SPP"])
        self.assertEqual(os.path.basename(p), "Scene_2.spp")


if __name__ == "__main__":
    unittest.main()
