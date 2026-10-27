#!/usr/bin/env python3
"""End-to-end tests for the play loop, with no PC and no lichess.

These drive `lichess_bot.play_game()` against `tests/virtualscreen.py`: a board
rendered from the real lichess palette and the real piece glyphs, answering the
same calls pc-eye answers. Nothing is mocked inside the code under test -- the
board is genuinely located from pixels, the pieces are genuinely recognised, the
clicks genuinely move pieces on a python-chess board.

    python3 -m unittest tests.test_loop -v
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import chess  # noqa: E402

import virtualscreen as VS  # noqa: E402

STUB = os.path.join(HERE, "stub_engine.py")
REAL = os.path.join(ROOT, "sable")


def fresh_module(screen, engine, stub_moves="", shots_dir=None):
    """Import lichess_bot against a virtual screen (must be done per test)."""
    # Never let a test write into results/: that directory is committed evidence,
    # and the vision tests read every frame in results/bot_shots as a real capture.
    tmp = shots_dir or tempfile.mkdtemp(prefix="sable-shots-")
    os.environ["SABLE_SHOTS_DIR"] = tmp
    # ...and use a private COPY of the committed templates, so a test can never
    # rewrite the real ones.
    committed = os.path.join(ROOT, "results", "templates.npz")
    tpl = os.path.join(tmp, "templates.npz")
    if os.path.exists(committed) and not os.path.exists(tpl):
        import shutil
        shutil.copyfile(committed, tpl)
    os.environ["SABLE_TEMPLATES"] = tpl
    VS.install(screen)
    os.environ["SABLE_ENGINE"] = engine
    os.environ["SABLE_SETOPTIONS"] = ""
    if stub_moves:
        os.environ["STUB_MOVES"] = stub_moves
    else:
        os.environ.pop("STUB_MOVES", None)
    for name in ("lichess_bot", "boardvision"):
        sys.modules.pop(name, None)
    import lichess_bot
    lichess_bot.reset_geometry_cache()
    lichess_bot.reset_engines()
    return lichess_bot


def events(result, name):
    return [e for e in result["log"] if e.get("event") == name]


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sable-loop-")

    def tearDown(self):
        import lichess_bot
        lichess_bot.reset_engines()


class TestRenderedBoardIsReadable(LoopTest):
    """The fixture must be worth trusting before it is used to judge anything."""

    def test_virtual_screen_round_trips_through_the_recogniser(self):
        screen = VS.VirtualScreen()
        B = fresh_module(screen, STUB)
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(screen.shot())).convert("RGB")
        import boardvision as V
        geo = V.find_board(im)
        L, T, R, Bb = geo["bbox"]
        self.assertLessEqual(max(abs(L - VS.BOARD[0]), abs(T - VS.BOARD[1]),
                                 abs(R - VS.BOARD[2]), abs(Bb - VS.BOARD[3])), 3)
        # Use the committed templates, which is what the loop does now: learning
        # from the frame under test would grade the fixture with itself.
        rec = B.Recogniser()
        if not rec.load(os.path.join(ROOT, "results", "templates.npz")):
            self.skipTest("no committed templates")
        read, _ = V.read_board(rec, im, geo, False)
        self.assertEqual(read.board_fen(), chess.Board().board_fen())

    def test_black_orientation_is_rendered_mirrored_and_readable(self):
        screen = VS.VirtualScreen(our_side=chess.BLACK)
        B = fresh_module(screen, STUB)
        from PIL import Image
        import io
        import boardvision as V
        im = Image.open(io.BytesIO(screen.shot())).convert("RGB")
        geo = V.find_board(im)
        rec = B.Recogniser()
        if not rec.load(os.path.join(ROOT, "results", "templates.npz")):
            self.skipTest("no committed templates")
        read, _ = V.read_board(rec, im, geo, True)   # flipped
        self.assertEqual(read.board_fen(), chess.Board().board_fen(),
                         "a mirrored board does not read back as the start position")


class TestFullGame(LoopTest):
    def test_game_runs_to_a_conclusion_and_verifies_every_move(self):
        """A complete game, first-legal-move both sides, no PC.

        The point is not the result: it is that the loop tracks the board through
        ~20 real screenshots, verifies its own moves, and finishes with the local
        board agreeing with what is on the screen.
        """
        reply = ["e7e5", "b8c6", "f8c5", "g8f6", "d7d6", "c8e6", "d8d7", "e8g8"]
        screen = VS.VirtualScreen(opponent_reply=reply, reply_after_looks=1)
        B = fresh_module(screen, STUB)
        log = os.path.join(self.tmp, "g.json")
        r = B.play_game(chess.WHITE, engine_ms=25, max_seconds=60, log_path=log,
                        board_timeout=15, use_region=True)
        ev = [e["event"] for e in r["log"]]

        self.assertIn("start_verified", ev)
        self.assertIn("my_move", ev)
        self.assertNotIn("own_move_not_landed", ev, "the loop thinks a move failed")
        self.assertNotIn("click_failed", ev, "a click was rejected")
        self.assertNotIn("own_move_not_landed", [e for e in ev if e == "own_move_not_landed"] or [],
                         "a move was reported as not landing")
        self.assertGreaterEqual(len(events(r, "my_move")), 5,
                                "the game ended far too early")

        finals = events(r, "final")
        self.assertEqual(len(finals), 1, "no final evidence recorded")
        self.assertTrue(finals[0]["agrees"],
                        f"final local board disagrees with the screen:\n"
                        f"local  {finals[0]['local_fen']}\nscreen {finals[0]['screen_fen']}")
        # every move we made was verified against the screen
        played = events(r, "my_move")
        verified = events(r, "own_move_verified")
        self.assertGreaterEqual(len(verified), max(1, len(played) - 1))

    def test_region_screenshots_are_used_and_are_board_sized(self):
        screen = VS.VirtualScreen(opponent_reply=["e7e5", "b8c6"], reply_after_looks=1)
        B = fresh_module(screen, STUB)
        import boardvision as V
        geo = V.find_board(screen.render())
        _, im, g2 = B.grab_region(geo)
        self.assertEqual(g2["method"], "crop")
        self.assertLess(im.size[0] * im.size[1], 1920 * 1080 / 4,
                        "a region screenshot should be much smaller than the frame")

    def test_black_side_is_played_correctly(self):
        """Playing Black used to learn piece templates in the wrong orientation.

        `rec.learn(..., False)` was hard-coded while lichess mirrors the board for
        the player, so every template was paired with the wrong square. This game,
        played as Black, fails on the previous code and passes here.
        """
        # reply_after_looks=2: White does not move until the bot has taken its
        # opening screenshot, which is what makes this test about the ORIENTATION
        # rather than about adopting a position White has already changed.
        screen = VS.VirtualScreen(our_side=chess.BLACK,
                                  opponent_reply=["e2e4", "d2d4", "g1f3"],
                                  reply_after_looks=2)
        B = fresh_module(screen, STUB)
        log = os.path.join(self.tmp, "gb.json")
        r = B.play_game(chess.BLACK, engine_ms=30, max_seconds=20, log_path=log,
                        board_timeout=15)
        ev = [e["event"] for e in r["log"]]
        self.assertTrue({"start_verified", "start_adopted"} & set(ev),
                        f"could not establish the opening position as Black: {ev[:8]}")
        self.assertIn("my_move", ev)
        first = (events(r, "start_verified") or events(r, "start_adopted"))[0]
        self.assertTrue(first["flipped"], "Black should be learned as a flipped board")
        f = events(r, "final")
        self.assertTrue(f and f[-1]["agrees"],
                        "over the whole game the local board must end up equal to the screen")
        finals = events(r, "final")
        self.assertTrue(finals and finals[-1]["agrees"],
                        "final board disagrees with the screen when playing Black")


class TestRecovery(LoopTest):
    def test_a_dropped_click_is_detected_and_retried(self):
        """The failure the old loop could not see: a click that never registered.

        Before this change the board was updated optimistically, so a missed click
        only surfaced plies later as a resync. Here the virtual screen swallows the
        first move click; the loop must notice, retry, and carry on.
        """
        screen = VS.VirtualScreen(opponent_reply=["e7e5", "b8c6"], reply_after_looks=1)
        B = fresh_module(screen, STUB)
        original = screen.click
        state = {"swallowed": 0}

        def drop_first_move_click(x, y, button="left"):
            # every second click on the board is the destination square
            if screen.selected is not None and state["swallowed"] == 0:
                screen.selected = None
                state["swallowed"] = 1
                return {"ok": True, "exact": True, "landed": [x, y]}
            return original(x, y, button)

        screen.click = drop_first_move_click
        log = os.path.join(self.tmp, "gd.json")
        r = B.play_game(chess.WHITE, engine_ms=30, max_seconds=20, log_path=log,
                        board_timeout=15)
        ev = [e["event"] for e in r["log"]]
        self.assertEqual(state["swallowed"], 1, "the test did not drop a click")
        self.assertIn("own_move_not_landed", ev, "the dropped click went unnoticed")
        self.assertIn("own_move_landed_on_retry", ev, "the loop did not recover by retrying")
        finals = events(r, "final")
        self.assertTrue(finals and finals[-1]["agrees"],
                        "board drifted after the dropped click")

    def test_a_failed_region_read_falls_back_to_the_whole_frame(self):
        """Region reads assume the board has not moved. If they fail, re-locate."""
        screen = VS.VirtualScreen(opponent_reply=["e7e5", "b8c6"], reply_after_looks=1)
        B = fresh_module(screen, STUB)
        calls = {"n": 0}
        real = B.grab_region

        def flaky(geo, label="board", margin=0):
            calls["n"] += 1
            if calls["n"] in (2, 3):          # two region reads fail mid-game
                raise RuntimeError("simulated: window moved")
            return real(geo, label, margin)

        B.grab_region = flaky
        log = os.path.join(self.tmp, "gs.json")
        try:
            r = B.play_game(chess.WHITE, engine_ms=30, max_seconds=25, log_path=log,
                            board_timeout=15)
        finally:
            B.grab_region = real
        ev = [e["event"] for e in r["log"]]
        self.assertIn("my_move", ev, "the loop died when a region read failed")
        self.assertIn("final", ev)
        finals = events(r, "final")
        self.assertTrue(finals[-1]["agrees"],
                        "the board drifted after the region reads failed")


class TestRealEngine(LoopTest):
    def test_one_game_with_the_real_sable_binary(self):
        """Prove the persistent-engine change did not break the real integration."""
        if not os.path.exists(REAL):
            self.skipTest("sable binary not built (run make)")
        # Deliberately short. The virtual screen auto-queens instead of showing
        # lichess's promotion picker, so a game long enough to contain a promotion
        # exercises the fixture's gaps rather than the engine integration. What this
        # test is for is the wiring: one persistent engine process, legal moves,
        # every move verified, final board equal to the screen.
        screen = VS.VirtualScreen(opponent_reply=["e7e5", "b8c6"], reply_after_looks=1)
        B = fresh_module(screen, REAL)
        log = os.path.join(self.tmp, "gr.json")
        r = B.play_game(chess.WHITE, engine_ms=40, max_seconds=9, log_path=log,
                        board_timeout=15)
        ev = [e for e in r["log"] if e.get("event") == "my_move"]
        self.assertGreaterEqual(len(ev), 2, "the real engine produced no moves")
        for e in ev:
            uci = e["move"]
            self.assertRegex(uci, r"^[a-h][1-8][a-h][1-8][qrbn]?$", "not a UCI move")
        verified = events(r, "own_move_verified")
        self.assertGreaterEqual(len(verified), len(ev) - 1,
                                "moves were played without being verified")
        finals = events(r, "final")
        self.assertTrue(finals and finals[-1]["agrees"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
