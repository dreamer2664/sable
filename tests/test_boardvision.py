#!/usr/bin/env python3
"""Tests for tools/boardvision.py — the board-location and read path.

These run headless, on a laptop, with no PC, no pc-eye, no lichess and no
screenshots taken live: the fixtures are the 238 real frames the live games left
in `results/bot_shots/`, committed with the repo. That matters, because the bugs
this module fixes were invisible to a synthetic test and obvious on real frames.

    python3 -m unittest tests.test_boardvision -v
    SABLE_VISION_TEST_N=40 python3 -m unittest tests.test_boardvision   # faster

Environment overrides: SABLE_VISION_TEST_N (frames to use), SABLE_SHOTS (dir).
"""
import glob
import json
import os
import sys
import unittest

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import boardvision as V  # noqa: E402

SHOTS = os.environ.get("SABLE_SHOTS", os.path.join(ROOT, "results", "bot_shots"))
N = int(os.environ.get("SABLE_VISION_TEST_N", "0")) or None

# The box hand-verified against the live lichess window these frames came from.
TRUTH = (607, 173, 1309, 876)
TOL = 2      # px, per edge


def poll_frames():
    fs = sorted(glob.glob(os.path.join(SHOTS, "*poll*.jpg")))
    return fs[:N] if N else fs


def load(path):
    return Image.open(path).convert("RGB")


def delta(box):
    return max(abs(box[i] - TRUTH[i]) for i in range(4))


class TestGeometry(unittest.TestCase):
    """The regression that started this: 60% -> 100% frame coverage."""

    def test_every_recorded_frame_is_located(self):
        frames = poll_frames()
        if not frames:
            self.skipTest(f"no fixtures in {SHOTS}")
        failures, deltas = [], []
        for f in frames:
            try:
                g = V.find_board(load(f))
                deltas.append(delta(g["bbox"]))
            except Exception as e:
                failures.append((os.path.basename(f), str(e)))
        self.assertEqual(failures, [], f"{len(failures)}/{len(frames)} frames unreadable: {failures[:3]}")
        worst = max(deltas)
        self.assertLessEqual(
            worst, TOL,
            f"geometry is wrong by up to {worst}px on {os.path.basename(frames[int(np.argmax(deltas))])}")

    def test_refuses_a_screen_with_no_board(self):
        # grey gradient: no two distinct saturated mid-tone colours anywhere
        a = np.tile(np.arange(0, 256, 8, dtype=np.uint8)[None, :, None], (200, 1, 3))
        with self.assertRaises(RuntimeError) as cm:
            V.find_board(Image.fromarray(a))
        self.assertIn("board colours", str(cm.exception))

    def test_dark_background_does_not_hide_the_squares(self):
        """A dark page (0,0,0)/(1,1,1)/(2,2,2) must not fill the colour shortlist.

        Regression for ranking colours by frequency *before* filtering to
        board-like ones: on lichess's dark theme that returned no colours at all.
        """
        sq = 25
        a = np.zeros((400, 400, 3), dtype=np.uint8)          # dark page
        for r in range(8):
            for f in range(8):
                col = (238, 218, 185) if (r + f) % 2 == 0 else (174, 138, 106)
                a[100 + r * sq:100 + (r + 1) * sq, 100 + f * sq:100 + (f + 1) * sq] = col
        cols = V.board_colours(a)
        self.assertIsNotNone(cols, "board colours lost behind the dark background")
        for c in cols:
            self.assertGreaterEqual(int(c.mean()), 60)

    def test_squares_are_square(self):
        frames = poll_frames()
        if not frames:
            self.skipTest("no fixtures")
        g = V.find_board(load(frames[0]))
        L, T, R, B = g["bbox"]
        self.assertLessEqual(abs((R - L) - (B - T)), TOL)

    def test_square_boxes_tile_the_board_exactly(self):
        geo = {"bbox": [100, 200, 100 + 8 * 60 - 1, 200 + 8 * 60 - 1], "sq": 60.0}
        boxes = [V.square_box(geo, f, r) for f in range(8) for r in range(8)]
        self.assertEqual(min(b[0] for b in boxes), 100)
        self.assertEqual(min(b[1] for b in boxes), 200)
        self.assertEqual(max(b[2] for b in boxes), 100 + 8 * 60)
        self.assertEqual(max(b[3] for b in boxes), 200 + 8 * 60)
        # every square the same size, so an off-by-one cannot accumulate
        widths = {b[2] - b[0] for b in boxes}
        self.assertEqual(len(widths), 1, f"unequal square widths {widths}")

    def test_rank_index_zero_is_rank_one(self):
        geo = {"bbox": [0, 0, 799, 799], "sq": 100.0}
        f = V.square_centre(geo, 0, 0)          # a1
        self.assertEqual(f, (50, 750))          # bottom-left
        f = V.square_centre(geo, 7, 7)          # h8
        self.assertEqual(f, (750, 50))          # top-right
        f = V.square_centre(geo, 0, 0, flipped=True)  # a1 when the board is flipped
        self.assertEqual(f, (750, 50))


class TestCropGeometry(unittest.TestCase):
    """Asking the server for the board region makes geometry free."""

    def test_crop_geometry_is_the_whole_image(self):
        im = Image.new("RGB", (704, 704), (174, 138, 106))
        g = V.geo_from_crop(im)
        self.assertEqual(g["bbox"], [0, 0, 703, 703])
        self.assertEqual(g["method"], "crop")
        self.assertEqual(g["ms"], 0.0)

    def test_crop_must_be_plausibly_square(self):
        with self.assertRaises(RuntimeError):
            V.geo_from_crop(Image.new("RGB", (704, 300)))

    def test_region_string_matches_the_board(self):
        geo = {"bbox": [607, 173, 1309, 875], "sq": 87.75}
        self.assertEqual(V.board_region(geo), "607,173,703,703")
        self.assertEqual(V.board_region(geo, margin=8), "599,165,719,719")
        # never negative, even for a board against the screen edge
        geo0 = {"bbox": [2, 2, 500, 500], "sq": 62.0}
        x, y, w, h = (int(v) for v in V.board_region(geo0, margin=8).split(","))
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)

    def test_reading_a_crop_gives_the_same_position_as_a_full_frame(self):
        """The in-game fast path must not change what the recogniser sees."""
        frames = poll_frames()
        if not frames:
            self.skipTest("no fixtures")
        import lichess_bot as B
        rec = B.Recogniser()
        if not rec.load(os.path.join(ROOT, "results", "templates.npz")):
            self.skipTest("no cached templates")
        for f in frames[:5]:
            im = load(f)
            geo = V.find_board(im)
            L, T, R, Bb = geo["bbox"]
            crop = im.crop((L, T, R + 1, Bb + 1))
            full_read, _ = V.read_board(rec, im, geo, False)
            crop_read, _ = V.read_board(rec, crop, V.geo_from_crop(crop), False)
            self.assertEqual(full_read.board_fen(), crop_read.board_fen(),
                             f"crop read differs from full-frame read on {os.path.basename(f)}")


class TestConfidenceGate(unittest.TestCase):
    """The gate the live loop was missing: a wrong read must be refusable."""

    @classmethod
    def setUpClass(cls):
        cls.rec = None
        try:
            import lichess_bot as B
            cls.B = B
            cls.rec = B.Recogniser()
            if not cls.rec.load(os.path.join(ROOT, "results", "templates.npz")):
                cls.rec = None
        except Exception:
            cls.rec = None
        cls.im, cls.geo, cls.want = None, None, None
        p = os.path.join(ROOT, "results", "L6_1", "final_screen.jpg")
        j = os.path.join(ROOT, "results", "g_L6_1.json")
        if os.path.exists(p) and os.path.exists(j):
            cls.im = load(p)
            cls.geo = V.find_board(cls.im)
            cls.want = [e for e in json.load(open(j)) if e.get("event") == "final"][-1]["local_fen"].split()[0]

    def setUp(self):
        if self.rec is None or self.im is None:
            self.skipTest("templates or fixture missing")

    def test_clean_read_is_accepted_and_exact(self):
        b, r = V.read_plausible(self.rec, self.im, self.geo, False)
        self.assertEqual(r, "ok")
        self.assertEqual(b.board_fen(), self.want)

    def test_either_side_to_move_is_accepted(self):
        """Whose turn it is cannot be seen; assuming our own side must not refuse."""
        import chess
        for side in (chess.WHITE, chess.BLACK):
            b, r = V.read_plausible(self.rec, self.im, self.geo, False, side=side)
            self.assertEqual(r, "ok", f"refused for side {side}")

    def test_misaligned_board_is_refused(self):
        for shift in (7, 17):
            g = dict(self.geo)
            L, T, R, Bb = self.geo["bbox"]
            g["bbox"] = [L + shift, T + shift, R + shift, Bb + shift]
            b, r = V.read_plausible(self.rec, self.im, g, False)
            self.assertIsNone(b, f"a {shift}px misalignment was accepted")
            # any specific reason is fine -- king count, confidence, invalid
            # position -- as long as it refuses and says something useful.
            self.assertTrue(r and r != "ok", f"unhelpful refusal reason {r!r}")

    def test_threshold_is_measured_not_guessed(self):
        """The gate sits between the measured clean and misaligned populations.

        Clean reads measure a mean margin of 0.223..0.456; a 7-17px misalignment
        measures 0.040..0.122. The gate must fall strictly between them, or it is
        either refusing good frames or accepting bad ones.
        """
        _, detail = V.read_board(self.rec, self.im, self.geo, False)
        clean_mean = float(np.mean([v[1] for v in detail.values() if v[0] != "."]))
        self.assertGreater(clean_mean, V.DEFAULT_MIN_CONFIDENCE,
                           f"a clean read ({clean_mean:.3f}) fails the gate "
                           f"({V.DEFAULT_MIN_CONFIDENCE})")
        self.assertGreaterEqual(clean_mean / V.DEFAULT_MIN_CONFIDENCE, 1.2,
                                "not enough headroom between a clean read and the gate")

    def test_a_real_start_position_passes_the_gate(self):
        """Eight identical pawns score low individually; the mean must still pass."""
        p = os.path.join(ROOT, "results", "bot_shots", "1792954475704_wait.jpg")
        if not os.path.exists(p):
            self.skipTest("no start-position fixture")
        im = load(p)
        geo = V.find_board(im)
        b, r = V.read_plausible(self.rec, im, geo, False)
        self.assertEqual(r, "ok", "a real start position was refused by the gate")
        self.assertEqual(b.board_fen(), __import__("chess").Board().board_fen())


class TestCache(unittest.TestCase):
    def test_second_lookup_does_not_recompute(self):
        frames = poll_frames()
        if not frames:
            self.skipTest("no fixtures")
        im = load(frames[0])
        cache = V.GeometryCache()
        calls = {"n": 0}
        real = V.find_board

        def counting(*a, **k):
            calls["n"] += 1
            return real(*a, **k)

        V.find_board = counting
        try:
            a = cache.get(im)
            b = cache.get(im)
        finally:
            V.find_board = real
        self.assertEqual(calls["n"], 1, "geometry was recomputed on a cache hit")
        self.assertEqual(a["bbox"], b["bbox"])

    def test_cache_is_keyed_by_image_size(self):
        cache = V.GeometryCache()
        frames = poll_frames()
        if len(frames) < 2:
            self.skipTest("no fixtures")
        im = load(frames[0])
        cache.set(im, {"bbox": [1, 2, 3, 4], "sq": 1.0, "method": "x", "ms": 0})
        other = im.resize((im.width // 2, im.height // 2))
        self.assertNotEqual(cache.get(other)["bbox"], [1, 2, 3, 4])

    def test_clear_forgets_everything(self):
        cache = V.GeometryCache()
        frames = poll_frames()
        if not frames:
            self.skipTest("no fixtures")
        im = load(frames[0])
        cache.get(im)
        cache.clear()
        self.assertEqual(cache._geo, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
