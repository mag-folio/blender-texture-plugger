"""Pure tests of the Painter plugin's orbit/zoom maths."""
import importlib.util, math, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "tp_bridge_orbit", os.path.join(HERE, os.pardir, "texture_plugger", "painter_plugin",
                                    "texture_plugger_bridge.py"))
br = importlib.util.module_from_spec(spec)
sys.modules["tp_bridge_orbit"] = br
spec.loader.exec_module(br)


def dist(a, b):
    return math.dist(a, b)


class OrbitTests(unittest.TestCase):
    C = [10.0, 5.0, -3.0]

    def test_distance_kept_and_looks_at_centre(self):
        pos = [10.0, 8.0, 40.0]
        d0 = dist(pos, self.C)
        for yaw, pitch in ((15, 0), (-90, 10), (180, -30), (400, 400)):
            new, fwd = br.orbit_around(pos, self.C, yaw, pitch)
            self.assertAlmostEqual(dist(new, self.C), d0, places=6)
            to_c = [self.C[i] - new[i] for i in range(3)]
            n = math.sqrt(sum(x * x for x in to_c))
            for i in range(3):
                self.assertAlmostEqual(fwd[i], to_c[i] / n, places=6)

    def test_pitch_clamped_never_flips(self):
        new, fwd = br.orbit_around([0, 0, 10], [0, 0, 0], 0, 500)
        self.assertLess(abs(fwd[0]) + abs(fwd[2]), 1.0)
        self.assertLess(abs(fwd[1]), 1.0)
        br.euler_from_basis(fwd)        # must be a valid camera

    def test_full_turn_returns(self):
        pos = [3.0, 2.0, 9.0]
        p = pos
        for _ in range(24):
            p, _f = br.orbit_around(p, self.C, 15, 0)
        for i in range(3):
            self.assertAlmostEqual(p[i], pos[i], places=6)

    def test_camera_on_centre_is_safe(self):
        new, _ = br.orbit_around(self.C, self.C, 10, 10)
        self.assertTrue(all(math.isfinite(x) for x in new))

    def test_zoom(self):
        pos = [10.0, 5.0, 17.0]
        z = br.zoom_about(pos, self.C, 0.5)
        self.assertAlmostEqual(dist(z, self.C), dist(pos, self.C) / 2, places=6)
        z = br.zoom_about(pos, self.C, 0.0)      # never reaches the centre
        self.assertGreater(dist(z, self.C), 0.0)


if __name__ == "__main__":
    unittest.main()
