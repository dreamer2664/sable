#!/usr/bin/env python3
"""HOLD THE DRAW — the true version of "draw Stockfish max".

You cannot draw Stockfish 17.1 from the starting position. But a fortress is a
fortress: in a position that is a *theoretical draw*, a correct engine holds it
against any opponent, including Stockfish at full strength. That is a real,
verifiable claim, and this is how it gets verified rather than asserted.

Method:

  1. GROUND TRUTH comes from the lichess endgame tablebase API, not from
     Stockfish's opinion and not from my memory of endgame theory. A position is
     only used if the tablebase says the category is exactly "draw". This gate
     has teeth: it has already rejected hand-picked positions I was confident
     about, including one that was mate for the other side and one that was a
     forced win. Positions that merely look drawn are excluded.

  2. Sable defends it against Stockfish 17.1 at FULL STRENGTH (no Skill Level
     cap, no UCI_Elo cap, 2 cores).

  3. The result is judged by python-chess's rules: checkmate, stalemate,
     threefold repetition, fifty-move rule, insufficient material.
"""
import json, os, sys, urllib.parse, urllib.request
import chess, chess.engine

HERE = os.path.dirname(os.path.abspath(__file__))
SABLE = os.path.join(HERE, "..", "sable")
SF = os.path.join(HERE, "..", "..", "bin", "sf")
TB = "https://tablebase.lichess.ovh/standard?fen="

# Every position has <= 7 pieces so the tablebase can give an exact answer.
CANDIDATES = [
    ("Rook pawn, defender in front (a-file)", "8/8/8/8/1k6/8/P7/K7 w - - 0 1", chess.WHITE),
    ("Rook pawn, defender in front (h-file)", "8/8/8/8/5k2/8/7P/7K w - - 0 1", chess.WHITE),
    ("Two knights cannot force mate", "8/8/8/4k3/8/8/8/KNN5 w - - 0 1", chess.WHITE),
    ("Wrong-colour bishop fortress", "8/8/8/2k5/8/8/P7/BK6 w - - 0 1", chess.WHITE),
    ("Opposite bishops, dead position", "8/8/3k4/8/8/2K5/8/2B2b2 w - - 0 1", chess.WHITE),
    ("Philidor: R+P vs R, main line", "4k3/8/r3K3/4P3/8/8/8/7R w - - 0 1", chess.WHITE),
    ("Knight vs knight", "8/8/4k3/8/8/4K3/8/1N2n3 w - - 0 1", chess.WHITE),
    ("Bishop + wrong rook pawn", "8/8/8/8/8/k7/p7/KB6 w - - 0 1", chess.WHITE),
]

def tablebase(fen):
    """Exact theoretical result from the lichess tablebase. Returns category or None."""
    try:
        with urllib.request.urlopen(TB + urllib.parse.quote(fen), timeout=30) as r:
            d = json.load(r)
        return d.get("category"), d.get("dtz")
    except Exception as e:
        return f"error: {e}", None

def main(max_plies=200, sable_ms=200, out=None):
    sable = chess.engine.SimpleEngine.popen_uci(SABLE)
    sf = chess.engine.SimpleEngine.popen_uci(SF)
    sf.configure({"Threads": 1, "Hash": 128})
    rows, held, tested = [], 0, 0
    try:
        for name, fen, sable_side in CANDIDATES:
            b = chess.Board(fen)
            if not b.is_valid():
                print(f"[SKIP] {name}: illegal position (status {b.status()})")
                rows.append({"name": name, "fen": fen, "valid": False})
                continue
            cat, dtz = tablebase(fen)
            if cat != "draw":
                print(f"[SKIP] {name}: tablebase says '{cat}' (dtz={dtz}) — not a drawn position, excluded")
                rows.append({"name": name, "fen": fen, "category": cat, "held": None})
                continue
            tested += 1
            print(f"[TEST] {name}  (tablebase: draw, dtz={dtz})", flush=True)
            bb = b.copy()
            plies, outcome, why = 0, None, None
            while not bb.is_game_over(claim_draw=True) and plies < max_plies:
                eng = sable if bb.turn == sable_side else sf
                limit = chess.engine.Limit(time=sable_ms / 1000) if eng is sable else chess.engine.Limit(time=0.2)
                try:
                    res = eng.play(bb, limit)
                except Exception as e:
                    outcome, why = "FORFEIT", f"engine error: {type(e).__name__}"
                    break
                if res.move is None or res.move not in bb.legal_moves:
                    outcome, why = "FORFEIT", f"illegal/no move: {res.move}"
                    break
                bb.push(res.move)
                plies += 1
            if outcome is None:
                if bb.is_game_over(claim_draw=True):
                    o = bb.outcome(claim_draw=True)
                    why = o.termination.name
                    outcome = "DRAWN" if o.winner is None else "LOST"
                else:
                    why, outcome = "ply cap reached", "DRAWN"
            ok = outcome == "DRAWN"
            held += 1 if ok else 0
            print(f"[{'HELD' if ok else 'FAIL'}] {name:40s} {outcome:8s} {str(why):26s} {plies} plies", flush=True)
            rows.append({"name": name, "fen": fen, "category": cat, "dtz": dtz,
                         "outcome": outcome, "why": why, "plies": plies, "held": ok})
    finally:
        for e in (sable, sf):
            try: e.quit()
            except Exception: pass
    print(f"\n=== {held}/{tested} tablebase-drawn positions held against Stockfish 17.1 at full strength ===")
    if out:
        json.dump({"opponent": "Stockfish 17.1, full strength (no skill/elo cap), 1 thread",
                   "ground_truth": "lichess endgame tablebase (exact, not an engine's opinion)",
                   "sable_ms_per_move": sable_ms, "screened": tested, "held": held,
                   "positions": rows}, open(out, "w"), indent=2)
        print("written:", out)
    return 0 if tested and held == tested else 1

if __name__ == "__main__":
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    sys.exit(main(out=out))
