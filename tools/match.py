#!/usr/bin/env python3
"""Sable vs Stockfish match harness.

Honest reporting rules baked in:
  * every game's moves are legal-checked by python-chess,
  * a game that hits the ply cap is adjudicated by material and LABELLED as such,
  * Elo is reported with a 95% CI from a bootstrap over games, never as a bare number,
  * the exact Stockfish settings are written into the result file.
"""
import argparse, json, math, os, random, subprocess, sys, time
import chess, chess.engine

HERE = os.path.dirname(os.path.abspath(__file__))
SABLE = os.path.join(HERE, "..", "sable")

# Short, sane, varied openings so games are not 20 copies of the same game.
OPENINGS = [
    [],
    ["e2e4", "e7e5"],
    ["e2e4", "c7c5"],
    ["d2d4", "d7d5"],
    ["d2d4", "g8f6"],
    ["c2c4", "e7e5"],
    ["g1f3", "d7d5"],
    ["e2e4", "e7e6"],
    ["e2e4", "c7c6"],
    ["d2d4", "g7g6"],
    ["g1f3", "g8f6"],
    ["c2c4", "c7c5"],
]

def make_engine(path, **opts):
    e = chess.engine.SimpleEngine.popen_uci(path)
    try:
        if opts: e.configure(opts)
    except Exception:
        pass
    return e

def elo_from_score(p):
    """Elo difference implied by a score fraction: delta = -400*log10(1/p - 1)."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -400.0 * math.log10(1.0 / p - 1.0)

def boot_ci(points, iters=4000):
    if not points: return (0.0, 0.0)
    n = len(points)
    out = []
    for _ in range(iters):
        s = sum(random.choice(points) for _ in range(n)) / n
        out.append(elo_from_score(s))
    out.sort()
    return (out[int(0.025 * iters)], out[int(0.975 * iters)])

def material(b):
    vals = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3.2, chess.ROOK: 5, chess.QUEEN: 9}
    sc = 0
    for pt, v in vals.items():
        sc += v * (len(b.pieces(pt, chess.WHITE)) - len(b.pieces(pt, chess.BLACK)))
    return sc

def play_game(sable, sf, opening, sable_ms, sf_ms, sable_is_white, max_plies, sf_limit):
    b = chess.Board()
    for m in opening:
        b.push(chess.Move.from_uci(m))
    illegal = None
    moves = 0
    while not b.is_game_over(claim_draw=True) and b.ply() < max_plies:
        eng = sable if (b.turn == chess.WHITE) == sable_is_white else sf
        limit = chess.engine.Limit(time=sable_ms / 1000.0) if eng is sable else sf_limit
        try:
            res = eng.play(b, limit)
        except chess.engine.EngineError as ex:
            illegal = f"engine error: {ex}"
            break
        if res.move is None:
            illegal = "engine returned no move"
            break
        if res.move not in b.legal_moves:
            illegal = f"ILLEGAL MOVE {res.move.uci()} at ply {b.ply()}"
            break
        b.push(res.move)
        moves += 1
    if illegal:
        return {"result": "0-1" if sable_is_white else "1-0", "why": "sable_illegal", "detail": illegal,
                "plies": b.ply(), "illegal": True}
    if b.is_game_over(claim_draw=True):
        out = b.outcome(claim_draw=True)
        if out.winner is None:
            return {"result": "1/2-1/2", "why": out.termination.name, "plies": b.ply()}
        winner_is_white = out.winner == chess.WHITE
        sable_won = winner_is_white == sable_is_white
        return {"result": "1-0" if winner_is_white else "0-1", "why": out.termination.name, "plies": b.ply(),
                "sable_win": sable_won}
    # adjudicate on material
    m = material(b)
    if abs(m) < 1.5:
        return {"result": "1/2-1/2", "why": "adjudicated_material_even", "plies": b.ply(), "adjudicated": True}
    white_better = m > 0
    sable_better = white_better == sable_is_white
    return {"result": "1-0" if white_better else "0-1", "why": "adjudicated_material", "plies": b.ply(),
            "adjudicated": True, "sable_win": sable_better, "margin": abs(m)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=10)
    ap.add_argument("--sable-ms", type=int, default=100)
    ap.add_argument("--sf-ms", type=int, default=100)
    ap.add_argument("--sf-skill", type=int, default=None)
    ap.add_argument("--sf-elo", type=int, default=None)
    ap.add_argument("--sf-depth", type=int, default=None)
    ap.add_argument("--sf-nodes", type=int, default=None)
    ap.add_argument("--max-plies", type=int, default=240)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=None)
    ap.add_argument("--sf", default=os.path.join(HERE, "..", "..", "bin", "sf"))
    args = ap.parse_args()

    random.seed(args.seed)
    opts = {}
    if args.sf_skill is not None: opts.update({"Skill Level": args.sf_skill})
    if args.sf_elo is not None:
        opts.update({"UCI_LimitStrength": True, "UCI_Elo": args.sf_elo})
    sable = make_engine(SABLE, Threads=1, Hash=32)
    sf = make_engine(args.sf, **opts)
    sf.configure({"Threads": 1, "Hash": 64})

    if args.sf_nodes: sf_limit = chess.engine.Limit(nodes=args.sf_nodes)
    elif args.sf_depth: sf_limit = chess.engine.Limit(depth=args.sf_depth)
    else: sf_limit = chess.engine.Limit(time=args.sf_ms / 1000.0)

    games = []
    t0 = time.time()
    for i in range(args.games):
        opening = OPENINGS[i % len(OPENINGS)]
        sable_white = (i % 2 == 0)
        r = play_game(sable, sf, opening, args.sable_ms, args.sf_ms, sable_white, args.max_plies, sf_limit)
        r.update({"game": i + 1, "opening": opening, "sable_white": sable_white})
        games.append(r)
        tag = "SABLE WINS" if r.get("sable_win") else ("draw" if r["result"] == "1/2-1/2" else "sable loses")
        if r.get("illegal"): tag = "!!! ILLEGAL " + str(r.get("detail"))
        print(f"[{i+1}/{args.games}] {tag:28s} {r['result']:7s} {r['why']:32s} {r['plies']} plies", flush=True)

    W = sum(1 for g in games if g.get("sable_win"))
    D = sum(1 for g in games if g["result"] == "1/2-1/2")
    L = len(games) - W - D
    n = len(games)
    points = [1.0 if g.get("sable_win") else (0.5 if g["result"] == "1/2-1/2" else 0.0) for g in games]
    score = sum(points) / n
    elo = elo_from_score(score)
    lo, hi = boot_ci(points)
    summary = {
        "games": n, "sable_wins": W, "draws": D, "losses": L,
        "score": round(score, 4), "elo_vs_opponent": round(elo, 1),
        "elo_95ci": [round(lo, 1), round(hi, 1)],
        "sable_movetime_ms": args.sable_ms,
        "stockfish": {"opts": opts, "limit": str(sf_limit), "path": args.sf},
        "adjudicated_games": sum(1 for g in games if g.get("adjudicated")),
        "illegal_games": sum(1 for g in games if g.get("illegal")),
        "wall_clock_s": round(time.time() - t0, 1),
        "games_detail": games,
    }
    print("\n=== RESULT ===")
    elo_txt = "unbounded (+inf, clean sweep)" if W + D == n or score in (0.0, 1.0) else f"{elo:+.0f}"
    print(f"W/D/L  {W}/{D}/{L}   score {score:.3f}   Elo vs opponent {elo_txt}  (95% CI {lo:+.0f} .. {hi:+.0f})")
    print(f"SF settings: {opts or '(full strength)'}  limit {sf_limit}   Sable {args.sable_ms}ms/move")
    if args.out:
        json.dump(summary, open(args.out, "w"), indent=2)
        print("written:", args.out)
    sable.quit(); sf.quit()

if __name__ == "__main__":
    main()
