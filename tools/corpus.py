#!/usr/bin/env python3
"""Generate a training corpus with Stockfish, without a browser in the loop.

Why this exists: the mouse-driven harness costs ~0.75s of clicking per move, so it
yields one game every 2-4 minutes and tells us as much about my clicking as about
the engine. Here Stockfish plays itself -- full strength on one side, a
replication of a lichess computer level on the other -- at whatever speed the CPU
allows. That gives two things:

  * `pairs.jsonl`  position + Stockfish's move, i.e. supervised examples of strong
                   play. This is the artifact that could actually improve Sable.
  * `games.pgn`    complete games, for opening-book mining and for checking how
                   the sparring level behaves.

The sparring levels mirror lichess's own settings (fishnet source):
  1..8 -> skill [-9,-5,-1,3,7,11,16,20], depth [5,5,5,5,5,8,13,22],
          movetime [50,100,150,200,300,400,500,1000] ms
so "spar 6" here is the same opponent Sable faces on lichess level 6.
"""
import argparse
import json
import os
import sys
import time

import chess
import chess.pgn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lichess_bot as B          # noqa: E402  (reuses the hang-free UCI client)

STRONG = "/home/user/bin/sf"

LVL_SKILL = [-9, -5, -1, 3, 7, 11, 16, 20]
LVL_DEPTH = [5, 5, 5, 5, 5, 8, 13, 22]
LVL_TIME = [50, 100, 150, 200, 300, 400, 500, 1000]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=10)
    ap.add_argument("--spar-level", type=int, default=6)
    ap.add_argument("--strong-ms", type=int, default=120)
    ap.add_argument("--max-plies", type=int, default=220)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "results", "corpus"))
    a = ap.parse_args()

    if not os.access(STRONG, os.X_OK):
        B._ensure_executable(STRONG)
    if not os.access(STRONG, os.X_OK):
        sys.exit(f"stockfish not executable: {STRONG}")

    lvl = max(1, min(8, a.spar_level)) - 1
    spar_opts = {
        "Skill Level": max(0, LVL_SKILL[lvl]),
        "UCI_LimitStrength": "false",
    }
    spar_ms = LVL_TIME[lvl]

    strong = B._UciEngine(STRONG, {})
    spar = B._UciEngine(STRONG, spar_opts)

    os.makedirs(a.out, exist_ok=True)
    pairs_path = os.path.join(a.out, "pairs.jsonl")
    pgn_path = os.path.join(a.out, "games.pgn")

    print(f"[corpus] strong={a.strong_ms}ms vs spar level {a.spar_level} "
          f"(skill {spar_opts['Skill Level']}, {spar_ms}ms)", flush=True)

    pairs = 0
    results = {"1-0": 0, "0-1": 0, "1/2-1/2": 0, "*": 0}
    t0 = time.time()
    with open(pairs_path, "a") as pf, open(pgn_path, "a") as gf:
        for g in range(1, a.games + 1):
            board = chess.Board()
            game = chess.pgn.Game()
            game.headers.update({
                "Event": "Sable training corpus",
                "Site": "offline self-play",
                "Date": time.strftime("%Y.%m.%d", time.gmtime()),
                "White": "Stockfish-max",
                "Black": f"Stockfish-spar-L{a.spar_level}",
            })
            node = game
            # alternate colours so the corpus is not all one side of the book
            strong_is_white = (g % 2 == 1)
            game.headers["White"] = "Stockfish-max" if strong_is_white else f"Stockfish-spar-L{a.spar_level}"
            game.headers["Black"] = f"Stockfish-spar-L{a.spar_level}" if strong_is_white else "Stockfish-max"

            while not board.is_game_over(claim_draw=True) and board.ply() < a.max_plies:
                mine_strong = (board.turn == chess.WHITE) == strong_is_white
                if mine_strong:
                    uci = strong.best(board.fen(), a.strong_ms)
                else:
                    uci = spar.best(board.fen(), spar_ms)
                try:
                    mv = chess.Move.from_uci(uci)
                except Exception:
                    break
                if mv not in board.legal_moves:
                    print(f"  [warn] illegal {uci} at ply {board.ply()}", flush=True)
                    break
                if mine_strong:
                    # the learning pair: this position, and what full strength chose
                    pf.write(json.dumps({"fen": board.fen(), "best": uci,
                                         "ply": board.ply()}) + "\n")
                    pairs += 1
                board.push(mv)
                node = node.add_variation(mv)

            res = board.result(claim_draw=True) if board.is_game_over(claim_draw=True) else "*"
            results[res] = results.get(res, 0) + 1
            game.headers["Result"] = res
            gf.write(str(game) + "\n\n")
            gf.flush()
            print(f"  game {g}: {res} plies={board.ply()} pairs={pairs} "
                  f"({time.time()-t0:.0f}s)", flush=True)

    strong.close()
    spar.close()
    print(f"[corpus] done: {results} pairs={pairs} -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
