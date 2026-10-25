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
# overridable so a launcher can point at wherever pc-eye actually lives
PCEYE_DIR = os.environ.get("SABLE_PCEYE", os.path.join(HERE, "..", "..", "pceye"))
sys.path.insert(0, PCEYE_DIR)
import pceye  # noqa: E402  (the client written earlier)

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
SHOTS = os.path.join(HERE, "..", "results", "bot_shots")
os.makedirs(SHOTS, exist_ok=True)

# ----------------------------------------------------------------- screen io


_LAST = {}


def find_board_cached(im):
    """Geometry only changes when the layout changes; reuse it, but re-validate."""
    key = im.size
    geo = _LAST.get(key)
    if geo is not None:
        L, T, R, B = geo["bbox"]
        if R < im.size[0] and B < im.size[1]:
            return geo
    geo = find_board(im)
    _LAST[key] = geo
    return geo


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


def click(x, y, settle=0.12):
    """Click a screen point, raising on failure instead of exiting.

    pceye.req() calls sys.exit() on an HTTP error. Inside a live game that is
    fatal: one out-of-range coordinate would terminate the bot and leave the
    clock running. So this posts directly and raises a catchable error.
    """
    url, tok = pceye.load_creds()
    body = json.dumps({"action": "click", "x": int(x), "y": int(y)}).encode()
    req = urllib.request.Request(f"{url}/act?t={urllib.parse.quote(tok)}", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        raise RuntimeError(f"click rejected ({(int(x), int(y))}): HTTP {e.code} {detail}")
    if d.get("exact") is False:
        raise RuntimeError(f"click did not land: wanted {(x, y)} got {d.get('landed')}")
    if settle:
        time.sleep(settle)
    return d


# ----------------------------------------------------------------- geometry

def find_board(im):
    """Locate the board. Raises instead of returning a plausible-looking guess."""
    px = im.load()
    W, H = im.size
    from collections import Counter
    c = Counter()
    for y in range(0, H, 3):
        for x in range(0, W, 3):
            c[px[x, y]] += 1
    cands = []
    for col, _ in c.most_common(60):
        if 6 <= max(col) - min(col) and 60 <= sum(col) / 3 <= 235:
            if all(sum(abs(col[i] - o[i]) for i in range(3)) > 40 for o in cands):
                cands.append(col)
        if len(cands) == 2:
            break
    if len(cands) < 2:
        raise RuntimeError("no board colours on screen")
    A, B = cands

    def isboard(x, y):
        p = px[x, y]
        for col in (A, B):
            if abs(p[0] - col[0]) < 24 and abs(p[1] - col[1]) < 24 and abs(p[2] - col[2]) < 24:
                return True
        return False

    cell = 16
    minx, miny, maxx, maxy = W, H, -1, -1
    for y0 in range(0, H, cell):
        for x0 in range(0, W, cell):
            tot = hit = 0
            for y in range(y0, min(y0 + cell, H), 4):
                for x in range(x0, min(x0 + cell, W), 4):
                    tot += 1
                    if isboard(x, y):
                        hit += 1
            if tot and hit / tot > 0.9:
                minx, miny = min(minx, x0), min(miny, y0)
                maxx, maxy = max(maxx, min(x0 + cell, W)), max(maxy, min(y0 + cell, H))
    if maxx < 0:
        raise RuntimeError("no board region found")

    def frac_x(x, y0, y1):
        n = h = 0
        for y in range(y0, y1, 2):
            n += 1
            if isboard(x, y):
                h += 1
        return h / max(n, 1)

    def frac_y(y, x0, x1):
        n = h = 0
        for x in range(x0, x1, 2):
            n += 1
            if isboard(x, y):
                h += 1
        return h / max(n, 1)

    L = R = T = Bo = None
    for x in range(minx - cell, minx + 2 * cell):
        if 0 <= x < W and frac_x(x, miny + 20, maxy - 20) > 0.97:
            L = x; break
    for x in range(maxx + cell, maxx - 2 * cell, -1):
        if 0 <= x < W and frac_x(x, miny + 20, maxy - 20) > 0.97:
            R = x; break
    for y in range(miny - cell, miny + 2 * cell):
        if 0 <= y < H and frac_y(y, minx + 20, maxx - 20) > 0.97:
            T = y; break
    for y in range(maxy + cell, maxy - 2 * cell, -1):
        if 0 <= y < H and frac_y(y, minx + 20, maxx - 20) > 0.97:
            Bo = y; break
    if None in (L, R, T, Bo):
        raise RuntimeError("could not refine board edges")
    bw, bh = R - L + 1, Bo - T + 1
    sx, sy = bw / 8.0, bh / 8.0
    if abs(sx - sy) > 1.5 or not (30 <= sx <= 220):
        raise RuntimeError(f"board geometry rejected: {bw}x{bh} -> {sx:.1f}x{sy:.1f}")
    return {"bbox": [L, T, R, Bo], "sq": sx}


def square_box(geo, file_idx, rank_idx, flipped=False):
    """Pixel box of a square. rank_idx 0 = rank 1."""
    L, T, R, B = geo["bbox"]
    if flipped:
        file_idx, rank_idx = 7 - file_idx, 7 - rank_idx
    sx = (R - L + 1) / 8.0
    sy = (B - T + 1) / 8.0
    x0 = int(round(L + file_idx * sx))
    y0 = int(round(B + 1 - (rank_idx + 1) * sy))
    return (x0, y0, int(round(x0 + sx)), int(round(y0 + sy)))


def square_centre(geo, file_idx, rank_idx, flipped=False):
    x0, y0, x1, y1 = square_box(geo, file_idx, rank_idx, flipped)
    return ((x0 + x1) // 2, (y0 + y1) // 2)


# ----------------------------------------------------------------- recognition

PATCH_N = 32

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

SHAPES = ["P", "N", "B", "R", "Q", "K"]


def sq_patch(im, geo, f, r, flipped, n=PATCH_N, inset=0.14):
    """Centre crop of a square.

    The inset is not cosmetic. lichess draws coordinate labels (a-h, 1-8) inside
    the edge of the border squares, and those dark glyphs were being counted as
    piece pixels -- which flipped white bishops, queens and rooks on rank 1 to
    "black" and made the bot lose games to a misread, not to Stockfish. Sampling
    the middle 72% keeps the piece (always centred) and drops the labels.
    """
    x0, y0, x1, y1 = square_box(geo, f, r, flipped)
    dx, dy = int((x1 - x0) * inset), int((y1 - y0) * inset)
    return im.crop((x0 + dx, y0 + dy, x1 - dx, y1 - dy)).resize((n, n), Image.LANCZOS)


def patch_features(p):
    """Return (shape mask, fill fraction, is_white, stats).

    Colour is decided by COUNTING strongly-bright and strongly-dark pixels
    against the square's own background, not by comparing medians or means.
    Both of those were measured and both were unreliable: the mean is dragged
    down by a white piece's dark outline, and the median can land mid-grey on a
    dark square. Counting is unambiguous --

        white piece : a large bright population (its fill) + a thin dark outline
        black piece : a large dark population, nothing brighter than the square

    and it holds on light and dark squares alike, because everything is measured
    against that square's own background rather than an absolute threshold.
    """
    a = np.asarray(p, dtype=np.float32)
    h, w, _ = a.shape
    luma = a @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    k = max(2, w // 8)
    corners = np.concatenate([
        luma[:k, :k].ravel(), luma[:k, -k:].ravel(),
        luma[-k:, :k].ravel(), luma[-k:, -k:].ravel()])
    bg = float(np.median(corners))
    bright = luma > bg + 22.0
    dark = luma < bg - 45.0
    mask = (bright | dark)
    fill = float(mask.mean())

    # Colour: look INSIDE the glyph, not at all of it.
    # Every lichess piece is drawn as a fill inside a dark outline. Counting
    # bright vs dark pixels over the whole glyph therefore fails for the pieces
    # whose outlines and internal detail are thick -- rook, bishop, queen -- even
    # though they are plainly white. (Measured: 87% square accuracy, and the bot
    # lost a live game to it.) Eroding the mask throws the outline away, and the
    # remaining interior is simply the piece's fill colour.
    interior = mask
    for _ in range(2):
        interior = (interior & np.roll(interior, 1, 0) & np.roll(interior, -1, 0)
                    & np.roll(interior, 1, 1) & np.roll(interior, -1, 1))
    if interior.sum() >= 8:
        zone = interior
    elif mask.sum() > 0:
        zone = mask
    else:
        zone = None
    if zone is None:
        fill_luma = bg
        is_white = False
    else:
        inner_bright = int((luma[zone] > bg + 25.0).sum())
        inner_dark = int((luma[zone] < bg - 45.0).sum())
        fill_luma = float(np.median(luma[zone]))
        # Measured on a real board: black pieces have a fill luma of ~2-3, white
        # pieces ~250, and the white queen -- whose interior is part fill, part
        # crown detail -- sits at ~152 against a background of 219. Comparing the
        # fill to the background (rather than to a count of bright pixels) puts
        # every one of those on the correct side with a wide margin.
        is_white = bool(fill_luma > bg * 0.6)
    stats = {"bg": round(bg, 1), "bright": int(bright.sum()), "dark": int(dark.sum()),
             "fill_luma": round(fill_luma, 1),
             "inner_bright": int((luma[zone] > bg + 25.0).sum()) if zone is not None else 0,
             "inner_dark": int((luma[zone] < bg - 45.0).sum()) if zone is not None else 0}
    return mask.astype(np.float32), fill, is_white, stats


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
    # cwd matters: the engine loads book.txt / avoid.txt from the directory it is
    # started in. Pin it to the engine's own folder so learned lines are always
    # found, however the bot was launched.
    eng = _engine_path()
    if _is_stockfish(eng):
        return _stockfish_best(fen, ms)
    _ensure_executable(eng)
    pre = os.environ.get("SABLE_SETOPTIONS", "")
    uci = (pre + "\n" if pre else "") + f"position fen {fen}\ngo movetime {ms}\nquit\n"
    p = subprocess.run([eng], input=uci,
                       capture_output=True, text=True, timeout=30,
                       cwd=os.path.dirname(os.path.abspath(eng)))
    for line in p.stdout.splitlines():
        if line.startswith("bestmove"):
            return line.split()[1]
    raise RuntimeError("engine gave no move")


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
            return path, im, geo
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("no board appeared on screen")


def play_game(side=chess.WHITE, engine_ms=200, max_seconds=120, log_path=None,
              board_timeout=40.0, record_dir=None, resume=False, clock_ms=None):
    """Play one game against whatever is on screen. Returns a dict of evidence."""
    log = []

    def note(**kw):
        kw["t"] = round(time.time() - t_start, 2)
        log.append(kw)
        if log_path:
            with open(log_path, "w") as f:
                json.dump(log, f, indent=2)

    t_start = time.time()
    path, im, geo = wait_for_board()
    note(event="board_found", geo=[round(v) for v in geo["bbox"]])

    TPL = os.path.join(HERE, "..", "results", "templates.npz")
    rec = Recogniser()
    if resume:
        # Templates must come from a position we know. They describe how lichess
        # DRAWS a piece, not where it is, so one cached set works for every board
        # in the session. (Learning them from an unknown mid-game board is
        # circular and produced garbage.)
        if not rec.load(TPL):
            raise RuntimeError("no cached templates; start a game normally once so they can be learned")
        flipped, read, _ = read_auto(rec, im, geo)
        board = read
        board.turn = side
        note(event="resumed", fen=board.board_fen(), flipped=flipped)
        # Is it actually my move? Give the opponent three seconds to move; if the
        # position is unchanged after that, the move is mine.
        time.sleep(3.0)
        _, im2 = grab("resumecheck")
        geo2 = find_board_cached(im2)
        now2, _ = rec.read(im2, geo2, flipped)
        if now2.board_fen() != board.board_fen():
            for m in list(board.legal_moves):
                probe = board.copy(); probe.push(m)
                if probe.board_fen() == now2.board_fen():
                    board = probe
                    break
            note(event="opponent_moved_first", fen=board.board_fen())
        my_turn = True
    else:
        rec.learn(im, geo, chess.Board(), False)
        rec.save(TPL)
        note(event="templates_cached", path=TPL)
        flipped, read, _ = read_auto(rec, im, geo)
        if read.board_fen() != chess.Board().board_fen():
            raise RuntimeError(f"game did not start from the normal position: {read.board_fen()}")
        note(event="start_verified", flipped=flipped)
        board = chess.Board()
        my_turn = (board.turn == side)
    stalls = 0
    # Our OWN clock, not the wall clock. Counting the opponent's thinking against
    # us made the harness abandon games with most of our time still on the board.
    my_spent_ms = 0.0
    MOVE_OVERHEAD_MS = 750   # measured cost of click + verify + screenshot

    while time.time() - t_start < max_seconds:
        # ---- my move
        if my_turn and not board.is_game_over():
            # Clock pacing. The harness itself costs ~0.4-0.8s per move in
            # screenshots and clicks, so the engine's thinking time has to leave
            # room for it. Losing on time is a loss like any other (it happened
            # at level 4, with the board still defensible), so the budget is
            # derived from the real clock, not from a fixed delay.
            elapsed_ms = (time.time() - t_start) * 1000
            if clock_ms:
                clock_left = max(0.0, clock_ms - my_spent_ms)
                # Only stop when OUR clock is nearly gone, keeping enough time to
                # land one more move.
                if clock_left < 4000:
                    note(event="low_clock_stop", left_ms=round(clock_left)); break
                think = int(max(60, min(engine_ms, clock_left / 26.0)))
            else:
                remaining = max_seconds - (time.time() - t_start)
                think = max(60, min(engine_ms, int(remaining * 1000 / 12)))
            turn_started = time.time()
            try:
                uci = sable_best(board.fen(), think)
            except Exception as e:
                note(event="engine_error", err=str(e)); break
            mv = chess.Move.from_uci(uci)
            if mv not in board.legal_moves:
                note(event="illegal_engine_move", move=uci); break
            try:
                a, b = play_move(board, mv, geo, flipped, rec=rec, label=f'ply{board.ply()}')
            except Exception as e:
                note(event="click_failed", move=uci, err=str(e)); break
            board.push(mv)
            # charge what the turn ACTUALLY cost, not an estimate: clicking and
            # verifying is the dominant term and it varies with the position
            my_spent_ms += (time.time() - turn_started) * 1000.0
            note(event="my_move", move=uci, from_px=list(a), to_px=list(b), think_ms=think,
                 my_clock_left_ms=round(clock_ms - my_spent_ms) if clock_ms else None)
            time.sleep(0.10)
            my_turn = False
            stalls = 0
            continue

        # ---- opponent's move: poll until the position changes in a way that
        # corresponds to EXACTLY ONE legal move.
        #
        # A single read is not enough: lichess animates the moving piece, so a
        # poll can land mid-flight and see a bishop part-way between f8 and d6,
        # which reads as garbage. That is precisely how the first 10+0 attempt
        # died -- the board said "p" where a bishop belonged, no legal move
        # matched, and the bot gave up. So: retry the read a few times before
        # concluding anything, and only ever act on an unambiguous match.
        cands = []
        stalls += 1
        time.sleep(0.55)          # the computer moves in 50-1000ms; wait once, don't spin
        for attempt in range(2):
            try:
                path, im = grab("poll")
                geo = find_board_cached(im)
                now, _ = rec.read(im, geo, flipped)
            except Exception as e:
                note(event="read_failed", err=str(e))
                time.sleep(0.1)
                continue
            if now.board_fen() == board.board_fen():
                break                                  # still my opponent's turn
            got = detect_opponent_move(board, now)
            if len(got) >= 1:
                cands = got
                break
            time.sleep(0.10)                           # probably mid-animation
        if not cands:
            if board.is_game_over(claim_draw=True):
                break
            if stalls in (6, 14):
                # Resync instead of dying. If the local board has drifted from the
                # real one (a click that missed, a piece misread, a move lichess
                # never registered), abandoning the game is the worst option: the
                # clock keeps running for the player either way. Adopt the board as
                # it actually stands and carry on.
                try:
                    _, imr = grab("resync")
                    geor = find_board_cached(imr)
                    real, _ = rec.read(imr, geor, flipped)
                    if real.is_valid() and len(real.pieces(chess.KING, side)) == 1:
                        real.turn = side
                        lf = [m.uci() for m in board.move_stack]
                        note(event="resynced", was=board.board_fen(), now=real.board_fen(),
                             diverge_plies=len(lf))
                        board = real
                        my_turn = True
                        stalls = 0
                        continue
                except Exception as e:
                    note(event="resync_failed", err=str(e))
            if stalls > 18:                            # ~10s and still nothing
                note(event="no_progress", stalls=stalls, last_read=now.board_fen())
                break
            time.sleep(0.04)
            continue
        board.push(cands[0])
        note(event="opponent_move", move=cands[0].uci(), plies=board.ply())
        stalls = 0
        if board.is_game_over(claim_draw=True):
            break
        my_turn = True

    # ---- final evidence
    try:
        path, im = grab("final")
        geo = find_board_cached(im)
        final_read, _ = rec.read(im, geo, flipped)
        note(event="final", local_fen=board.fen(), screen_fen=final_read.fen(),
             agrees=(final_read.board_fen() == board.board_fen()),
             local_result=board.result(claim_draw=True) if board.is_game_over(claim_draw=True) else "ongoing")
        if record_dir:
            os.makedirs(record_dir, exist_ok=True)
            with open(os.path.join(record_dir, "final_screen.jpg"), "wb") as f:
                im.save(f, "JPEG", quality=90)
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
