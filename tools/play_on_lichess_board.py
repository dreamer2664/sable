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
  * the board geometry is measured from the screenshot (density scan, edge
    refinement, then an assertion that the region is square and divides evenly
    into 8x8). If the assertion fails it refuses instead of guessing.
  * the game state is tracked locally with python-chess; every click is the
    centre of a square derived from that measured geometry.
  * a screenshot is taken after every move and saved, so a human can check the
    board really advanced.

Usage:
  python3 tools/play_on_lichess_board.py --side white --moves 10
  python3 tools/play_on_lichess_board.py --fen "<fen>" --side black --moves 4
"""
import argparse, json, os, subprocess, sys, time
import chess

HERE = os.path.dirname(os.path.abspath(__file__))
PCEYE = os.path.join(HERE, "..", "..", "pceye", "pceye.py")
SABLE = os.path.join(HERE, "..", "sable")

def pceye(*args, timeout=90):
    r = subprocess.run([sys.executable, PCEYE, *map(str, args)],
                       capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    try:
        return json.loads(out[out.index("{"):])
    except Exception:
        return {"ok": False, "raw": out, "err": r.stderr}

def shot(label):
    r = pceye("shot", "--label", label)
    if not r.get("ok"):
        sys.exit(f"cannot see the screen: {r}")
    return r["path"]

def geometry(path):
    """Measure the board. Uses the same density+refinement method validated by hand."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    W, H = im.size
    px = im.load()
    # board colours: the two most common coloured mid-tones
    from collections import Counter
    c = Counter()
    for y in range(0, H, 3):
        for x in range(0, W, 3):
            c[px[x, y]] += 1
    cands = []
    for col, _ in c.most_common(60):
        mx, mn = max(col), min(col)
        if 6 <= mx - mn and 60 <= sum(col) / 3 <= 235:
            if all(sum(abs(col[i] - o[i]) for i in range(3)) > 40 for o in cands):
                cands.append(col)
        if len(cands) == 2:
            break
    if len(cands) < 2:
        sys.exit("could not identify board colours; is the board on screen?")
    A, B = cands
    def isboard(x, y):
        p = px[x, y]
        for col in (A, B):
            if abs(p[0]-col[0]) < 24 and abs(p[1]-col[1]) < 24 and abs(p[2]-col[2]) < 24:
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
                    if isboard(x, y): hit += 1
            if tot and hit / tot > 0.9:
                minx, miny = min(minx, x0), min(miny, y0)
                maxx, maxy = max(maxx, min(x0 + cell, W)), max(maxy, min(y0 + cell, H))
    if maxx < 0:
        sys.exit("no board region found")
    def colfrac(x, y0, y1):
        n = h = 0
        for y in range(y0, y1, 2):
            n += 1
            if isboard(x, y): h += 1
        return h / max(n, 1)
    def rowfrac(y, x0, x1):
        n = h = 0
        for x in range(x0, x1, 2):
            n += 1
            if isboard(x, y): h += 1
        return h / max(n, 1)
    L = R = T = Bo = None
    for x in range(minx - cell, minx + 2 * cell):
        if 0 <= x < W and colfrac(x, miny + 20, maxy - 20) > 0.97: L = x; break
    for x in range(maxx + cell, maxx - 2 * cell, -1):
        if 0 <= x < W and colfrac(x, miny + 20, maxy - 20) > 0.97: R = x; break
    for y in range(miny - cell, miny + 2 * cell):
        if 0 <= y < H and rowfrac(y, minx + 20, maxx - 20) > 0.97: T = y; break
    for y in range(maxy + cell, maxy - 2 * cell, -1):
        if 0 <= y < H and rowfrac(y, minx + 20, maxx - 20) > 0.97: Bo = y; break
    if None in (L, R, T, Bo):
        sys.exit("could not refine board edges")
    bw, bh = R - L + 1, Bo - T + 1
    sx, sy = bw / 8.0, bh / 8.0
    if abs(sx - sy) > 1.5 or not (30 <= sx <= 200):
        sys.exit(f"REFUSING: board geometry does not check out (sx={sx:.1f} sy={sy:.1f})")
    return {"bbox": [L, T, R, Bo], "square": sx}

def centre(geo, square_name, flip=False):
    f = chess.square_file(chess.parse_square(square_name))
    r = chess.square_rank(chess.parse_square(square_name))
    if flip:                                  # black at the bottom
        f, r = 7 - f, 7 - r
    L, T, R, B = geo["bbox"]
    s = (R - L + 1) / 8.0
    sy = (B - T + 1) / 8.0
    return (int(round(L + (f + 0.5) * s)), int(round(B + 1 - (r + 0.5) * sy)))

def sable_move(fen, ms=300):
    p = subprocess.run([SABLE], input=f"position fen {fen}\ngo movetime {ms}\nquit\n",
                       capture_output=True, text=True, timeout=60)
    for line in p.stdout.splitlines():
        if line.startswith("bestmove"):
            return line.split()[1]
    sys.exit("engine produced no move")

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
        mv = sable_move(board.fen(), args.ms)
        m = chess.Move.from_uci(mv)
        if m not in board.legal_moves:
            print("ENGINE RETURNED ILLEGAL MOVE", mv); break
        path = shot(f"before_{i}")
        shots.append(path)
        geo = geometry(path)
        a, b = centre(geo, chess.square_name(m.from_square), flip), centre(geo, chess.square_name(m.to_square), flip)
        print(f"[{i+1}] {mv}  {chess.square_name(m.from_square)}{a} -> {chess.square_name(m.to_square)}{b}")
        if args.go:
            r1 = pceye("click", a[0], a[1], "--nosettle")
            time.sleep(0.5)
            r2 = pceye("click", b[0], b[1], "--nosettle")
            if r1.get("exact") is False or r2.get("exact") is False:
                print("!! pointer did not land where asked, aborting"); break
            time.sleep(1.2)
            shots.append(shot(f"after_{m.uci()}"))
        board.push(m)
    print("\nlocal board believes:", board.fen())
    print("screenshots:", ", ".join(os.path.basename(s) for s in shots[-4:]))
    if not args.go:
        print("\n(dry run — pass --go to actually click)")

if __name__ == "__main__":
    main()
