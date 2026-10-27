#!/usr/bin/env python3
"""Run Sable up the lichess computer ladder: level 1, 2, 3 ... toward 8.

What "learning" means here, stated plainly so the claim matches the code:

  * OPENING BOOK. After every game where Sable scored, the opening moves from
    that game are appended to `book.txt`. The engine plays book moves instantly,
    which both avoids repeating known-bad openings and preserves clock in fast
    time controls. This is real, checkable accumulation across games -- the book
    file grows and the engine's early play changes.
  * HARNESS. Every rung re-verifies the UI state before starting, so a result is
    never mislabelled.

It is NOT a neural network learning from losses. Sable's search and evaluation
are fixed; the ladder tells you where they are good enough and where they stop.

Safety properties, because this drives a real UI:
  * the strength level is VERIFIED by finding the green highlighted button, not
    assumed from the click we sent;
  * if verification fails, the game is not started;
  * every game records its result, its move list, and a screenshot.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import chess
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get("SABLE_PCEYE", os.path.join(HERE, "..", "..", "pceye")))
import lichess_bot as B  # noqa: E402

# overridable so a launcher can point at wherever pc-eye actually lives
PCEYE_DIR = os.environ.get("SABLE_PCEYE", os.path.join(HERE, "..", "..", "pceye"))
PCEYE = os.path.join(PCEYE_DIR, "pceye.py")
RESULTS = os.path.join(HERE, "..", "results")
BOOK = os.path.join(HERE, "..", "book.txt")
HISTORY = os.path.join(RESULTS, "ladder_history.json")

# --- native screen coordinates of the lichess "Game setup" dialog -------------
# Measured from labelled fine-grid screenshots, not guessed.
HOME_COMPUTER_BTN = (1647, 627)
TAB_REALTIME = (955, 341)
INPUT_MINUTES = (947, 404)
INPUT_INCREMENT = (1017, 404)
LEVEL_Y = 576
CHIP_Y = 470
CHIP_X0 = 702      # centre of the "1+0" preset
CHIP_DX = 51.1     # spacing between presets
# order of the preset chips on the lichess dialog
TC_TO_CHIP = {(1, 0): 0, (2, 1): 1, (3, 0): 2, (3, 2): 3, (5, 0): 4,
              (5, 3): 5, (10, 0): 6, (10, 5): 7, (15, 10): 8, (30, 0): 9, (30, 20): 10}
LEVEL_X1 = 697
LEVEL_DX = 85.3
SIDE_WHITE = (737, 722)
SIDE_BLACK = (1013, 722)
START = (986, 830)

LICHESS_GREEN = np.array([129, 168, 76], dtype=np.float32)


def sh(*args, **kw):
    return subprocess.run([sys.executable, PCEYE, *map(str, args)],
                          capture_output=True, text=True, **kw)


def click(x, y, settle=0.5):
    sh("click", int(x), int(y), "--nosettle")
    time.sleep(settle)


def set_input(x, y, text):
    click(x, y, 0.35)
    sh("key", "ctrl+a", "--nosettle")
    time.sleep(0.2)
    sh("paste", text, "--nosettle")
    time.sleep(0.35)


def screenshot(label):
    path, im = B.grab(label)
    return path, im


def level_button_x(level):
    return int(round(LEVEL_X1 + LEVEL_DX * (level - 1)))


def detect_highlighted_level(im):
    """Which strength button is green right now? Returns level or None."""
    a = np.asarray(im, dtype=np.float32)
    scores = {}
    for lv in range(1, 9):
        x = level_button_x(lv)
        patch = a[LEVEL_Y - 8:LEVEL_Y + 8, x - 25:x + 25]
        d = float(np.abs(patch - LICHESS_GREEN).mean())
        scores[lv] = d
    best = min(scores, key=scores.get)
    if scores[best] < 60.0:            # clearly green
        return best
    return None


def green_center(im, y, x0=620, x1=1330, band=8):
    """Centre x of the lichess-green segment in a horizontal band, or None."""
    a = np.asarray(im, dtype=np.float32)
    h, w, _ = a.shape
    y0, y1 = max(0, y - band), min(h, y + band)
    sub = a[y0:y1, x0:x1]
    d = np.abs(sub - LICHESS_GREEN).mean(axis=2)
    xs = np.where(d < 55.0)[1]
    if xs.size < 12:
        return None
    return float(np.median(xs)) + x0


def is_green_at(im, x, y, rx=24, ry=9):
    """Is lichess-green present in a small box around (x, y)?

    A local test, deliberately: scanning the whole row catches other green
    furniture (the active tab, the level button) and reports a false negative for
    a chip that is in fact correctly selected.
    """
    a = np.asarray(im, dtype=np.float32)
    h, w, _ = a.shape
    sub = a[max(0, y - ry):min(h, y + ry), max(0, x - rx):min(w, x + rx)]
    if sub.size == 0:
        return False
    d = np.abs(sub - LICHESS_GREEN).mean(axis=2)
    return float((d < 55.0).mean()) > 0.25


def find_chips(im, y, x0=620, x1=1330, band=7):
    """Detect the preset chips in their row and return their centre x positions.

    Reading them out of the pixels beats typing into the minutes/increment
    fields: a click that quietly failed left the game at 10+0 while the ladder
    believed it was running 1+0. The chips are also a single click for both
    numbers, so there is less to get wrong.
    """
    a = np.asarray(im, dtype=np.float32)
    h, w, _ = a.shape
    y0, y1 = max(0, y - band), min(h, y + band)
    strip = a[y0:y1, x0:x1].mean(axis=(0, 2))          # per-column brightness
    bg = float(np.median(strip))
    ischip = np.abs(strip - bg) > 4.0
    runs, start = [], None
    for i, v in enumerate(ischip):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= 14:
                runs.append((start, i))
            start = None
    if start is not None and len(ischip) - start >= 14:
        runs.append((start, len(ischip)))
    return [int((a_ + b_) / 2 + x0) for a_, b_ in runs]


def glyph_count(im, box):
    """Rough glyph count inside a box, used to confirm the minutes/increment inputs."""
    x0, y0, x1, y1 = box
    a = np.asarray(im.convert("L").crop(box), dtype=np.float32)
    if a.size == 0:
        return 0
    fg = a > (a.max() * 0.55)          # light text on dark input
    cols = fg.any(axis=0)
    runs = 0
    prev = False
    for c in cols:
        if c and not prev:
            runs += 1
        prev = c
    return runs


def dialog_open(im=None):
    """Is the Game setup modal on screen? A time-control chip is always
    highlighted, so green in the chip band is a reliable, OCR-free signal."""
    if im is None:
        _, im = screenshot("dialogcheck")
    a = np.asarray(im, dtype=np.float32)
    sub = a[CHIP_Y - 16:CHIP_Y + 16, 640:1160]
    d = np.abs(sub - LICHESS_GREEN).mean(axis=2)
    return int((d < 60).sum()) > 200


def open_computer_dialog(attempts=3):
    """Open the setup modal and PROVE it opened before touching anything in it.
    A blind click on a still-rendering homepage left the driver clicking through a
    dialog that was never there, and every later step landed on the page behind."""
    for i in range(attempts):
        click(*HOME_COMPUTER_BTN, settle=2.0)
        for _ in range(8):
            if dialog_open():
                return True
            time.sleep(0.5)
        # not open: give the page more time and try again
        time.sleep(2.0 + i * 2.0)
    return False


def open_setup(level, minutes, increment, side="white"):
    """Drive the setup dialog. Returns (ok, detail)."""
    sh("key", "ctrl+l", "--nosettle"); time.sleep(0.4)
    sh("paste", "https://lichess.org/", "--nosettle"); time.sleep(0.4)
    sh("key", "enter"); time.sleep(8)
    if not open_computer_dialog():
        return False, {"error": "Game setup dialog did not open"}
    click(*TAB_REALTIME, settle=1.2)

    # time control via the preset chip, then verified from the pixels
    path0, im0 = screenshot(f"chips_L{level}")
    # Chip row geometry measured from verified clicks, not guessed:
    # 1+0 sits at x=702 and 10+5 at x=1060, which fixes a spacing of 51.1px for
    # the 11 presets. Each click is then VERIFIED by finding the green highlight.
    chips = [int(round(CHIP_X0 + CHIP_DX * i)) for i in range(11)]
    idx = TC_TO_CHIP.get((minutes, increment))
    if idx is None:
        return False, {"error": f"time control {minutes}+{increment} has no preset chip"}
    click(chips[idx], CHIP_Y, settle=1.0)
    path1, im1 = screenshot(f"chipsel_L{level}")
    chip_ok = is_green_at(im1, chips[idx], CHIP_Y)
    click(level_button_x(level), LEVEL_Y, settle=1.0)
    click(*(SIDE_WHITE if side == "white" else SIDE_BLACK), settle=0.8)

    path, im = screenshot(f"setup_L{level}")
    got = detect_highlighted_level(im)
    gcx = green_center(im, LEVEL_Y)
    return (got == level and chip_ok), {
        "detected_level": got, "wanted_level": level,
        "chip_ok": chip_ok, "time_control": f"{minutes}+{increment}",
        "chips_found": chips, "chosen_chip": chips[idx] if idx < len(chips) else None,
        "setup_shot": path}


def start_game():
    click(*START, settle=0.0)


def append_to_history(rec):
    os.makedirs(RESULTS, exist_ok=True)
    hist = []
    if os.path.exists(HISTORY):
        try:
            hist = json.load(open(HISTORY))
        except Exception:
            hist = []
    hist.append(rec)
    with open(HISTORY, "w") as f:
        json.dump(hist, f, indent=2)


def add_book_lines(moves, plies=12, sable_white=None):
    """Store the opening of a game we scored in -- via learn.add_book, not a copy.

    This function used to be a second implementation of the same idea, and the two
    drifted: this one wrote the OPPONENT's moves as well (which the engine can
    never look up, because it only consults the book when it is to move), skipped
    the legality check, and had no quality gate at all. Delegating means there is
    one rule about what earns a place in the book.
    """
    try:
        import learn as LEARN
        r = LEARN.add_book(moves, plies=plies, sable_white=sable_white)
        return r["added"]
    except Exception as e:
        print(f"  !! book learning failed: {e}")
        return 0


def play_rung(level, ms, max_seconds, timeouts=(3, 2), side="white"):
    """Play `timeouts` games at this level. Returns the summary for the rung."""
    out = {"level": level, "games": []}
    for gi in range(timeouts[0]):
        minutes, increment = timeouts[1] if isinstance(timeouts[1], tuple) else (1, 0)
        ok, detail = open_setup(level, minutes, increment, side)
        if not ok:
            out["games"].append({"error": "setup verification failed", **detail})
            return out
        start_game()
        log = os.path.join(RESULTS, f"g_L{level}_{gi+1}.json")
        r = B.play_game(chess.WHITE if side == "white" else chess.BLACK,
                        engine_ms=ms, max_seconds=max_seconds,
                        clock_ms=minutes * 60_000,
                        log_path=log, record_dir=os.path.join(RESULTS, f"L{level}_{gi+1}"))
        board = r["final_board"]
        result = board.result(claim_draw=True) if board.is_game_over(claim_draw=True) else "ongoing"
        moves = [m.uci() for m in board.move_stack]
        scored = ((result == "1-0" and side == "white") or
                  (result == "0-1" and side == "black"))
        drew = result == "1/2-1/2"
        added = (add_book_lines(moves, sable_white=(side == "white"))
                 if (scored or drew) else 0)
        rec = {"level": level, "game": gi + 1, "result": result, "moves": len(moves),
               "plies": board.ply(), "elapsed_s": r["elapsed_s"],
               "sable_scored": scored or drew, "book_lines_added": added,
               "final_fen": board.fen(), "log": log}
        out["games"].append(rec)
        append_to_history(rec)
        print(f"  level {level} game {gi+1}: {result}  ({len(moves)} moves, "
              f"{r['elapsed_s']}s, book +{added})", flush=True)
    wins = sum(1 for g in out["games"] if g.get("sable_scored"))
    out["wins_or_draws"] = wins
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start_level", type=int, default=1)
    ap.add_argument("--to", dest="end_level", type=int, default=8)
    ap.add_argument("--games", type=int, default=1)
    ap.add_argument("--ms", type=int, default=200)
    ap.add_argument("--max-seconds", type=int, default=150)
    ap.add_argument("--minutes", type=int, default=1)
    ap.add_argument("--increment", type=int, default=0)
    a = ap.parse_args()

    summary = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "time_control": f"{a.minutes}+{a.increment}", "rungs": []}
    for level in range(a.start_level, a.end_level + 1):
        print(f"=== LADDER: lichess computer level {level} "
              f"({a.minutes}+{a.increment}) ===", flush=True)
        rung = play_rung(level, a.ms, a.max_seconds,
                         timeouts=(a.games, (a.minutes, a.increment)))
        summary["rungs"].append(rung)
        got = rung.get("wins_or_draws", 0)
        print(f"  -> level {level}: {got}/{a.games} scored", flush=True)
        if rung.get("games") and rung["games"][0].get("error"):
            print("  setup verification failed, stopping the ladder", flush=True)
            break
    outp = os.path.join(RESULTS, "ladder_summary.json")
    with open(outp, "w") as f:
        json.dump(summary, f, indent=2)
    print("\nwritten:", outp)


if __name__ == "__main__":
    main()
