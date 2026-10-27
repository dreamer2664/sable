#!/usr/bin/env python3
"""A virtual lichess board, so the whole play loop can be tested without a PC.

WHY THIS EXISTS
---------------
Every bug in this project's history was found by losing a live game: a mid-animation
read, a mis-detected board, a click that missed. Reproducing one meant starting a
real game on a real machine with a real tunnel up, which is why there were so few
tests and why the same class of bug kept returning.

This module closes that gap. It renders a board the way lichess draws one -- the
two square colours and the real piece glyphs, cropped from the screenshots in
`results/bot_shots/` -- and answers the same calls pc-eye answers:

    GET /shot[?region=x,y,w,h]   -> a JPEG of the virtual screen
    POST /act {"action":"click"} -> a click, applied to the virtual position

`lichess_bot` cannot tell the difference, so `play_game` runs end to end in a few
seconds with no screen, no tunnel and no opponent. Every test in `test_loop.py`
drives a real game through it.

The glyphs are real because a synthetic shape would not reproduce the actual
failure modes: the dark-theme palette, the anti-aliased edges, the outline-heavy
rooks and queens that made colour classification fail at 87%.
"""
import io
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

import chess  # noqa: E402

# The two lichess square colours this project's screenshots use.
LIGHT = (238, 218, 185)
DARK = (174, 138, 106)
SCREEN_W, SCREEN_H = 1920, 1080
BOARD = (606, 173, 1308, 875)          # the box measured on the recorded frames
FEN_PIECES = "PNBRQKpnbrqk"

START_FRAME = os.path.join(ROOT, "results", "bot_shots", "1792954475704_wait.jpg")


def square_colour(file_idx, rank_idx):
    """a1 is a dark square on lichess."""
    return LIGHT if (file_idx + rank_idx) % 2 == 1 else DARK


SPRITE = int(round((BOARD[2] - BOARD[0] + 1) / 8.0))     # 88 px


def V_SQUARE_BOX(geo, f, r):
    """Square box within a board-image-local geometry (no dependency on tools/)."""
    L, T, R, B = geo["bbox"]
    sx = (R - L + 1) / 8.0
    sy = (B - T + 1) / 8.0
    x0 = int(round(L + f * sx))
    y0 = int(round(B + 1 - (r + 1) * sy))
    return (x0, y0, int(round(x0 + sx)), int(round(y0 + sy)))


def _square_centre(f, r):
    """Pixel centre of a square, using the same fractional grid the source uses."""
    L, T, R, B = BOARD
    sx = (R - L + 1) / 8.0
    sy = (B - T + 1) / 8.0
    return L + (f + 0.5) * sx, T + (7 - r + 0.5) * sy


def load_sprites(frame_path=START_FRAME):
    """Crop one image of each piece and of an empty square, centred, from a real
    start-position screenshot.

    Pieces are cropped CENTRED and at a fixed size rather than from square corner
    to square corner. The board's 703px width does not divide evenly by 8, so
    corner-to-corner boxes alternate 87/88 px and a paste built that way lands a
    pixel off what lichess drew; the recogniser then sees a subtly different glyph
    -- enough, in fact, to read the white queen as black. A fixed centred patch
    keeps every piece exactly where lichess keeps it and is identical for every
    square.
    """
    if not os.path.exists(frame_path):
        raise FileNotFoundError(f"no start-position frame at {frame_path}")
    im = Image.open(frame_path).convert("RGB")
    half = SPRITE // 2
    sprites = {}
    for sq, piece in chess.Board().piece_map().items():
        f, r = chess.square_file(sq), chess.square_rank(sq)
        cx, cy = _square_centre(f, r)
        cx, cy = int(round(cx)), int(round(cy))
        box = (cx - half, cy - half, cx - half + SPRITE, cy - half + SPRITE)
        sprites[piece.symbol()] = np.asarray(im.crop(box), dtype=np.int16)
        # The reference background is the colour of the square the sprite was cut
        # from -- known exactly -- NOT the crop's corner pixel. At a8 the corner
        # lands a pixel outside the board on the page, and using that as the
        # background made every dark piece get blended toward the board colour:
        # the black rook's fill went from luma 3 to 62 and it read as WHITE.
        sprites[piece.symbol() + "@bg"] = np.array(square_colour(f, r), dtype=np.int16)
    # an empty square, from the middle of the board (ranks 3-6 are empty at the start)
    cx, cy = _square_centre(4, 3)
    cx, cy = int(round(cx)), int(round(cy))
    empty = np.asarray(im.crop((cx - half, cy - half, cx - half + SPRITE, cy - half + SPRITE)),
                       dtype=np.int16)
    sprites["_empty"] = empty
    sprites["_empty@bg"] = np.array(square_colour(4, 3), dtype=np.int16)
    return sprites


class VirtualScreen:
    """A board, rendered to a screen-sized image, reachable through pc-eye's API."""

    def __init__(self, board=None, sprites=None, our_side=chess.WHITE,
                 opponent_reply=None, reply_after_looks=1, fallback_reply=True):
        self.board = board or chess.Board()
        self.sprites = sprites or load_sprites()
        src = Image.open(START_FRAME).convert("RGB")
        L, T, R, B = BOARD
        bg = np.asarray(src.crop((L, T, R + 1, B + 1)), dtype=np.float32).copy()
        # Paint out lichess's coordinate labels (a-h, 1-8), which it draws inside
        # the border squares. They are the one part of the fixture that is fragile:
        # they sit on the square edge, so a one-pixel geometry difference changes
        # how much of a label falls inside the recogniser's crop, and a rook next to
        # an "a" can read as a pawn. The recogniser samples the middle 72% of each
        # square, so nothing it actually looks at is lost.
        geo0 = {"bbox": [0, 0, R - L, B - T], "sq": (R - L + 1) / 8.0}
        for r in range(8):
            for f in range(8):
                x0, y0, x1, y1 = V_SQUARE_BOX(geo0, f, r)
                w, h = x1 - x0, y1 - y0
                edge_x, edge_y = max(1, int(w * 0.14)), max(1, int(h * 0.14))
                col = np.array(square_colour(f, r), dtype=np.float32)
                bg[y0:y0 + edge_y, x0:x1] = col
                bg[y1 - edge_y:y1, x0:x1] = col
                bg[y0:y1, x0:x0 + edge_x] = col
                bg[y0:y1, x1 - edge_x:x1] = col
        self._background = bg
        self._page = np.asarray(src, dtype=np.float32)   # the real screenshot, as a backdrop
        self.our_side = our_side
        self.opponent_reply = list(opponent_reply or [])   # scripted UCI moves
        self.reply_after_looks = reply_after_looks
        self._looks = 0
        self.selected = None
        self.clicks = []
        self.shots = 0
        self.moves_played = []
        self.auto_opponent = True
        # When the scripted replies run out, keep playing (first legal move) so a
        # game can reach a real conclusion instead of stalling on an opponent that
        # has stopped moving.
        self.fallback_reply = fallback_reply
        self.shot_delay = 0.0
        self._cache = {}
        self.events = []

    # -- rendering -----------------------------------------------------------
    def render(self):
        """Paint the position onto a copy of the REAL board image.

        Starting from the captured board and repainting only the squares whose
        contents changed keeps every untouched square byte-identical to what
        lichess drew -- the exact 703px grid, the anti-aliased edges and the
        coordinate labels on the border squares. Re-deriving the whole board from
        sprites instead made it a pixel narrower than the source, which moved the
        "a" label just far enough to make a1 read as a pawn.
        """
        key = self.board.fen()
        if key in self._cache:
            return self._cache[key]
        L, T, R, B = BOARD
        geo = {"bbox": [0, 0, R - L, B - T], "sq": (R - L + 1) / 8.0}
        start_board = chess.Board()
        flipped = (self.our_side == chess.BLACK)
        if flipped:
            # The captured page is an UNMIRRORED board, so the "only repaint what
            # changed" shortcut cannot apply: every square is drawn somewhere else.
            # Paint the whole board from sprites instead.
            board_img = np.zeros((B - T + 1, R - L + 1, 3), dtype=np.float32)
            for r in range(8):
                for f in range(8):
                    fc, rk = 7 - f, 7 - r
                    x0, y0, x1, y1 = V_SQUARE_BOX(geo, fc, rk)
                    board_img[y0:y1, x0:x1] = square_colour(fc, rk)
        else:
            board_img = np.array(self._background, dtype=np.float32)
        half = SPRITE // 2
        bl, bt = L, T                        # board_img is board-local; the page adds the offset
        for r in range(8):
            for f in range(8):
                sq = chess.square(f, r)
                want = self.board.piece_at(sq)
                have = start_board.piece_at(sq)
                if want == have and not flipped:
                    continue                     # untouched: keep lichess's pixels
                sym = want.symbol() if want else "_empty"
                sprite = self.sprites.get(sym)
                if sprite is None:
                    continue
                fc, rk = (f, r) if self.our_side != chess.BLACK else (7 - f, 7 - r)
                # Clear the whole square first, so nothing of the previous occupant
                # survives underneath the new glyph.
                x0, y0, x1, y1 = V_SQUARE_BOX(geo, fc, rk)
                board_img[y0:y1, x0:x1] = np.array(square_colour(fc, rk), dtype=np.float32)
                bg_src = self.sprites.get(sym + "@bg", sprite[0, 0]).astype(np.float32)
                bg_dst = np.array(square_colour(fc, rk), dtype=np.float32)
                patch = sprite.astype(np.float32)
                if np.abs(bg_src - bg_dst).sum() > 0:
                    # source-over: patch = a*C + (1-a)*bg_src  =>  out = patch + (1-a)*(bg_dst-bg_src)
                    delta = np.abs(patch - bg_src).max(axis=2)
                    a = np.clip(delta / 110.0, 0.0, 1.0)[:, :, None]
                    patch = patch + (1.0 - a) * (bg_dst[None, None, :] - bg_src[None, None, :])
                # Paste CENTRED on the same fractional grid the sprite was cut from,
                # not at the square's corner box. The board's 703px width does not
                # divide by 8 (87.875 px per square), so corner boxes alternate 87
                # and 88 px and a corner-anchored 88px paste lands up to a pixel off
                # what lichess drew. One pixel was enough to read a white rook on h4
                # as a PAWN (template distance 0.201 for P against 0.225 for R),
                # which is exactly the kind of false confident read the loop has to
                # be protected from -- so the fixture must not manufacture it.
                cx, cy = _square_centre(fc, rk)
                px, py = int(round(cx - bl - half)), int(round(cy - bt - half))
                h, w = patch.shape[0], patch.shape[1]
                bh, bw = board_img.shape[0], board_img.shape[1]
                sx0, sy0 = max(0, px), max(0, py)
                sx1, sy1 = min(bw, px + w), min(bh, py + h)
                if sx1 > sx0 and sy1 > sy0:
                    board_img[sy0:sy1, sx0:sx1] = np.clip(
                        patch[sy0 - py:sy1 - py, sx0 - px:sx1 - px], 0, 255)
        # Use the REAL screenshot as the page around the board. The board detector
        # refines its edges against whatever surrounds the board, so a synthetic
        # flat page made it report the board one pixel narrower than the real frame
        # -- enough to shift every square patch and turn a rook into a pawn. Copying
        # the original page makes the fixture behave exactly like the capture it
        # came from.
        full = np.array(self._page, dtype=np.float32)
        full[T:B + 1, L:R + 1] = np.clip(board_img, 0, 255)
        im = Image.fromarray(np.clip(full, 0, 255).astype(np.uint8))
        self._cache[key] = im
        return im

    def shot(self, region=None, fmt="jpeg"):
        if self.shot_delay:
            import time
            time.sleep(self.shot_delay)
        self.shots += 1
        im = self.render()
        if region:
            x, y, w, h = (int(v) for v in str(region).split(","))
            im = im.crop((x, y, x + w, y + h))
        buf = io.BytesIO()
        im.save(buf, "JPEG" if fmt == "jpeg" else "PNG",
                **({"quality": 80} if fmt == "jpeg" else {}))
        return buf.getvalue()

    # -- input ---------------------------------------------------------------
    def _square_at(self, x, y):
        L, T, R, B = BOARD
        if not (L <= x <= R and T <= y <= B):
            return None
        sx = (R - L + 1) / 8.0
        sy = (B - T + 1) / 8.0
        f = int((x - L) // sx)
        r = 7 - int((y - T) // sy)
        if self.our_side == chess.BLACK:
            f, r = 7 - f, 7 - r
        return chess.square(max(0, min(7, f)), max(0, min(7, r)))

    def click(self, x, y, button="left"):
        self.clicks.append((x, y))
        sq = self._square_at(x, y)
        self.events.append(("click", x, y, chess.square_name(sq) if sq is not None else None))
        if sq is None:
            return {"ok": True, "exact": True, "landed": [x, y], "off_board": True}
        if self.selected is None:
            self.selected = sq
            return {"ok": True, "exact": True, "landed": [x, y]}
        mv = chess.Move(self.selected, sq)
        # promotion: lichess opens a picker; this board auto-queens, which is what
        # the completed picker click ends up producing anyway.
        if self.board.piece_at(mv.from_square) and self.board.piece_at(mv.from_square).piece_type == chess.PAWN \
                and chess.square_rank(sq) in (0, 7):
            mv = chess.Move(self.selected, sq, promotion=chess.QUEEN)
        self.selected = None
        if mv not in self.board.legal_moves:
            self.events.append(("rejected", mv.uci()))
            return {"ok": False, "exact": True, "error": f"illegal {mv.uci()}"}
        self.board.push(mv)
        self.moves_played.append(mv.uci())
        return {"ok": True, "exact": True, "landed": [x, y]}

    def maybe_opponent_moves(self):
        """Play the opponent's reply once the bot has moved and looked a few times."""
        if not self.auto_opponent:
            return
        if self.board.turn == self.our_side:
            return
        self._looks += 1
        if self._looks < self.reply_after_looks:
            return
        self._looks = 0
        if self.opponent_reply:
            uci = self.opponent_reply.pop(0)
            try:
                mv = chess.Move.from_uci(uci)
            except ValueError:
                mv = None
            if mv is not None and mv in self.board.legal_moves:
                self.board.push(mv)
                return
        if self.fallback_reply:
            mv = next(iter(self.board.legal_moves), None)
            if mv is not None:
                self.board.push(mv)


def install(screen):
    """Install a fake `pceye` module so lichess_bot talks to `screen` instead of a PC.

    Must be called before `import lichess_bot`.
    """
    import json
    import types
    import urllib.parse

    mod = types.ModuleType("pceye")

    def load_creds():
        return ("http://virtual", "tok")

    def req(path, method="GET", body=None, timeout=30):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        if path.startswith("/shot"):
            screen.maybe_opponent_moves()
            region = (q.get("region") or [None])[0]
            fmt = (q.get("fmt") or ["jpeg"])[0]
            return {}, screen.shot(region=region, fmt=fmt), 200
        if path.startswith("/act"):
            payload = json.loads(body) if isinstance(body, (str, bytes)) else (body or {})
            if payload.get("action") == "click":
                return screen.click(payload["x"], payload["y"]), b"{}", 200
            return {"ok": True}, b"{}", 200
        raise RuntimeError(f"virtual pceye: no route {path}")

    def act(payload):
        payload = json.loads(payload) if isinstance(payload, (str, bytes)) else dict(payload)
        if payload.get("action") == "click":
            return screen.click(payload["x"], payload["y"])
        return {"ok": True, "action": payload.get("action")}

    mod.load_creds = load_creds
    mod.req = req
    mod.act = act
    sys.modules["pceye"] = mod
    return mod
