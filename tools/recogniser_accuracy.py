#!/usr/bin/env python3
"""Measure the board recogniser's accuracy against known positions.

The point of this file: before a clocked game, I need a number for how often the
bot reads a square wrong, not an impression. Each test position is pushed onto a
local lichess analysis board through its FEN box, read cold by the recogniser,
and compared square by square against the FEN we typed in.

Shape templates are learned from the start position only, then never retrained,
so a position is always read by a recogniser that has not seen it.
"""
import glob
import json
import os
import subprocess
import sys
import time

import chess
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "pceye"))
import lichess_bot as B  # noqa: E402

PCEYE = os.path.join(HERE, "..", "..", "pceye", "pceye.py")
SCALE = 1920 / 1568.0

# A FEN box click in rendered-preview coordinates (the analysis page layout).
FEN_BOX_RENDERED = (800, 760)

POSITIONS = [
    ("start", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
    ("open game", "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 6 6"),
    ("queens off, dark sq", "r3kb1r/pp1n1ppp/2p1pn2/q7/2BP4/2N1PN2/PP3PPP/R2QK2R w KQkq - 0 9"),
    ("queen on dark g3", "rnbqkb1r/pp1p1ppp/4pn2/2p5/P1P5/6Q1/1P1PPPPP/RNB1KBNR b KQkq - 1 4"),
    ("promoted pieces", "4k3/8/8/8/8/8/8/QQQQKQQQ w - - 0 1"),
    ("many knights", "n1n1k1n1/8/8/8/8/8/8/N1N1K1N1 w - - 0 1"),
    ("endgame kings", "8/8/4k3/8/8/3K4/8/8 w - - 0 1"),
    ("endgame pawns+d", "8/5p2/4k3/8/8/2K2P2/8/8 w - - 0 1"),
    ("dense middlegame", "r1bq1rk1/pp2ppbp/2np1np1/8/3NP3/2N1BP2/PPPQ2PP/R3KB1R w KQ - 3 8"),
    ("black heavy", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1"),
    ("all types dark", "rnbqkbnr/8/8/8/8/8/8/RNBQKBNR w - - 0 1"),
    ("castled both", "r4rk1/ppp2ppp/8/8/8/8/PPP2PPP/R4RK1 w - - 0 12"),
]


def click_native(x, y):
    subprocess.run([sys.executable, PCEYE, "click", str(int(x)), str(int(y)), "--nosettle"],
                   capture_output=True, text=True)


def set_fen(fen):
    rx, ry = FEN_BOX_RENDERED
    click_native(rx * SCALE, ry * SCALE)
    time.sleep(0.25)
    subprocess.run([sys.executable, PCEYE, "key", "ctrl+a", "--nosettle"], capture_output=True)
    time.sleep(0.12)
    subprocess.run([sys.executable, PCEYE, "paste", fen, "--nosettle"], capture_output=True)
    time.sleep(0.25)
    subprocess.run([sys.executable, PCEYE, "key", "enter"], capture_output=True)
    time.sleep(2.2)


def main():
    # ---- learn from the start position, on this fresh analysis board
    set_fen(chess.STARTING_FEN)
    path, im = B.grab_stable("learn")
    geo = B.find_board(im)
    rec = B.Recogniser()
    rec.learn(im, geo, chess.Board(), False)
    print(f"board {geo['bbox']}  learned templates for {sorted(rec.shapes)}\n")

    rows, total_sq, total_ok, exact_ok, exact_n = [], 0, 0, 0, 0
    for name, fen in POSITIONS:
        set_fen(fen)
        path, im = B.grab_stable(f"acc_{name.replace(' ', '_')}")
        geo2 = B.find_board_cached(im)
        detected_flip, read, detail = B.read_auto(rec, im, geo2)
        tgt = chess.Board(fen)
        bad = []
        for s in chess.SQUARES:
            want = tgt.piece_at(s).symbol() if tgt.piece_at(s) else "."
            got = detail[s][0]
            total_sq += 1
            if got == want:
                total_ok += 1
            else:
                bad.append({"sq": chess.square_name(s), "want": want, "got": got,
                            "square_is_dark": (chess.square_file(s) + chess.square_rank(s)) % 2 == 0})
        exact_n += 1
        exact = read.board_fen() == tgt.board_fen()
        exact_ok += 1 if exact else 0
        rows.append({"name": name, "fen": fen, "exact": exact, "errors": bad,
                     "read": read.board_fen(), "detected_flipped": detected_flip})
        print(f"{'PASS' if exact else 'FAIL'}  {name:22s} flip={str(detected_flip):5s} "
              f"({len(bad)} wrong square{'s' if len(bad)!=1 else ''})"
              + ("" if exact else "  " + ", ".join(f"{b['sq']}:{b['want']}->{b['got']}" for b in bad[:6])))

    print(f"\nsquare accuracy {total_ok}/{total_sq} = {100*total_ok/total_sq:.2f}%")
    print(f"exact positions {exact_ok}/{exact_n}")
    out = {"square_accuracy": round(total_ok / total_sq, 5),
           "exact_positions": f"{exact_ok}/{exact_n}", "positions": rows}
    with open(os.path.join(HERE, "..", "results", "recogniser_accuracy.json"), "w") as f:
        json.dump(out, f, indent=2)
    return 0 if exact_ok == exact_n else 1


if __name__ == "__main__":
    sys.exit(main())
