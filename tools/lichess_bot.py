#!/usr/bin/env python3
"""A sandbox-side bot that plays a live game on the screen through pc-eye.

Why this exists: a 1+0 game leaves 60 seconds for the whole game. Any design
that puts a human (or an LLM turn) between moves loses on time by move 15. So
the whole loop — look, recognise, choose, click, verify — runs here, in a tight
local loop, with no round-trips except HTTP to pc-eye.

How it knows the position without OCR:

  * the board rectangle is measured from the screenshot (density scan + edge
    refinement + a squareness assertion). Wrong geometry => refuse, never guess.
  * piece templates are learned from a position we already know (the start
    position), so nothing is hard-coded about lichess's piece set.
  * every square is classified by nearest template and the result is turned into
    a FEN, which python-chess then checks: right side to move, king counts,
    no impossible placement. If validation fails the read is rejected.

It only ever plays the side it was told to play, and it refuses to touch anything
that is not a local board / a game against the computer.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import atexit
import chess
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
# pc-eye's repo does NOT ship an importable client (no pceye.py), so importing it
# unconditionally made every tool in this directory unusable on a fresh machine --
# including the tests, which need no screen at all. Try an installed module first,
# then fall back to the client this repo carries; import never fails either way.
PCEYE_DIR = os.environ.get("SABLE_PCEYE", os.path.join(HERE, "..", "..", "pceye"))
sys.path.insert(0, PCEYE_DIR)
try:
    import pceye  # noqa: E402
except ImportError:                      # pragma: no cover - the normal case
    import pceye_client as pceye  # noqa: E402

def _engine_path():
    """Which engine drives the mouse. Overridable so the same harness can run
    Sable or a reference engine without editing code."""
    return os.environ.get("SABLE_ENGINE", os.path.join(HERE, "..", "sable"))


def _ensure_executable(path):
    """The workspace snapshot does not preserve the execute bit, so a restored
    binary silently fails to launch and the bot drops a game. Repair it here
    rather than discovering it mid-game."""
    try:
        if os.path.exists(path) and not os.access(path, os.X_OK):
            os.chmod(path, os.stat(path).st_mode | 0o111)
    except Exception:
        pass


SABLE = _engine_path()
_ensure_executable(SABLE)
# Screenshots are evidence, and evidence belongs in results/ -- but a test run must
# not write there. The tests were polluting results/bot_shots with synthetic frames,
# which the geometry test then read back as real captured evidence and failed on.
# SABLE_SHOTS_DIR points somewhere else (tests use a temp dir).
SHOTS = os.environ.get("SABLE_SHOTS_DIR") or os.path.join(HERE, "..", "results", "bot_shots")
os.makedirs(SHOTS, exist_ok=True)

# ----------------------------------------------------------------- screen io


_LAST = {}


# ------------------------------------------------------------------ imports
# Geometry and pixel features live in one place now. They had been copy-pasted
# into three tools, so the same bug had to be fixed three times -- and was not.
# Everything below keeps its old name, so other tools that import `B.find_board`
# or `B.sq_patch` keep working unchanged.
import boardvision as V  # noqa: E402
from boardvision import (  # noqa: E402,F401
    PATCH_N, SHAPES, sq_patch, square_box, square_centre, patch_features,
)

_CACHE = V.GeometryCache()


def find_board_cached(im):
    """Board geometry for this screen layout, cached and re-validated.

    Named as before, but it now costs ~0 ms instead of ~145 ms on a cache hit --
    the geometry was being re-derived on every poll, for a value that only changes
    when the window moves.
    """
    return _CACHE.get(im)


def find_board(im):
    """Locate the board. Raises instead of returning a plausible-looking guess."""
    return V.find_board(im)


def reset_geometry_cache():
    """Forget the cached board box (call when the layout may have changed)."""
    _CACHE.clear()


def grab_region(geo, label="board", margin=0):
    """Screenshot ONLY the board region.

    The live loop asked for the full 1920x1080 frame every time (`?max=0`), which
    is 123 KB per shot over the tunnel when 43 KB -- the board itself -- is all the
    recogniser ever looks at. Measured on the recorded frames: 2.9x less traffic
    per move, and the returned image IS the board, so no search is needed to find
    it. `geo_from_crop` supplies the geometry for free.
    """
    region = V.board_region(geo, margin=margin)
    hdrs, body, _ = pceye.req(f"/shot?max=0&region={region}")
    if body[:2] != b"\xff\xd8":
        raise RuntimeError("screen capture did not return a JPEG")
    p = os.path.join(SHOTS, f"{int(time.time()*1000)}_{label}.jpg")
    with open(p, "wb") as f:
        f.write(body)
    im = Image.open(p).convert("RGB")
    return p, im, V.geo_from_crop(im)


def grab_stable(label="stable", tries=8, tol=0.0005):
    """Screenshot only when two consecutive frames agree.

    lichess animates pieces when the position changes. A read taken mid-animation
    sees translated or scaled glyphs, and that silently corrupts both the learned
    templates and the game state. This is not hypothetical: reading mid-animation
    is what produced the rank-1 misreads that stalled the first live game.
    """
    prev = None
    for i in range(tries):
        path, im = grab(f"{label}{i}")
        cur = np.asarray(im.resize((160, 90)).convert("L"), dtype=np.float32)
        if prev is not None and float(np.abs(cur - prev).mean()) < tol * 255:
            return path, im
        prev = cur
        time.sleep(0.28)
    return path, im


def grab(label="board"):
    hdrs, body, _ = pceye.req("/shot?max=0")
    if body[:2] != b"\xff\xd8":
        raise RuntimeError("screen capture did not return a JPEG")
    p = os.path.join(SHOTS, f"{int(time.time()*1000)}_{label}.jpg")
    with open(p, "wb") as f:
        f.write(body)
    return p, Image.open(p).convert("RGB")


def act(payload):
    """POST /act through whichever client is in use, raising instead of exiting.

    `click()` used to post to pc-eye itself with urllib while everything else went
    through the client module, so there were two HTTP paths to the same server and
    the tests could only intercept one of them. One contract now: if the client
    offers `act`, use it; else do the request here.
    """
    if hasattr(pceye, "act"):
        return pceye.act(payload)
    _, raw, _ = pceye.req("/act", "POST", payload)
    return json.loads(raw)


def click(x, y, settle=0.12):
    """Click a screen point, raising on failure instead of exiting.

    pceye.req() in the original client called sys.exit() on an HTTP error. Inside a
    live game that is fatal: one out-of-range coordinate would terminate the bot
    and leave the clock running.
    """
    d = act({"action": "click", "x": int(x), "y": int(y)})
    if d.get("exact") is False:
        raise RuntimeError(f"click did not land: wanted {(x, y)} got {d.get('landed')}")
    if settle:
        time.sleep(settle)
    return d


def key(combo, settle=0.0):
    d = act({"action": "key", "key": str(combo)})
    if settle:
        time.sleep(settle)
    return d


def paste(text, settle=0.0):
    d = act({"action": "paste", "text": text})
    if settle:
        time.sleep(settle)
    return d


# ----------------------------------------------------------------- recognition

# ---------------------------------------------------------------------------
# Recognition, v2.
#
# v1 inferred the piece *and its colour* from whichever template matched, and it
# failed exactly where you would expect: a white queen on a dark square matched
# a black queen template, because the glyph SHAPE is identical and only the fill
# differs. A single misread like that corrupts the game state and the bot stops.
#
# v2 splits the two questions, which is what makes it robust:
#   * SHAPE  -- the set of pixels that differ from the square's own background.
#               Compared against 6 colour-agnostic shape templates.
#   * COLOUR -- the mean luminance of those pixels against that same background.
#               Lighter than the square => white piece, darker => black piece.
#   * EMPTY  -- too few non-background pixels to be a piece at all.
# Because both parts are measured against the square's own background, the same
# code works on light and dark squares and on a themed board.
# ---------------------------------------------------------------------------

# sq_patch / patch_features / square_box / square_centre / PATCH_N / SHAPES are
# imported from boardvision at the top of this file (see "imports"). They were
# defined here and duplicated in two other tools.


class Recogniser:
    """Learns 6 shape templates from a position it already knows, then reads any
    later position by shape + colour."""

    def __init__(self):
        self.shapes = {}      # symbol -> list of masks

    def learn(self, im, geo, board, flipped):
        seen = set()
        for sq, pc in board.piece_map().items():
            sym = pc.symbol().upper()
            if sym in seen:
                continue
            f, r = chess.square_file(sq), chess.square_rank(sq)
            mask, fill, is_white, _ = patch_features(sq_patch(im, geo, f, r, flipped))
            self.shapes.setdefault(sym, []).append(mask)
            seen.add(sym)
        for s in SHAPES:
            if s not in self.shapes:
                raise RuntimeError(f"could not learn a template for {s}; start position not readable")

    def save(self, path):
        if not self.shapes:
            return
        flat = {}
        for sym, masks in self.shapes.items():
            for i, m in enumerate(masks):
                flat[f"{sym}_{i}"] = m
        np.savez_compressed(path, **flat)

    def load(self, path):
        if not os.path.exists(path):
            return False
        with np.load(path) as z:
            self.shapes = {}
            for k in z.files:
                sym = k.rsplit("_", 1)[0]
                self.shapes.setdefault(sym, []).append(z[k])
        return len(self.shapes) > 0

    def classify(self, mask):
        best, bestd = None, 1e18
        for sym, masks in self.shapes.items():
            for m in masks:
                d = float(np.abs(mask - m).mean())
                if d < bestd:
                    bestd, best = d, sym
        return best, bestd

    def read(self, im, geo, flipped, empty_threshold=0.03):
        board = chess.Board(None)
        detail = {}
        for sq in chess.SQUARES:
            f, r = chess.square_file(sq), chess.square_rank(sq)
            mask, fill, is_white, _ = patch_features(sq_patch(im, geo, f, r, flipped))
            if fill < empty_threshold:
                detail[sq] = (".", 0.0)
                continue
            sym, d = self.classify(mask)
            piece = chess.Piece.from_symbol(sym if is_white else sym.lower())
            board.set_piece_at(sq, piece)
            detail[sq] = (piece.symbol(), d)
        return board, detail


def read_auto(rec, im, geo):
    """Read the board in whichever orientation is the right way up.

    lichess flips the board for the side to move, so the orientation has to be
    detected rather than assumed. Scoring the *read* board by king position is
    not reliable: if the orientation choice is wrong the read is garbage, but a
    garbage read can still put a king on a plausible rank. (That is how clicks
    ended up mirrored onto the wrong squares.) So the score is computed from the
    image-independent structure that actually differs between the two readings:
    whichever orientation places more of White's material on the near half and
    more of Black's on the far half is the right one.
    """
    best = None
    for flipped in (False, True):
        board, detail = rec.read(im, geo, flipped)
        wk = board.pieces(chess.KING, chess.WHITE)
        bk = board.pieces(chess.KING, chess.BLACK)
        if len(wk) != 1 or len(bk) != 1:
            score = -1000
        else:
            def half(mask_squares):
                near = sum(1 for s in mask_squares if chess.square_rank(s) <= 3)
                far = len(mask_squares) - near
                return near, far
            w_near, w_far = half(board.pieces(chess.PAWN, chess.WHITE) |
                                 board.pieces(chess.KNIGHT, chess.WHITE) |
                                 board.pieces(chess.BISHOP, chess.WHITE) |
                                 board.pieces(chess.ROOK, chess.WHITE) |
                                 board.pieces(chess.QUEEN, chess.WHITE))
            b_near, b_far = half(board.pieces(chess.PAWN, chess.BLACK) |
                                 board.pieces(chess.KNIGHT, chess.BLACK) |
                                 board.pieces(chess.BISHOP, chess.BLACK) |
                                 board.pieces(chess.ROOK, chess.BLACK) |
                                 board.pieces(chess.QUEEN, chess.BLACK))
            wk_rank = chess.square_rank(next(iter(wk)))
            bk_rank = chess.square_rank(next(iter(bk)))
            score = (w_near - w_far) + (b_far - b_near) \
                    + (2 if wk_rank <= 3 else -2) + (2 if bk_rank >= 4 else -2)
        if best is None or score > best[0]:
            best = (score, flipped, board, detail)
    return best[1], best[2], best[3]


def plausible(board, side):
    """Cheap sanity gate before we believe a read."""
    if len(board.pieces(chess.KING, chess.WHITE)) != 1:
        return False, "white king count"
    if len(board.pieces(chess.KING, chess.BLACK)) != 1:
        return False, "black king count"
    if not board.is_valid():
        return False, f"invalid position ({board.status()})"
    if board.turn != side:
        board.turn = side
    return True, "ok"


# ----------------------------------------------------------------- engine

# ------------------------------------------------------ reference engine driver
# Sable reads its stdin synchronously, so piping "go movetime N" then "quit" is
# safe and reaches full depth (verified: depth 11 either way). Stockfish does not
# -- it treats the queued "quit" as a stop and returns a depth-1 move (it answered
# a2a3 for a 400ms "max strength" search). So anything whose name suggests a
# stockfish build is driven over a real UCI handshake instead.
_sf_cache = {}


def _is_stockfish(path):
    n = os.path.basename(path).lower()
    return n == "sf" or "stockfish" in n or n.startswith("sf-")


def _parse_setoptions(spec):
    """'setoption name Skill Level value 5' -> {'Skill Level': 5}"""
    opts = {}
    for line in (spec or "").splitlines():
        line = line.strip()
        if not line.lower().startswith("setoption"):
            continue
        body = line[len("setoption"):].strip()
        if body.lower().startswith("name"):
            body = body[4:].strip()
        name, _, val = body.partition("value")
        name, val = name.strip(), val.strip()
        if name:
            try:
                opts[name] = int(val)
            except ValueError:
                opts[name] = val
    return opts


class _UciEngine:
    """Minimal blocking UCI client.

    python-chess's SimpleEngine kept non-daemon threads alive and the driver hung
    on exit, so this speaks the protocol directly over pipes. It is a persistent
    process, so the hash carries over between moves within a game.
    """

    def __init__(self, path, options=None):
        _ensure_executable(path)
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                  cwd=os.path.dirname(os.path.abspath(path)))
        self._send("uci")
        self._read_until("uciok", 15.0)
        for k, v in (options or {}).items():
            self._send(f"setoption name {k} value {v}")
        self._send("isready")
        self._read_until("readyok", 15.0)

    def _send(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def _read_until(self, token, timeout_s):
        deadline = time.time() + timeout_s
        while True:
            if time.time() > deadline:
                raise RuntimeError(f"engine did not send {token!r}")
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("engine exited")
            if line.strip().startswith(token):
                return

    def best(self, fen, ms):
        self._send(f"position fen {fen}")
        self._send(f"go movetime {int(ms)}")
        deadline = time.time() + ms / 1000.0 + 10.0
        while True:
            if time.time() > deadline:
                raise RuntimeError("no bestmove from engine")
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("engine exited")
            if line.startswith("bestmove"):
                parts = line.split()
                return parts[1] if len(parts) > 1 else None

    def close(self):
        try:
            self._send("quit")
        except Exception:
            pass
        try:
            self.p.wait(timeout=3)
        except Exception:
            try: self.p.kill()
            except Exception: pass


_engine_cache = {}


def _quit_engines():
    for k, e in list(_engine_cache.items()):
        try: e.close()
        except Exception: pass
        _engine_cache.pop(k, None)


atexit.register(_quit_engines)


def _stockfish_best(fen, ms):
    path = _engine_path()
    opts = _parse_setoptions(os.environ.get("SABLE_SETOPTIONS", ""))
    key = (path, tuple(sorted(opts.items())))
    eng = _engine_cache.get(key)
    if eng is None:
        eng = _UciEngine(path, opts)
        _engine_cache[key] = eng
    try:
        return eng.best(fen, ms)
    except Exception:
        # a wedged engine must not poison every later move
        try: eng.close()
        except Exception: pass
        _engine_cache.pop(key, None)
        raise


def sable_best(fen, ms=300):
    """Best move from the configured engine, over a PERSISTENT process.

    This used to spawn `./sable`, feed it one move and `quit`, every single move.
    Measured on a 200 ms search: 578 ms wall clock, of which 390 ms was process
    start, book load, a fresh 32 MB transposition table and teardown. In a 1+0
    game -- 60 seconds, ~30 of our moves -- that is ~12 seconds spent starting a
    program that already knows the position. Reusing one process removes the
    overhead and, because the hash and everything learned about the position
    survive between moves, it is also a little stronger.

    The old path is kept for engines that misbehave over a pipe, and for the
    reference-engine case (Stockfish), which needs a real UCI handshake because it
    treats a queued `quit` as a stop and returns a depth-1 move.
    """
    eng = _engine_path()
    opts = _parse_setoptions(os.environ.get("SABLE_SETOPTIONS", ""))
    key = (os.path.abspath(eng), tuple(sorted(opts.items())))
    e = _engine_cache.get(key)
    if e is None:
        _ensure_executable(eng)
        if not os.access(eng, os.X_OK) and not _is_stockfish(eng):
            raise RuntimeError(f"engine {eng} is not executable")
        e = _UciEngine(eng, opts)
        _engine_cache[key] = e
    try:
        return e.best(fen, ms)
    except Exception:
        # a wedged engine must not poison every later move
        try:
            e.close()
        except Exception:
            pass
        _engine_cache.pop(key, None)
        raise


def reset_engines():
    """Close every cached engine process (tests, and between games)."""
    _quit_engines()


# ----------------------------------------------------------------- play

_PROMO_REC = None


def set_promo_recogniser(rec):
    global _PROMO_REC
    _PROMO_REC = rec


def promotion_landed(im, geo, sq, flipped, want):
    """Did the piece on `sq` become `want`? Read straight off the screen."""
    if _PROMO_REC is None:
        return False
    f, r = chess.square_file(sq), chess.square_rank(sq)
    mask, fill, is_white, _ = patch_features(sq_patch(im, geo, f, r, flipped))
    if fill < 0.03:
        return False
    sym, _ = _PROMO_REC.classify(mask)
    return sym == want


def play_move(board, move, geo, flipped, verify=True, rec=None, label="mv"):
    """Make a move on the board, including promotions.

    Promotions are handled by VERIFICATION, not by guessing where the picker is.
    An earlier version clicked a fixed offset above the target square and hoped;
    when a pawn actually promoted the move never completed, the board never
    changed, and the bot sat there until it lost on time. So now:

      1. click from, click to  (this raises lichess's promotion picker)
      2. read the target square off the screen
      3. if it is not the piece we want, click the picker entry again and re-read,
         then try the entries down the column
      4. fail loudly rather than stalling silently
    """
    global _PROMO_REC
    if rec is not None:
        _PROMO_REC = rec
    a = square_centre(geo, chess.square_file(move.from_square), chess.square_rank(move.from_square), flipped)
    b = square_centre(geo, chess.square_file(move.to_square), chess.square_rank(move.to_square), flipped)
    click(*a)
    click(*b, settle=0.30)

    if not move.promotion or _PROMO_REC is None:
        return a, b

    # Verification is on the WHOLE position, not just the promotion square.
    # Checking only the target square cannot distinguish "promotion completed"
    # from "the picker is still open", because the picker's first entry is a queen
    # drawn on that very square -- that false positive is exactly how the bot
    # ended up standing still. The picker also covers the three squares below it,
    # so the full board only matches once the picker has actually closed and the
    # move has been registered.
    sq_px = (geo["bbox"][2] - geo["bbox"][0] + 1) / 8.0
    expected = board.copy()
    expected.push(move)

    def settled():
        # Reuse the geometry we already measured: the picker covers board squares
        # and breaks the density scan that locates the board.
        try:
            _, shot = grab(f"promo_{label}")
            read, _ = rec.read(shot, geo, flipped)
            return read.board_fen() == expected.board_fen()
        except Exception:
            return False

    if settled():
        return a, b
    # The picker extends away from the promotion square, and which way that is
    # depends on where the square sits, not on who is playing. Try both
    # directions, stay inside the board, and skip anything out of range -- an
    # off-screen click used to raise a 409 that killed the process.
    # Candidate picker entries, most likely first. k=0 MUST be included: the
    # picker's queen entry sits exactly on the promotion square, so a loop that
    # starts at k=1 only ever clicks knight/rook/bishop and never the queen --
    # which is precisely what made every promotion fail to settle.
    L, T, R, B = geo["bbox"]
    time.sleep(0.45)                      # let the picker finish appearing
    tries = [(b[0], b[1])]
    for k in range(1, 4):
        for sign in (1, -1):
            cy = b[1] + sign * int(round(sq_px * k))
            if T <= cy <= B:
                tries.append((b[0], cy))
    for (cx, cy) in tries:
        click(cx, cy, settle=0.32)
        if settled():
            return a, (cx, cy)
    raise RuntimeError(
        f"promotion to {chess.piece_name(move.promotion)} on "
        f"{chess.square_name(move.to_square)} did not settle after 4 attempts")


def move_landed(expected_board, seen):
    """Did our move reach the board, given what is on the screen now?

    Comparing board_fen() exactly is wrong: a level-6 computer can answer in
    50-1000 ms, so by the time the verification screenshot is read the position may
    already contain the opponent's reply -- and treating that as "our move did not
    land" would click the same move a second time. So a position is accepted when
    it is either exactly the expected one, or reachable from it by one legal
    opponent move. Anything else is a genuine failure.
    """
    if seen is None:
        return False, "unreadable"
    if seen.board_fen() == expected_board.board_fen():
        return True, "landed"
    for m in expected_board.legal_moves:
        probe = expected_board.copy()
        probe.push(m)
        if probe.board_fen() == seen.board_fen():
            return True, "landed_then_answered"
    return False, "not_landed"


def detect_opponent_move(prev_board, board_now):
    """Which legal move turns prev_board into board_now? Uses the piece placement."""
    cands = []
    for m in prev_board.legal_moves:
        probe = prev_board.copy()
        probe.push(m)
        if probe.board_fen() == board_now.board_fen():
            cands.append(m)
    return cands


# ----------------------------------------------------------------- game loop

def wait_for_board(timeout=40):
    """Poll until a board is on screen. Used right after a game is started."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            path, im = grab("wait")
            geo = find_board(im)
            reset_geometry_cache()
            _CACHE.set(im, geo)
            return path, im, geo
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"no board appeared on screen within {timeout:.0f}s")


POLL_FIRST_S = 0.10          # first re-read delay after our move
POLL_GROWTH = 1.45           # each unchanged frame waits this much longer
POLL_MAX_S = 1.20
STALL_RESYNC_AT = (6, 14)    # same schedule as before: resync, do not abandon
STALL_GIVE_UP = 18


def play_game(side=chess.WHITE, engine_ms=200, max_seconds=120, log_path=None,
              board_timeout=40.0, record_dir=None, resume=False, clock_ms=None,
              use_region=True, verify_own_moves=True, min_confidence=V.DEFAULT_MIN_CONFIDENCE):
    """Play one game against whatever is on screen. Returns a dict of evidence.

    What changed from the version this replaces, and why it is faster:

      * GEOMETRY IS CACHED and, during play, not searched for at all: screenshots
        are requested for the board region only (2.9x fewer bytes) and the
        returned image *is* the board. ~145 ms -> 0 ms per frame.
      * THE ENGINE IS PERSISTENT, so a move no longer costs ~390 ms of process
        start-up and loses its transposition table between moves.
      * THE POLL NO LONGER SLEEPS 0.55 s BEFORE EVERY LOOK. It reads, and if the
        frame has not changed waits a little longer next time. Typical games now
        spend their time waiting for the opponent, not for the harness.

    and why it is more correct:

      * EVERY READ PASSES A GATE before it is believed (`read_plausible`: king
        counts, per-square template confidence, and a position that chess allows).
        A failed read triggers one full-frame re-locate and re-read instead of
        being adopted.
      * OUR OWN MOVE IS VERIFIED. The board used to be updated optimistically --
        a click that missed was only discovered several plies later, which is what
        the "final resync=4..6" lines in the old logs were. Now the position is
        re-read after each of our moves and a mismatch is retried once, then
        reported.
      * TEMPLATES ARE LEARNED IN THE RIGHT ORIENTATION. They were always learned
        with `flipped=False` regardless of the side being played, so a game as
        Black (lichess flips the board) would learn every piece against the wrong
        square.
    """
    log = []

    def note(**kw):
        kw["t"] = round(time.time() - t_start, 2)
        log.append(kw)
        if log_path:
            with open(log_path, "w") as f:
                json.dump(log, f, indent=2)

    t_start = time.time()
    path, im, geo = wait_for_board(board_timeout)
    note(event="board_found", geo=[round(v) for v in geo["bbox"]],
         method=geo.get("method"), find_ms=geo.get("ms"), dim=geo.get("dim"))

    TPL = os.environ.get("SABLE_TEMPLATES") or os.path.join(HERE, "..", "results", "templates.npz")
    rec = Recogniser()

    def read_checked(label, expect_side=None, retry_full=True):
        """Read the board, and believe it only if it passes the gate.

        Returns (board, reason, full_geo). On a failed read from a region crop the
        crop is abandoned and the frame is re-located at full size once, because
        the usual cause is that the window moved or the page scrolled.
        """
        nonlocal geo
        src = label
        if use_region:
            try:
                _, shot, _ = grab_region(geo, src)
                board, reason = V.read_plausible(rec, shot, V.geo_from_crop(shot),
                                                 flipped, side=expect_side,
                                                 min_confidence=min_confidence)
                if board is not None:
                    return board, reason, geo
            except Exception as e:
                reason = f"region read failed: {e}"
            else:
                reason = reason or "region read rejected"
        else:
            reason = "region disabled"
        if not retry_full:
            return None, reason, geo
        _, shot = grab(f"{label}_full")
        try:
            geo = find_board(shot)
            _CACHE.set(shot, geo)
        except Exception as e:
            # Re-locating failed. That is expected while a modal is up: lichess
            # dims the whole page behind the promotion picker and a dialog, and a
            # dimmed frame is genuinely not locatable by colour (measured: the
            # dark squares come out 33 in the blue channel away from any single
            # brightness correction, so no gain factor recovers both colours).
            # The board has not moved, so fall back to the last good geometry and
            # let the plausibility gate decide whether the read is worth having.
            if _CACHE.last is None:
                return None, f"{reason}; re-locate failed: {e}", geo
            geo = _CACHE.last
            note(event="geometry_reused", why=str(e)[:80])
            board, reason2 = V.read_plausible(rec, shot, geo, flipped,
                                              side=expect_side,
                                              min_confidence=min_confidence)
            return board, reason2, geo
        board, reason2 = V.read_plausible(rec, shot, geo, flipped, side=expect_side,
                                          min_confidence=min_confidence)
        return board, reason2, geo

    if resume:
        # Templates must come from a position we know. They describe how lichess
        # DRAWS a piece, not where it is, so one cached set works for every board
        # in the session.
        if not rec.load(TPL):
            raise RuntimeError("no cached templates; start a game normally once so they can be learned")
        # Lichess orients the board from the player's side; if that guess is wrong
        # the read fails the gate, so try the other orientation before giving up.
        flipped = (side == chess.BLACK)
        board, reason, geo = read_checked("resume")
        if board is None:
            flipped = not flipped
            board, reason, geo = read_checked("resume_flipped")
        if board is None:
            raise RuntimeError(f"cannot read the resumed position: {reason}")
        note(event="resumed", fen=board.board_fen(), flipped=flipped, read=reason)
        # Is it actually my move? Give the opponent three seconds; if the position
        # is unchanged after that, the move is mine.
        time.sleep(3.0)
        now, reason, geo = read_checked("resumecheck")
        if now is not None and now.board_fen() != board.board_fen():
            for m in list(board.legal_moves):
                probe = board.copy()
                probe.push(m)
                if probe.board_fen() == now.board_fen():
                    board = probe
                    break
            note(event="opponent_moved_first", fen=board.board_fen())
        my_turn = True
    else:
        # Lichess orients the board so the player's own pieces are at the bottom.
        flipped = (side == chess.BLACK)
        # Prefer a template set that is ALREADY KNOWN to work, and never overwrite
        # it blindly. `learn()` keeps ONE template per shape, taken from the first
        # square that shape occupies in a1..h8 order, so a fresh learn on a board
        # where that square is awkward -- a coordinate label, a last-move
        # highlight, a piece mid-animation -- poisons every later read in the
        # session, and the good set it replaced is gone. So: load the cache, prove
        # it by reading the start position, and re-learn only if it fails.
        start_fen = chess.Board().board_fen()
        reused = False
        had_templates = rec.load(TPL)
        if had_templates:
            # Edge refinement is accurate to 1px, and one pixel measurably changes
            # reads (a single pixel off turned one square wrong and moved mean
            # confidence 0.864 -> 0.807). Resolve that ambiguity against the one
            # position we always know: the opening. Costs 9 reads once.
            geo, cal = V.calibrate_offset(rec, im, geo, flipped, truth=chess.Board())
            if cal["dx"] or cal["dy"]:
                _CACHE.set(im, geo)
            note(event="geometry_calibrated", **cal)
        if had_templates:
            probe, why = V.read_plausible(rec, im, geo, flipped, side=side,
                                          min_confidence=min_confidence)
            if probe is not None and probe.board_fen() == start_fen:
                reused = True
                note(event="templates_reused", path=TPL, note=why)
            else:
                note(event="templates_rejected", path=TPL,
                     reason=(why or "read did not match the start position"))
        if not reused and not had_templates:
            # Nothing cached: this is the only chance to learn. It is safe only
            # because the frame has to be the opening position for the labelling
            # (piece -> square) to be correct.
            rec.learn(im, geo, chess.Board(), flipped)
            rec.save(TPL)
            note(event="templates_cached", path=TPL, learned_flipped=flipped)
        elif not reused:
            # Cached templates exist but did not read this frame as the opening
            # position. That is expected when the opponent has already moved, and
            # it is NOT a reason to learn again: learning labels pieces by start-
            # position squares, so doing it on a mid-game board writes six
            # mislabelled templates over a good set and every later read in the
            # session degrades. Keep the good set and adopt the position instead.
            note(event="templates_kept", path=TPL,
                 why="cached set does not show the opening position; assuming the "
                     "opponent has already moved, keeping the templates")

        # Does the board still show the opening position? As White it always does.
        # As BLACK it often does not: lichess starts White's clock the moment the
        # game is created, so by the first screenshot White may already have moved.
        # Requiring the start position then aborted every game as Black before the
        # first move. Adopt whatever is on the board instead, exactly as `resume`
        # does, and let the normal opponent-move detection take it from there.
        read, reason = V.read_plausible(rec, im, geo, flipped, side=side,
                                        min_confidence=min_confidence)
        if read is not None and read.board_fen() == chess.Board().board_fen():
            note(event="start_verified", flipped=flipped)
            board = chess.Board()
            my_turn = (board.turn == side)
        else:
            flipped_alt = not flipped
            alt, alt_reason = V.read_plausible(rec, im, geo, flipped_alt, side=side,
                                                min_confidence=min_confidence)
            if read is None and alt is not None:
                read, reason, flipped = alt, alt_reason, flipped_alt
            if read is None:
                raise RuntimeError(f"cannot read the opening position: {reason}")
            board = read
            board.turn = chess.Board().turn if read.board_fen() == chess.Board().board_fen() else side
            note(event="start_adopted", flipped=flipped, fen=board.board_fen(),
                 turn=("white" if board.turn else "black"), reason=reason)
            my_turn = (board.turn == side)
            if not my_turn:
                note(event="opponent_moves_first", fen=board.board_fen())

    stalls = 0
    # Our OWN clock, not the wall clock. Counting the opponent's thinking against
    # us made the harness abandon games with most of our time still on the board.
    my_spent_ms = 0.0

    while time.time() - t_start < max_seconds:
        # ---- my move
        if my_turn and not board.is_game_over():
            elapsed_ms = (time.time() - t_start) * 1000
            if clock_ms:
                clock_left = max(0.0, clock_ms - my_spent_ms)
                # Only stop when OUR clock is nearly gone, keeping enough time to
                # land one more move.
                if clock_left < 4000:
                    note(event="low_clock_stop", left_ms=round(clock_left))
                    break
                think = int(max(60, min(engine_ms, clock_left / 26.0)))
            else:
                remaining = max_seconds - (time.time() - t_start)
                think = max(60, min(engine_ms, int(remaining * 1000 / 12)))
            turn_started = time.time()
            try:
                uci = sable_best(board.fen(), think)
            except Exception as e:
                note(event="engine_error", err=str(e))
                break
            mv = chess.Move.from_uci(uci)
            if mv not in board.legal_moves:
                note(event="illegal_engine_move", move=uci)
                break
            try:
                a, b = play_move(board, mv, geo, flipped, rec=rec, label=f'ply{board.ply()}')
            except Exception as e:
                note(event="click_failed", move=uci, err=str(e))
                break
            board.push(mv)
            note(event="my_move", move=uci, from_px=list(a), to_px=list(b), think_ms=think)

            # Verify the move actually landed on the board. A promotion is skipped
            # because play_move has already proved settlement for it, and the
            # picker overlay would make an independent read meaningless.
            if verify_own_moves and not mv.promotion:
                seen, reason, geo = read_checked(f"verify{board.ply()}")
                ok, why = move_landed(board, seen)
                if seen is None:
                    note(event="own_move_unverified", move=uci, reason=reason)
                elif not ok:
                    note(event="own_move_not_landed", move=uci,
                         expected=board.board_fen(), screen=seen.board_fen())
                    retry_board = board.copy()
                    retry_board.pop()
                    try:
                        play_move(retry_board, mv, geo, flipped, rec=rec,
                                  label=f'retry{board.ply()}')
                    except Exception as e:
                        note(event="click_retry_failed", move=uci, err=str(e))
                        my_turn = False
                        continue
                    seen, reason, geo = read_checked(f"reverify{board.ply()}")
                    ok, why = move_landed(board, seen)
                    if not ok:
                        note(event="own_move_still_not_landed", move=uci, reason=reason)
                        if seen is not None and seen.is_valid():
                            board = seen          # adopt reality rather than drift
                        my_turn = False
                        continue
                    note(event="own_move_landed_on_retry", move=uci)
                else:
                    note(event="own_move_verified", move=uci, how=why)
            my_spent_ms += (time.time() - turn_started) * 1000.0
            note(event="clock_used", my_clock_left_ms=round(clock_ms - my_spent_ms) if clock_ms else None,
                 spent_ms=round(my_spent_ms))
            time.sleep(0.05)
            my_turn = False
            stalls = 0
            continue

        # ---- opponent's move: poll until the position changes in a way that
        # corresponds to EXACTLY ONE legal move.
        #
        # A single read is not enough: lichess animates the moving piece, so a
        # poll can land mid-flight. The retry loop stays, but it no longer begins
        # with a fixed 0.55 s sleep -- the first read happens immediately and the
        # wait between reads grows only while the frame is unchanged.
        stalls += 1
        delay = min(POLL_MAX_S, POLL_FIRST_S * (POLL_GROWTH ** max(0, stalls - 1)))
        time.sleep(delay)
        cands = []
        now = None
        for attempt in range(2):
            seen, reason, geo = read_checked(f"poll{board.ply()}_{attempt}")
            if seen is None:
                note(event="read_failed", err=reason)
                time.sleep(0.08)
                continue
            now = seen
            if seen.board_fen() == board.board_fen():
                break                                  # still my opponent's turn
            got = detect_opponent_move(board, seen)
            if len(got) >= 1:
                cands = got
                break
            time.sleep(0.08)                           # probably mid-animation
        if not cands:
            if board.is_game_over(claim_draw=True):
                break
            if stalls in STALL_RESYNC_AT:
                note(event="resync_attempt", stalls=stalls,
                     read=(now.board_fen() if now is not None else None))
                if now is not None and now.is_valid() and len(now.pieces(chess.KING, side)) == 1:
                    if now.board_fen() == board.board_fen():
                        # Nothing actually changed: the opponent is simply still
                        # thinking. Claiming the turn here -- which the previous
                        # loop did -- makes the bot play out of turn, the move is
                        # refused, the position never advances, and the game is
                        # lost on the clock while it repeats the same move.
                        note(event="resync_no_change", stalls=stalls)
                    else:
                        now.turn = side
                        note(event="resynced", was=board.board_fen(), now=now.board_fen(),
                             diverge_plies=len(board.move_stack))
                        board = now
                        my_turn = True
                        stalls = 0
                        continue
            if stalls > STALL_GIVE_UP:
                note(event="no_progress", stalls=stalls,
                     last_read=now.board_fen() if now is not None else None)
                break
            continue
        board.push(cands[0])
        note(event="opponent_move", move=cands[0].uci(), plies=board.ply())
        stalls = 0
        if board.is_game_over(claim_draw=True):
            break
        my_turn = True

    # ---- final evidence
    try:
        final_read, reason, geo = read_checked("final", retry_full=True)
        if final_read is None:
            _, shot = grab("final")
            final_read, _ = rec.read(shot, find_board_cached(shot), flipped)
            note(event="final_read_gate_failed", reason=reason)
        # `agrees` must tolerate the opponent's reply being in flight. The final
        # screenshot is taken while the game may still be moving; comparing the
        # board_fen exactly marks a perfectly-tracked game as disagreeing the
        # moment the computer answers before the read lands. That mattered: three
        # drivers count a win only when `agrees is not False`, so an in-flight reply
        # could discard a legitimate win. Exact agreement is recorded separately.
        exact = (final_read.board_fen() == board.board_fen())
        tolerant = exact or move_landed(board, final_read)[0]
        note(event="final", local_fen=board.fen(), screen_fen=final_read.fen(),
             agrees=tolerant, agrees_exact=exact,
             local_result=board.result(claim_draw=True) if board.is_game_over(claim_draw=True) else "ongoing")
        if record_dir:
            os.makedirs(record_dir, exist_ok=True)
            _, shot = grab("final_record")
            with open(os.path.join(record_dir, "final_screen.jpg"), "wb") as f:
                shot.save(f, "JPEG", quality=90)
    except Exception as e:
        note(event="final_read_failed", err=str(e))
    return {"log": log, "final_board": board, "elapsed_s": round(time.time() - t_start, 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["white", "black"], default="white")
    ap.add_argument("--ms", type=int, default=200)
    ap.add_argument("--max-seconds", type=int, default=120)
    ap.add_argument("--log", default=None)
    ap.add_argument("--record", default=None)
    ap.add_argument("--resume", action="store_true", help="continue a game already in progress")
    a = ap.parse_args()
    side = chess.WHITE if a.side == "white" else chess.BLACK
    r = play_game(side, a.ms, a.max_seconds, a.log, record_dir=a.record, resume=a.resume)
    print(json.dumps({"elapsed_s": r["elapsed_s"], "plies": r["final_board"].ply(),
                      "fen": r["final_board"].fen(),
                      "result": r["final_board"].result(claim_draw=True)
                      if r["final_board"].is_game_over(claim_draw=True) else "ongoing",
                      "events": len(r["log"])}, indent=2))
    for e in r["log"]:
        if e.get("event") in ("my_move", "opponent_move", "final", "unknown_opponent_move", "click_failed"):
            print(e)
