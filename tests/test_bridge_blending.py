"""Painter's Passthrough blending mode is found under either spelling.

Painter 12.2 documents ``BlendingMode.PassThrough`` but the application names it
``Passthrough``; the plugin's Bake Lighting failed on the documented name.
Plain Python: run ``python tests/test_bridge_blending.py``.
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "tp_bridge_blend", os.path.join(ROOT, "texture_plugger", "painter_plugin",
                                    "texture_plugger_bridge.py"))
br = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(br)


class PainterReal:            # what Painter 12.2 actually has
    Normal = "normal"
    Passthrough = "passthrough"
    Multiply = "multiply"


class PainterDocumented:      # what its documentation says
    Normal = "normal"
    PassThrough = "passthrough"


class NoPassthrough:
    Normal = "normal"


class Passthrough(unittest.TestCase):
    def test_application_spelling(self):
        self.assertEqual(br.passthrough_mode(PainterReal), "passthrough")

    def test_documented_spelling(self):
        self.assertEqual(br.passthrough_mode(PainterDocumented), "passthrough")

    def test_missing_is_a_clear_error(self):
        with self.assertRaises(RuntimeError):
            br.passthrough_mode(NoPassthrough)


if __name__ == "__main__":
    unittest.main()
