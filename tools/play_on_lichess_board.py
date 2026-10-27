#!/usr/bin/env python3
"""Play Sable's moves on a LOCAL lichess analysis board, through pc-eye.

OFFLINE ONLY. This drives https://lichess.org/analysis — an analysis board where
the "opponent" is Stockfish running inside your own browser. It never contacts
matchmaking, never joins a game, and never needs an account or a game id.

Playing *online* games with engine assistance violates lichess's terms of
service. If you point this at a game with a human or an online bot opponent you
are cheating, and the authors of this file will not help you do it. There is
deliberately no game-join code here.

How it stays honest:
  * the board geometry comes from boardvision, the same detector the clocked
    games use (238/238 real frames, within 1px). It asserts the region is square
    and divides evenly into 8x8, and refuses instead of guessing.
  * the game state is tracked locally with python-chess; every click is the
    centre of a square derived from that measured geometry.
  * a screenshot is taken after every move and saved, so a human can check the
    board really advanced.

Usage:
  python3 tools/play_on_lichess_board.py --side white --moves 10
  python3 tools/play_on_lichess_board.py --fen "<fen>" --side black --moves 4
"""
import argparse, os, sys
import chess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import boardvision as V          # noqa: E402
import lichess_bot as B          # noqa: E402
import pceye_client as pceye     # noqa: E402


def shot(label):
    return pceye.shot(label=label)["path"]


def geometry(path):
    """Measure the board in `path`. One implementation, shared with the bot."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    geo = V.find_board(im)
    L, T, R, Bo = geo["bbox"]
    sx, sy = (R - L + 1) / 8.0, (Bo - T + 1) / 8.0
    if abs(sx - sy) > 1.5 or not (30 <= sx <= 200):
        sys.exit(f"REFUSING: board geometry does not check out (sx={sx:.1f} sy={sy:.1f})")
    return geo


def centre(geo, square_name, flip=False):
    sq = chess.parse_square(square_name)
    return V.square_centre(geo, chess.square_file(sq), chess.square_rank(sq), flip)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fen", default=chess.STARTING_FEN)
    ap.add_argument("--side", choices=["white", "black"], default="white",
                    help="which side you are playing ON THE BOARD (the analysis board is oriented to it)")
    ap.add_argument("--moves", type=int, default=6)
    ap.add_argument("--ms", type=int, default=300)
    ap.add_argument("--go", action="store_true", help="actually click (default is a dry run)")
    args = ap.parse_args()

    flip = args.side == "black"
    board = chess.Board(args.fen)
    print(f"OFFLINE analysis board only. side={args.side} dry_run={not args.go}")
    shots = []
    for i in range(args.moves):
        if board.is_game_over():
            print("game over:", board.result()); break
        mv = B.sable_best(board.fen(), args.ms)
        if not mv:
            print("engine produced no move"); break
        m = chess.Move.from_uci(mv)
        if m not in board.legal_moves:
            print("ENGINE RETURNED ILLEGAL MOVE", mv); break
        path = shot(f"before_{i}")
        shots.append(path)
        geo = geometry(path)
        a = centre(geo, chess.square_name(m.from_square), flip)
        b = centre(geo, chess.square_name(m.to_square), flip)
        print(f"[{i+1}] {mv}  {chess.square_name(m.from_square)}{a} -> {chess.square_name(m.to_square)}{b}")
        if args.go:
            r1 = pceye.click(a[0], a[1])
            r2 = pceye.click(b[0], b[1])
            if r1.get("exact") is False or r2.get("exact") is False:
                print("!! pointer did not land where asked, aborting"); break
            shots.append(shot(f"after_{m.uci()}"))
        board.push(m)
    print("\nlocal board believes:", board.fen())
    print("screenshots:", ", ".join(os.path.basename(s) for s in shots[-4:]))
    if not args.go:
        print("\n(dry run - pass --go to actually click)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
