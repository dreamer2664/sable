#!/usr/bin/env python3
"""boardvision — one source of truth for finding the board and reading it.

WHY THIS FILE EXISTS
--------------------
The board-location routine was copy-pasted into three tools
(`lichess_bot.find_board`, `play_on_lichess_board.geometry`,
`recogniser_accuracy`), so a bug fixed in one stayed alive in the other two. It
carried a bug worth naming, measured on the 238 screenshots the live runs left in
`results/bot_shots/`:

    the coarse pass found the board 238/238 times, but the edge-refinement pass
    then demanded that a SINGLE pixel column be >97% board colour. A column that
    happens to cross a rank full of pieces scores well below that, the refinement
    returned None, and the whole detection was thrown away -- including the coarse
    box that was already correct to within one 16px cell.

    measured: 143/238 = 60% of real frames located, ~145 ms each.

What replaces it, and why it is both more robust and faster:

  * COARSE pass at 1/4 scale (a box filter), giving the board to within 16px.
    Cheap: the full-resolution scan was ~145 ms on its own.
  * EDGE pass on four narrow full-resolution strips centred on the coarse edges.
    Height/width of the board is measured only where it matters, not across the
    whole frame.
  * The edge in each strip is the contiguous run of board-bearing lines that
    reaches the strip's INWARD end. The board always fills that end and stops
    somewhere inside, so this finds the true edge and ignores stray matching
    pixels. It does not depend on how many pieces sit on a rank.
  * COLOURS are chosen by filtering to board-like colours FIRST and ranking by
    frequency second. Ranking first fills every top-N slot with near-black page
    background ((0,0,0), (1,1,1), (2,2,2), ...) on a dark theme.

    measured: 238/238 = 100% of real frames, 1px from the hand-verified box,
    ~36 ms each.

It also exposes `geo_from_crop()`, which makes the in-game cost of geometry ZERO:
ask pc-eye for a screenshot of the board region only and the returned image *is*
the board, so no search is needed. That is 2.9x fewer bytes over the tunnel as
well (123 KB -> 43 KB per frame on the recorded captures).

Nothing here is allowed to guess. Every rejection raises with a sentence that
says what was wrong.
"""
from __future__ import annotations

import time

import numpy as np
from PIL import Image

PATCH_N = 32
SHAPES = ["P", "N", "B", "R", "Q", "K"]

# Defaults, all measured on the recorded frames rather than chosen by eye.
SCALE = 4          # coarse pass downscale
PAD = 24           # half-width of an edge strip, px at full resolution
EDGE_FRAC = 0.25   # a line is "board" if its coverage exceeds this fraction of the strip's best
SQ_MIN, SQ_MAX = 30.0, 220.0   # plausible square size in px
SQUARENESS_TOL = 1.5           # |sq_w - sq_h| tolerated
DEFAULT_MIN_CONFIDENCE = 0.15  # mean per-square template-confidence gate
                               # (measured: clean reads 0.223..0.456, a
                               #  7-17px misalignment 0.040..0.122)


# ------------------------------------------------------------------ colours
def board_colours(a, tol_spread=6, luma=(60, 235), merge=40):
    """The two dominant colours that could be squares.

    `a` is an RGB array (downscaled is fine, and faster).

    Filter to board-like colours first, then rank by frequency: a dark page
    background produces thousands of near-black buckets which otherwise fill
    every slot of a top-N list before a single square colour is seen.
    """
    a = np.asarray(a)
    flat = a[::2, ::2].reshape(-1, 3).astype(np.int32)
    q = flat // 8
    key = (q[:, 0] << 16) | (q[:, 1] << 8) | q[:, 2]
    uniq, counts = np.unique(key, return_counts=True)
    # unpack the //8 bucket, then scale back: 0..31 -> 0..248
    cols = (np.stack([(uniq >> 16) & 255, (uniq >> 8) & 255, uniq & 255], axis=1) * 8).astype(np.int16)
    spread = cols.max(axis=1) - cols.min(axis=1)
    mean = cols.mean(axis=1)
    ok = (spread >= tol_spread) & (mean >= luma[0]) & (mean <= luma[1])
    cols, counts = cols[ok], counts[ok]
    if len(cols) == 0:
        return None
    cands = []
    for idx in np.argsort(-counts):
        col = cols[idx]
        if all(np.abs(col - c).sum() > merge for c in cands):
            cands.append(col)
        if len(cands) == 2:
            break
    return cands if len(cands) == 2 else None


def colour_mask(a, cols, tol=24):
    """Boolean mask of pixels matching either square colour."""
    m = np.zeros(a.shape[:2], dtype=bool)
    for c in cols:
        m |= (np.abs(a - c).max(axis=2) < tol)
    return m


# ------------------------------------------------------------------ geometry
def coarse_box(mask, cell):
    """Bounding box of the region whose cells are >90% board colour."""
    H, W = mask.shape
    ii = np.zeros((H + 1, W + 1), dtype=np.int32)
    ii[1:, 1:] = mask.astype(np.int32).cumsum(0).cumsum(1)
    minx, miny, maxx, maxy = W, H, -1, -1
    for y0 in range(0, H, cell):
        y1 = min(y0 + cell, H)
        for x0 in range(0, W, cell):
            x1 = min(x0 + cell, W)
            tot = (y1 - y0) * (x1 - x0)
            if tot and (ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]) / tot > 0.9:
                minx, miny = min(minx, x0), min(miny, y0)
                maxx, maxy = max(maxx, x1), max(maxy, y1)
    return None if maxx < 0 else (minx, miny, maxx, maxy)


def edge_run(profile, inward, frac=EDGE_FRAC):
    """First and last line of the run that reaches the strip's inward end.

    A strip is cut inward-to-outward around a coarse edge, so the board fills the
    inward end and stops somewhere inside. Selecting the run that touches that end
    finds the true edge and ignores stray matching pixels elsewhere.

    (The rule it replaced -- "a single column must be 97% board colour" -- threw
    away correct detections whenever the column crossed pieces, and a "longest
    run" variant rejected genuine edges whenever the strip was narrower than the
    minimum run length. Both are why this looks fussy.)
    """
    if profile.max() <= 0:
        return None
    good = profile >= max(2.0, profile.max() * frac)
    idx = np.flatnonzero(good)
    if not len(idx):
        return None
    runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
    want = len(profile) - 1 if inward == "end" else 0
    hits = [r for r in runs if (r[-1] == want if inward == "end" else r[0] == want)]
    if not hits:
        return None
    run = max(hits, key=len)
    return int(run[0]), int(run[-1])


def _sane(L, T, R, B):
    bw, bh = R - L, B - T
    sx, sy = bw / 8.0, bh / 8.0
    return (abs(sx - sy) <= SQUARENESS_TOL) and (SQ_MIN <= sx <= SQ_MAX) and (SQ_MIN <= sy <= SQ_MAX)


def find_board(im, scale=SCALE, pad=PAD, frac=EDGE_FRAC):
    """Locate the board in a full screenshot.

    Returns {"bbox": [L, T, R, B], "sq": float, "method": str, "ms": float}.
    Raises RuntimeError with a sentence when the board cannot be found -- never a
    plausible-looking guess.
    """
    t0 = time.perf_counter()
    im = im.convert("RGB")
    W, H = im.size
    a_s = np.asarray(im.reduce(scale), dtype=np.int16)
    cols = board_colours(a_s)
    if cols is None:
        raise RuntimeError("no board colours on screen")
    cs = coarse_box(colour_mask(a_s, cols), cell=max(2, 16 // scale))
    if cs is None:
        raise RuntimeError("no board region found")
    cL, cT, cR, cB = (v * scale for v in cs)      # coarse, full-res, +-16px

    a = np.asarray(im, dtype=np.int16)

    def strip_edges(y0, y1, x0, x1, axis, inward):
        y0, y1 = max(0, min(H, y0)), max(0, min(H, y1))
        x0, x1 = max(0, min(W, x0)), max(0, min(W, x1))
        if y1 - y0 < 8 or x1 - x0 < 8:
            return None
        m = colour_mask(a[y0:y1, x0:x1], cols)
        prof = m.sum(axis=0) if axis == "x" else m.sum(axis=1)
        r = edge_run(prof, inward, frac)
        if not r:
            return None
        off = x0 if axis == "x" else y0
        return off + r[0], off + r[1]

    L = R = T = B = None
    e = strip_edges(cT - pad, cB + pad, cL - pad, cL + pad, "x", "end")
    if e:
        L = e[0]
    e = strip_edges(cT - pad, cB + pad, cR - pad, cR + pad, "x", "start")
    if e:
        R = e[1]
    L = cL if L is None else L
    R = cR if R is None else R
    e = strip_edges(cT - pad, cT + pad, L, R, "y", "end")
    if e:
        T = e[0]
    e = strip_edges(cB - pad, cB + pad, L, R, "y", "start")
    if e:
        B = e[1]
    T = cT if T is None else T
    B = cB if B is None else B

    method = "edges"
    if not _sane(L, T, R, B):
        L, T, R, B = cL, cT, cR, cB
        method = "coarse"
        if not _sane(L, T, R, B):
            raise RuntimeError(f"board geometry rejected: {R - L}x{B - T} px "
                               f"(a square would be {(R - L) / 8:.1f}x{(B - T) / 8:.1f})")
    return {"bbox": [int(L), int(T), int(R) - 1, int(B) - 1],
            "sq": (R - L) / 8.0, "method": method,
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


def geo_from_crop(im):
    """Geometry for an image that IS the board (a pc-eye `?region=` crop).

    Asking the server for the board region makes geometry free: there is nothing
    to search, and the result cannot drift with page layout. The only thing worth
    checking is that the crop is square enough to be an 8x8 grid.
    """
    W, H = im.size
    if not _sane(0, 0, W, H):
        raise RuntimeError(f"region screenshot is not a plausible board: {W}x{H} "
                           f"(a square would be {W / 8:.1f}x{H / 8:.1f})")
    return {"bbox": [0, 0, W - 1, H - 1], "sq": W / 8.0, "method": "crop", "ms": 0.0}


def board_region(geo, margin=0):
    """The `region=x,y,w,h` query value that asks pc-eye for just the board.

    A small margin keeps the board's outer edge inside the crop; the cost is a
    few pixels of payload and the value of not clipping a piece on the a/h file.
    """
    L, T, R, B = geo["bbox"]
    x, y = max(0, L - margin), max(0, T - margin)
    w, h = (R - L + 1) + 2 * margin, (B - T + 1) + 2 * margin
    return f"{int(x)},{int(y)},{int(w)},{int(h)}"


# ------------------------------------------------------------------ squares
def square_box(geo, file_idx, rank_idx, flipped=False):
    """Pixel box of one square. rank_idx 0 = rank 1."""
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


def sq_patch(im, geo, f, r, flipped, n=PATCH_N, inset=0.14):
    """Centre crop of one square, resized to n x n.

    The inset is not cosmetic: lichess draws file/rank labels inside the edge of
    the border squares, and those glyphs were being counted as piece pixels,
    flipping white pieces on rank 1 to black. Sampling the middle 72% keeps the
    piece (always centred) and drops the labels.
    """
    x0, y0, x1, y1 = square_box(geo, f, r, flipped)
    dx, dy = int((x1 - x0) * inset), int((y1 - y0) * inset)
    return im.crop((x0 + dx, y0 + dy, x1 - dx, y1 - dy)).resize((n, n), Image.LANCZOS)


# ------------------------------------------------------------------ features
def patch_features(p):
    """Return (shape mask, fill fraction, is_white, stats) for one square patch.

    Colour is decided by COUNTING strongly-bright and strongly-dark pixels
    against the square's own background, not by comparing medians or means:
    the mean is dragged down by a white piece's dark outline, and the median can
    land mid-grey on a dark square. Counting is unambiguous, and it holds on light
    and dark squares alike because everything is measured against that square's
    own background.

    Erosion before measuring the fill matters for the pieces with thick outlines
    (rook, bishop, queen): counting over the whole glyph made plainly-white pieces
    read as black, measured at 87% square accuracy on a real board.
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
        fill_luma = float(np.median(luma[zone]))
        # Colour = the interior's fill luminance RELATIVE TO THE SQUARE'S OWN
        # background, so one rule serves light and dark squares and re-themes.
        #
        # Where the boundary sits is measured, not chosen. Over the 161 pieces in
        # the ten ground-truth positions shipped in results/:
        #
        #     white pieces   fill/bg  0.677 .. 2.014   (the white QUEEN is 0.677:
        #                                               part fill, part crown detail)
        #     black pieces   fill/bg  0.001 .. 0.025
        #
        # a 27x gap. The previous constant, 0.6, sat 0.077 above the white queen
        # and 0.575 below the black maximum -- i.e. it was correct on clean frames
        # with almost no room, and a one-pixel misalignment (0.553) was enough to
        # flip a white queen to black. Placing the boundary at the geometric middle
        # of the measured gap, 0.13, leaves both sides more than 5x of margin; 0.25
        # is used as a deliberately conservative value that still gives ~2.7x on
        # the white side and ~10x on the black side.
        is_white = bool(fill_luma > bg * 0.25)
    stats = {"bg": round(bg, 1), "bright": int(bright.sum()), "dark": int(dark.sum()),
             "fill_luma": round(fill_luma, 1)}
    return mask.astype(np.float32), fill, is_white, stats


# ------------------------------------------------------------------ cache
class GeometryCache:
    """Remember the board box for a screen layout, but never blindly.

    Geometry changes only when the window moves, the layout changes or the page
    scrolls. Re-deriving it on every poll costs 36 ms x ~2.5 frames per move for
    no information, so it is cached against the image size and re-validated by a
    cheap full-frame check only when the caller asks for one.
    """

    def __init__(self):
        self._geo = {}

    def get(self, im, validate=False, scale=SCALE):
        key = im.size
        geo = self._geo.get(key)
        if geo is None:
            geo = find_board(im, scale=scale)
            self._geo[key] = geo
            return geo
        if validate:
            try:
                fresh = find_board(im, scale=scale)
            except RuntimeError:
                self._geo.pop(key, None)
                raise
            if tuple(fresh["bbox"]) != tuple(geo["bbox"]):
                self._geo[key] = fresh
                return fresh
        return geo

    def set(self, im, geo):
        self._geo[im.size] = geo

    def clear(self):
        self._geo.clear()


# ------------------------------------------------------------------ reading
def classify_scored(rec, mask):
    """Best shape and (confidence margin) for one square's mask.

    The margin is the gap between the best and the second-best template distance,
    relative to the best. A genuine piece scores a wide margin; a misread --
    a piece mid-animation, a themed glyph, a clipped square -- scores a narrow
    one. The live loop ignores this and simply takes the best match, which is how
    a half-drawn bishop became "a pawn" and stalled a game.
    """
    scores = []
    for sym, masks in rec.shapes.items():
        for m in masks:
            scores.append((float(np.abs(mask - m).mean()), sym))
    if not scores:
        return None, 0.0
    scores.sort()
    best_d, best = scores[0]
    second_d = next((d for d, s in scores[1:] if s != best), None)
    if second_d is None:
        margin = 1.0
    else:
        margin = 0.0 if second_d <= 0 else (second_d - best_d) / max(second_d, 1e-6)
    return best, margin


def read_board(rec, im, geo, flipped, empty_threshold=0.03):
    """Read a position, and say how sure each square is.

    Returns (board, detail) where detail[sq] = (symbol, confidence_margin), and
    "." with margin 1.0 for an empty square.
    Same result as `Recogniser.read`, plus the margins that make a bad read
    detectable instead of merely wrong.
    """
    import chess

    board = chess.Board(None)
    detail = {}
    for sq in chess.SQUARES:
        f, r = chess.square_file(sq), chess.square_rank(sq)
        mask, fill, is_white, _ = patch_features(sq_patch(im, geo, f, r, flipped))
        if fill < empty_threshold:
            detail[sq] = (".", 1.0)
            continue
        sym, margin = classify_scored(rec, mask)
        piece = chess.Piece.from_symbol(sym if is_white else sym.lower())
        board.set_piece_at(sq, piece)
        detail[sq] = (piece.symbol(), margin)
    return board, detail


def read_plausible(rec, im, geo, flipped, side=None, min_confidence=DEFAULT_MIN_CONFIDENCE):
    """Read, then refuse the result unless it is a position chess allows.

    Returns (board, reason). `reason` is "ok" on success, else a sentence.

    The side to move is NOT forced. Whose turn it is cannot be read off a
    screenshot, and assuming the caller's own side is a trap: after our own move
    the opponent is to move, so a position that is legal with Black to play looks
    "invalid (opposite check)" if White is assumed. Both turns are tried and the
    board is returned with whichever one validates, preferring `side` when both
    do. Callers compare `board_fen()`, which ignores the turn anyway.

    The confidence gate is the part with teeth, and it is judged on the MEAN
    per-square margin, not the worst square. Measured over the frames shipped in
    results/ plus a pawn-heavy start position:

        clean reads        mean margin 0.223 .. 0.456
        board shifted 7px  mean margin 0.040 .. 0.122
        board shifted 17px mean margin 0.040 .. 0.122

    A worst-square rule does not work: on a real start position the weakest single
    square scores 0.011 because eight identical pawns sit one square apart and no
    template separates them well, yet the read is perfect. One bad square is
    normal; a uniformly bad board is not. The live loop previously had no such
    check at all -- it adopted whatever the recogniser said, then clicked on it.
    """
    import chess

    board, detail = read_board(rec, im, geo, flipped)
    if len(board.pieces(chess.KING, chess.WHITE)) != 1:
        return None, "white king count != 1"
    if len(board.pieces(chess.KING, chess.BLACK)) != 1:
        return None, "black king count != 1"
    margins = [v[1] for v in detail.values() if v[0] != "."]
    if not margins:
        return None, "no pieces read at all"
    mean_conf = float(np.mean(margins))
    if mean_conf < min_confidence:
        worst = sorted(((round(v[1], 3), chess.square_name(s)) for s, v in detail.items()
                        if v[0] != "."))[:4]
        return None, (f"low reading confidence: mean {mean_conf:.3f} < {min_confidence} "
                      f"over {len(margins)} pieces (weakest {worst}) -- the board geometry "
                      f"is probably wrong, or a piece is mid-animation")
    legal = []
    for who in (chess.WHITE, chess.BLACK):
        board.turn = who
        if board.is_valid():
            legal.append(who)
    if not legal:
        return None, f"invalid position (status {board.status()})"
    board.turn = side if side in legal else legal[0]
    return board, "ok"




