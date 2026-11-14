"""Pure-Python tests for texture_plugger/detection.py (no Blender needed).

Run:  python tests/test_detection.py      (or: python -m unittest discover tests)
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DET_PATH = os.path.join(HERE, os.pardir, "texture_plugger", "detection.py")

_spec = importlib.util.spec_from_file_location("tp_detection", DET_PATH)
det = importlib.util.module_from_spec(_spec)
sys.modules["tp_detection"] = det
_spec.loader.exec_module(det)


def mt(name, **kw):
    return det.classify(name, **kw).map_type


class TestNoBpy(unittest.TestCase):
    def test_module_does_not_import_bpy(self):
        with open(DET_PATH, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("import bpy", src)
        self.assertNotIn("bpy", sys.modules)


class TestTokenize(unittest.TestCase):
    def texts(self, s):
        return [t.text for t in det.tokenize(s)]

    def test_separators_and_camel(self):
        self.assertEqual(self.texts("Bricks076C_1K-PNG_NormalGL"),
                         ["bricks", "076", "c", "1k", "png", "normal", "gl"])
        self.assertEqual(self.texts("AORoughness.metal"), ["ao", "roughness", "metal"])
        self.assertEqual(self.texts("my rock BaseColor"), ["my", "rock", "base", "color"])

    def test_kinds(self):
        toks = {t.text: t for t in det.tokenize("rock_4K_LOD0_VAR2_16bit_png_2048")}
        self.assertEqual(toks["4k"].kind, "res")
        self.assertEqual(toks["4k"].value, 4096)
        self.assertEqual(toks["lod0"].kind, "lod")
        self.assertEqual(toks["var2"].kind, "var")
        self.assertEqual(toks["16bit"].kind, "bits")
        self.assertEqual(toks["png"].kind, "fmt")
        self.assertEqual(toks["2048"].kind, "res")

    def test_unicode_kept_whole(self):
        self.assertEqual(self.texts("Ström_Albedo"), ["ström", "albedo"])

    def test_empty(self):
        self.assertEqual(det.tokenize(""), [])
        self.assertIsNone(mt(""))
        self.assertIsNone(mt(".png"))


class TestVendorNaming(unittest.TestCase):
    CASES = {
        # Poly Haven
        "aerial_rocks_02_diff_4k.jpg": "base_color",
        "aerial_rocks_02_nor_gl_4k.exr": "normal_gl",
        "aerial_rocks_02_nor_dx_4k.exr": "normal_dx",
        "aerial_rocks_02_rough_4k.jpg": "roughness",
        "aerial_rocks_02_arm_4k.jpg": "orm",
        "aerial_rocks_02_disp_4k.png": "height",
        "aerial_rocks_02_ao_4k.jpg": "ao",
        "aerial_rocks_02_spec_4k.jpg": "specular",
        "aerial_rocks_02_metal_4k.jpg": "metallic",
        # ambientCG
        "Bricks076C_1K-PNG_Color.png": "base_color",
        "Bricks076C_1K-PNG_NormalGL.png": "normal_gl",
        "Bricks076C_1K-PNG_NormalDX.png": "normal_dx",
        "Bricks076C_1K-PNG_Roughness.png": "roughness",
        "Bricks076C_1K-PNG_Displacement.png": "height",
        "Bricks076C_1K-PNG_AmbientOcclusion.png": "ao",
        "Metal032_2K-JPG_Metalness.jpg": "metallic",
        "Leaves001_2K-JPG_Opacity.jpg": "opacity",
        "Fabric_2K-JPG_Emission.jpg": "emission",
        # Quixel / Megascans
        "rock_sjkr2_4K_Albedo.jpg": "base_color",
        "rock_sjkr2_4K_Normal_LOD0.jpg": "normal_gl",
        "rock_sjkr2_4K_Roughness.jpg": "roughness",
        "rock_sjkr2_4K_Displacement.exr": "height",
        "rock_sjkr2_4K_Cavity.jpg": "cavity",
        "rock_sjkr2_4K_Gloss.jpg": "gloss",
        "rock_sjkr2_4K_Specular.jpg": "specular",
        "plant_4K_Translucency.jpg": "subsurface",
        "fabric_4K_Fuzz.jpg": "sheen",
        "rock_sjkr2_4K_Bump.jpg": "bump",
        "rock_sjkr2_4K_AO.jpg": "ao",
        # Substance
        "Material_BaseColor.png": "base_color",
        "Material_Metallic.png": "metallic",
        "Material_Normal_OpenGL.png": "normal_gl",
        "Material_Normal_DirectX.png": "normal_dx",
        "Material_Height.png": "height",
        "Material_Emissive.png": "emission",
        "Material_OcclusionRoughnessMetallic.png": "orm",
        "Material_ambient_occlusion.png": "ao",
        "Material_mixed_AO.png": "ao",
        "Material_Glossiness.png": "gloss",
        "Material_Opacity.png": "opacity",
        "Material_Transmission.png": "transmission",
        "Material_Anisotropy.png": "anisotropy",
        "Material_AnisotropyAngle.png": "anisotropy_rotation",
        "Material_CoatRoughness.png": "coat_roughness",
        "Material_Coat.png": "coat",
        "Material_SheenColor.png": "sheen_tint",
        "Material_SheenRoughness_Sheen.png": "sheen",
        "Material_Subsurface.png": "subsurface",
        # Textures.com
        "TexturesCom_Brick_Old_1K_albedo.tif": "base_color",
        "TexturesCom_Brick_Old_1K_normal.tif": "normal_gl",
        "TexturesCom_Brick_Old_1K_height.tif": "height",
        "TexturesCom_Brick_Old_1K_roughness.tif": "roughness",
        # FreePBR
        "bamboo-wood-semigloss-albedo.png": "base_color",
        "bamboo-wood-semigloss-ao.png": "ao",
        "bamboo-wood-semigloss-normal-ogl.png": "normal_gl",
        "bamboo-wood-semigloss-normal-dx.png": "normal_dx",
        "bamboo-wood-semigloss-metal.png": "metallic",
        "bamboo-wood-semigloss-roughness.png": "roughness",
        "bamboo-wood-semigloss-height.png": "height",
        # Poliigon
        "WoodFlooring044_COL_VAR1_3K.jpg": "base_color",
        "WoodFlooring044_NRM_3K.jpg": "normal_gl",
        "WoodFlooring044_NRM16_3K.tif": "normal_gl",
        "WoodFlooring044_GLOSS_3K.jpg": "gloss",
        "WoodFlooring044_REFL_3K.jpg": "specular",
        "WoodFlooring044_DISP16_3K.tif": "height",
        "WoodFlooring044_METALNESS_3K.jpg": "metallic",
        "WoodFlooring044_ROUGHNESS_3K.jpg": "roughness",
        "WoodFlooring044_AO_3K.jpg": "ao",
        "WoodFlooring044_SSS_3K.jpg": "subsurface",
        "WoodFlooring044_TRANSMISSION_3K.jpg": "transmission",
        "WoodFlooring044_ALPHAMASKED_3K.png": "base_color",
        "WoodFlooring044_MASK_3K.png": "opacity",
        # Game engine suffixes
        "T_Rock_01_D.tga": "base_color",
        "T_Rock_01_N.tga": "normal_gl",
        "T_Rock_01_R.tga": "roughness",
        "T_Rock_01_M.tga": "metallic",
        "T_Rock_01_S.tga": "specular",
        "T_Rock_01_H.tga": "height",
        "T_Rock_01_ORM.tga": "orm",
        "T_Rock_01_RMA.tga": "rma",
        "T_Rock_01_MRA.tga": "mra",
        "T_Rock_01_MRAO.tga": "mra",
        "Rock_MaskMap.png": "mask_map",
        "Rock_MetallicSmoothness.png": "metallic_smoothness",
        "Rock_AlbedoTransparency.png": "base_color",
        "Rock_ARM.png": "orm",
        "rock_preview.jpg": "preview",
    }

    def test_vendor_names(self):
        failures = []
        for name, expected in self.CASES.items():
            got = mt(name)
            if got != expected:
                failures.append(f"{name}: expected {expected}, got {got}")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))


class TestAmbiguity(unittest.TestCase):
    def test_set_name_containing_map_words(self):
        # "Metal" in the set name must not beat the real suffix.
        self.assertEqual(mt("Metal_Plate_02_ao.png"), "ao")
        self.assertEqual(mt("Metal_Plate_02_Roughness.png"), "roughness")
        self.assertEqual(mt("Red_Color_Brick_Normal.png"), "normal_gl")
        self.assertEqual(mt("Smooth_Concrete_Albedo.png"), "base_color")
        self.assertEqual(mt("Glossy_Tiles_Height_2k.png"), "height")

    def test_single_letters_only_as_final_suffix(self):
        self.assertIsNone(mt("d_rock.png"))
        self.assertIsNone(mt("r_stone_wall.png"))
        self.assertEqual(mt("Rock_d.png"), "base_color")
        self.assertEqual(mt("Rock_R_01.png"), "roughness")  # trailing number ignored
        self.assertEqual(mt("Rock_N_4k.png"), "normal_gl")
        # A letter glued into a longer word is not a token.
        self.assertIsNone(mt("Dandelion.png"))

    def test_whole_tokens_only(self):
        self.assertIsNone(mt("colonnade.png"))       # not "col"
        self.assertIsNone(mt("roughcast_wall.png"))  # not "rough"
        self.assertIsNone(mt("aorta.png"))           # not "ao"
        self.assertIsNone(mt("normality_check.png"))
        self.assertEqual(mt("bamboo-wood-semi-gloss-albedo.png"), "base_color")

    def test_longest_wins_at_same_position(self):
        self.assertEqual(mt("Mat_Sheen_Color.png"), "sheen_tint")
        self.assertEqual(mt("Mat_Coat_Roughness.png"), "coat_roughness")
        self.assertEqual(mt("Mat_Base_Color.png"), "base_color")
        self.assertEqual(mt("Mat_Ambient_Occlusion.png"), "ao")
        self.assertEqual(mt("Mat_Mask_Map.png"), "mask_map")

    def test_case_insensitive(self):
        self.assertEqual(mt("ROCK_BASECOLOR.PNG"), "base_color")
        self.assertEqual(mt("rock_basecolor.png"), "base_color")
        self.assertEqual(mt("RockBaseColor.png"), "base_color")
        self.assertEqual(mt("rockNormalDX.png"), "normal_dx")

    def test_normal_convention(self):
        self.assertEqual(mt("Rock_Normal.png"), "normal_gl")
        self.assertEqual(mt("Rock_Normal.png", normal_default="DX"), "normal_dx")
        self.assertEqual(mt("Rock_Normal_DX.png", normal_default="GL"), "normal_dx")
        self.assertEqual(mt("Rock_DirectX_Normal.png"), "normal_dx")
        self.assertEqual(mt("Rock_nrm_ogl.png", normal_default="DX"), "normal_gl")

    def test_unknown(self):
        self.assertIsNone(mt("holiday_photo.jpg"))
        self.assertIsNone(mt("Bricks076C.png"))

    def test_custom_keywords(self):
        kws = dict(det.DEFAULT_KEYWORDS)
        kws["base_color"] = ("farbe",)
        self.assertEqual(mt("Stein_Farbe.png", keywords=kws), "base_color")
        self.assertIsNone(mt("Stein_Albedo.png", keywords=kws))

    def test_parse_keyword_list(self):
        self.assertEqual(det.parse_keyword_list(" a, b ;c,, "), ("a", "b", "c"))
        self.assertEqual(det.normalize_keyword("Base Color"), "basecolor")


class TestSetKeys(unittest.TestCase):
    def test_same_key_across_maps(self):
        names = ["Bricks076C_1K-PNG_Color.png", "Bricks076C_1K-PNG_NormalGL.png",
                 "Bricks076C_2K-JPG_Roughness.jpg", "Bricks076C_1K-PNG_Displacement.png"]
        keys = {det.classify(n).set_key for n in names}
        self.assertEqual(keys, {"bricks_076_c"})

    def test_polyhaven_key(self):
        a = det.classify("aerial_rocks_02_nor_gl_4k.exr")
        b = det.classify("aerial_rocks_02_diff_1k.jpg")
        self.assertEqual(a.set_key, b.set_key)
        self.assertEqual(a.set_key, "aerial_rocks_02")
        self.assertEqual(a.resolution, 4096)

    def test_poliigon_bits_variant(self):
        d = det.classify("WoodFlooring044_NRM16_3K.tif")
        self.assertEqual(d.bits, 16)
        self.assertEqual(d.set_key, "wood_flooring_044")
        c = det.classify("WoodFlooring044_COL_VAR1_3K.jpg")
        self.assertEqual(c.variant, "var1")
        self.assertEqual(c.set_key, "wood_flooring_044")


class TestUdim(unittest.TestCase):
    def test_dot_udim(self):
        d = det.classify("/x/Hero_BaseColor.1001.png")
        self.assertEqual(d.udim_tile, 1001)
        self.assertEqual(d.map_type, "base_color")
        self.assertEqual(d.set_key, "hero")

    def test_underscore_udim_needs_siblings(self):
        lone = det.analyze(["/x/Crate_Roughness_1001.png", "/x/Crate_Albedo.png"])
        rough = [d for d in lone.detections if d.map_type == "roughness"][0]
        self.assertEqual(rough.udim_tiles, ())
        self.assertIsNone(rough.udim_tile)

        multi = det.analyze(["/x/Crate_Roughness_1001.png", "/x/Crate_Roughness_1002.png",
                             "/x/Crate_Albedo_1001.png", "/x/Crate_Albedo_1002.png",
                             "/x/Crate_Albedo_1011.png"])
        self.assertEqual(len(multi.sets), 1)
        ts = multi.sets[0]
        self.assertEqual(ts.choices["roughness"].detection.udim_tiles, (1001, 1002))
        self.assertEqual(ts.choices["base_color"].detection.udim_tiles, (1001, 1002, 1011))
        self.assertTrue(ts.choices["base_color"].detection.path.endswith("1001.png"))

    def test_dot_udim_grouping(self):
        a = det.analyze(["/x/Hero_BaseColor.1002.png", "/x/Hero_BaseColor.1001.png",
                         "/x/Hero_Normal.1001.exr"])
        ts = a.sets[0]
        self.assertEqual(ts.choices["base_color"].detection.udim_tiles, (1001, 1002))
        self.assertEqual(ts.choices["normal_gl"].detection.udim_tiles, (1001,))


class TestAnalyze(unittest.TestCase):
    def test_format_priority_and_resolution(self):
        paths = [
            "/p/rock_diff_4k.jpg", "/p/rock_diff_4k.png", "/p/rock_diff_2k.png",
            "/p/rock_nor_gl_4k.png", "/p/rock_nor_gl_4k.exr",
            "/p/rock_disp_4k.png", "/p/rock_disp_4k.exr",
        ]
        ts = det.analyze(paths).sets[0]
        self.assertTrue(ts.choices["base_color"].detection.path.endswith("rock_diff_4k.png"))
        self.assertTrue(ts.choices["normal_gl"].detection.path.endswith(".exr"))
        self.assertTrue(ts.choices["height"].detection.path.endswith(".exr"))
        self.assertEqual(len(ts.choices["base_color"].alternates), 2)

        low = det.analyze(paths, resolution_pref="LOWEST").sets[0]
        self.assertTrue(low.choices["base_color"].detection.path.endswith("rock_diff_2k.png"))
        target = det.analyze(paths, resolution_pref="2048").sets[0]
        self.assertTrue(target.choices["base_color"].detection.path.endswith("rock_diff_2k.png"))

        jpg_first = det.analyze(paths, format_priority="jpg,png").sets[0]
        self.assertTrue(jpg_first.choices["base_color"].detection.path.endswith(".jpg"))

    def test_variant_and_bits_preference(self):
        paths = ["/p/Wood_COL_VAR2_3K.jpg", "/p/Wood_COL_VAR1_3K.jpg",
                 "/p/Wood_NRM_3K.jpg", "/p/Wood_NRM16_3K.jpg"]
        ts = det.analyze(paths, data_format_priority="jpg").sets[0]
        self.assertTrue(ts.choices["base_color"].detection.path.endswith("VAR1_3K.jpg"))
        self.assertTrue(ts.choices["normal_gl"].detection.path.endswith("NRM16_3K.jpg"))

    def test_multiple_sets_grouping(self):
        paths = [
            "/m/Bricks01_Color.png", "/m/Bricks01_Roughness.png", "/m/Bricks01_NormalGL.png",
            "/m/Wood02_Color.png", "/m/Wood02_Roughness.png",
            "/m/Wood02_NormalGL.png", "/m/Wood02_AmbientOcclusion.png",
        ]
        a = det.analyze(paths)
        self.assertEqual([s.key for s in a.sets], ["wood_02", "bricks_01"])
        self.assertEqual(a.sets[0].map_count, 4)
        self.assertEqual(a.sets[1].map_count, 3)

    def test_conflicts(self):
        ts = det.analyze(["/c/x_NormalGL.png", "/c/x_NormalDX.png",
                          "/c/x_Roughness.png", "/c/x_Gloss.png", "/c/x_Cavity.png"]).sets[0]
        self.assertTrue(ts.choices["normal_gl"].enabled)
        self.assertFalse(ts.choices["normal_dx"].enabled)
        self.assertTrue(ts.choices["roughness"].enabled)
        self.assertFalse(ts.choices["gloss"].enabled)
        self.assertFalse(ts.choices["cavity"].enabled)
        self.assertEqual(ts.map_count, 2)

    def test_empty_key_uses_folder(self):
        ts = det.analyze([os.path.join("C:\\", "mats", "Leather", "albedo.png"),
                          os.path.join("C:\\", "mats", "Leather", "normal.png")]).sets[0]
        self.assertEqual(ts.name, "Leather")
        self.assertEqual(set(ts.choices), {"base_color", "normal_gl"})

    def test_unassigned_attached(self):
        a = det.analyze(["/a/Bricks076C_1K-PNG_Color.png", "/a/Bricks076C.png",
                         "/a/Bricks076C_1K-PNG_Roughness.png"])
        self.assertEqual(len(a.unassigned), 1)
        self.assertEqual([d.filename for d in a.sets[0].unassigned], ["Bricks076C.png"])

    def test_duplicates_and_non_images_ignored(self):
        a = det.analyze(["/a/x_Color.png", "/a/x_Color.png", "/a/readme.txt", "", None])
        self.assertEqual(len(a.detections), 1)

    def test_ordered_choices(self):
        ts = det.analyze(["/o/x_Height.png", "/o/x_Albedo.png", "/o/x_Roughness.png"]).sets[0]
        self.assertEqual([c.map_type for c in ts.ordered_choices()],
                         ["base_color", "roughness", "height"])


class TestFolderScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tp_det_")
        for n in ("a_Color.png", "a_Normal.JPG", "notes.txt"):
            open(os.path.join(self.tmp, n), "w").close()
        os.makedirs(os.path.join(self.tmp, "sub"))
        open(os.path.join(self.tmp, "sub", "a_Roughness.png"), "w").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan(self):
        flat = det.list_image_files(self.tmp)
        self.assertEqual([os.path.basename(p) for p in flat], ["a_Color.png", "a_Normal.JPG"])
        deep = det.list_image_files(self.tmp, recursive=True)
        self.assertEqual(len(deep), 3)
        self.assertEqual(det.list_image_files(os.path.join(self.tmp, "missing")), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
