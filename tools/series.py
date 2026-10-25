#!/usr/bin/env python3
"""Play a series of real 3+0 games against the lichess computer at one level.

"Real game" here means a complete game at the stated time control, played from
the lichess "Play against computer" dialog, finished to an actual result and
cross-checked against the screen -- not a test position.

Every game is logged; wins feed the opening book, and mistakes feed avoid.txt.
Progress is appended to results/series_<level>.json after each game so the run
can be inspected while it is going on.
"""
import argparse
import json
import os
import sys
import time

import chess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "pceye"))
import ladder as L          # noqa: E402
import lichess_bot as B     # noqa: E402
import learn as LEARN       # noqa: E402

RESULTS = os.path.join(HERE, "..", "results")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, required=True)
    ap.add_argument("--games", type=int, default=3)
    ap.add_argument("--minutes", type=int, default=3)
    ap.add_argument("--increment", type=int, default=0)
    ap.add_argument("--ms", type=int, default=400)
    ap.add_argument("--max-seconds", type=int, default=560)
    ap.add_argument("--no-learn", action="store_true")
    ap.add_argument("--engine", default="sable",
                    help="'sable' (default), 'sf', or a path to a UCI engine")
    ap.add_argument("--sf-skill", type=int, default=None,
                    help="Stockfish Skill Level 0-20; omit for full strength")
    a = ap.parse_args()

    # Preflight: prove the engine is executable and answers before a game is
    # started. A lost exec bit made play_game bail on move one and burned games.
    eng = os.path.abspath(os.path.join(HERE, "..", "sable"))
    if not os.access(eng, os.X_OK):
        sys.exit(f"engine {eng} is not executable (chmod +x it)")
    try:
        probe = B.sable_best("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 100)
        assert chess.Move.from_uci(probe) in chess.Board().legal_moves
    except Exception as e:
        sys.exit(f"engine preflight failed: {e}")
    print(f"[preflight] engine ok, opening move {probe}", flush=True)

    # Select the engine that drives the mouse.
    def _first_existing(paths):
        for p in paths:
            if p and os.path.exists(p):
                return p
        return paths[0]

    repo = os.path.abspath(os.path.join(HERE, ".."))
    parent = os.path.dirname(repo)
    if a.engine == "sf":
        # SABLE_SF lets the launcher point at wherever Stockfish lives; the
        # remaining candidates cover the layouts used on Linux and Windows.
        eng_path = _first_existing([
            os.environ.get("SABLE_SF"),
            os.path.join(parent, "bin", "sf"),
            os.path.join(parent, "bin", "sf.exe"),
            os.path.join(repo, "bin", "sf"),
            os.path.join(repo, "bin", "sf.exe"),
            os.path.join(repo, "sf.exe"),
            "/home/user/bin/sf",
        ])
    elif a.engine == "sable":
        eng_path = _first_existing([
            os.environ.get("SABLE_ENGINE"),
            os.path.join(repo, "sable.exe"),
            os.path.join(repo, "sable"),
        ])
    else:
        eng_path = os.path.abspath(a.engine)
    if not os.path.exists(eng_path):
        sys.exit(f"engine not found: {eng_path}")
    os.environ["SABLE_ENGINE"] = eng_path
    os.environ.pop("SABLE_SETOPTIONS", None)
    if a.sf_skill is not None:
        os.environ["SABLE_SETOPTIONS"] = f"setoption name Skill Level value {a.sf_skill}"
    playing_sable = os.path.basename(eng_path).lower().startswith("sable")
    tag = "sable" if playing_sable else f"sf({'skill '+str(a.sf_skill) if a.sf_skill is not None else 'max'})"
    print(f"[engine] {tag} at {eng_path}", flush=True)

    out_path = os.path.join(RESULTS, f"series_L{a.level}_{tag}.json")
    run = {"level": a.level, "engine": a.engine,
           "tc": f"{a.minutes}+{a.increment}", "ms": a.ms,
           "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "games": []}
    os.makedirs(RESULTS, exist_ok=True)
    # Keep earlier games at this level: --games N means "play N more".
    if os.path.exists(out_path):
        try:
            prev = json.load(open(out_path))
            if prev.get("games"):
                run["games"] = prev["games"]
                run["started"] = prev.get("started", run["started"])
        except Exception:
            pass
    first = len(run["games"]) + 1

    for gi in range(first, first + a.games):
        ok, detail = L.open_setup(a.level, a.minutes, a.increment, "white")
        if not ok:
            print(f"[L{a.level} g{gi}] setup verification failed: {detail}", flush=True)
            time.sleep(20)
            run["games"].append({"game": gi, "error": f"setup: {detail}"})
            json.dump(run, open(out_path, "w"), indent=2)
            continue
        L.start_game()
        glog = os.path.join(RESULTS, "series_games", f"L{a.level}_g{gi}.json")
        os.makedirs(os.path.dirname(glog), exist_ok=True)
        r = B.play_game(chess.WHITE, engine_ms=a.ms, max_seconds=a.max_seconds,
                        clock_ms=a.minutes * 60_000, log_path=glog)
        board = r["final_board"]
        result = (board.result(claim_draw=True)
                  if board.is_game_over(claim_draw=True) else "unknown")
        agrees = next((e.get("agrees") for e in reversed(r["log"])
                       if e.get("event") == "final"), None)
        moves = [m.uci() for m in board.move_stack]
        won = result == "1-0"
        # only count a win the screen corroborates
        counted = won and agrees is not False

        lr = {}
        if not a.no_learn:
            try:
                lr = LEARN.learn_from_game(moves, True, counted, grade=playing_sable)
            except Exception as e:
                lr = {"learn_error": str(e)}

        rec = {"game": gi, "engine": tag, "result": result, "won": won, "counted": counted,
               "agrees": agrees, "plies": len(moves), "elapsed_s": r["elapsed_s"],
               "moves": moves, "log": glog,
               "fen": board.fen(), "learn": lr,
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        run["games"].append(rec)
        played = [g for g in run["games"] if "result" in g]
        run["wins"] = sum(1 for g in played if g.get("counted"))
        run["played"] = len(played)
        json.dump(run, open(out_path, "w"), indent=2)
        # current run of consecutive counted wins, ending at this game
        streak = 0
        for g in reversed(run["games"]):
            if "result" not in g:
                continue
            if g.get("counted"):
                streak += 1
            else:
                break
        run["streak"] = streak
        run["best_streak"] = max(run.get("best_streak", 0), streak)
        json.dump(run, open(out_path, "w"), indent=2)
        print(f"[L{a.level} g{gi}] {result}  streak={streak}  plies={len(moves)}  {r['elapsed_s']}s  "
              f"agrees={agrees}  book+{lr.get('book_lines_added',0)} "
              f"avoid+{lr.get('avoid_lines_added',0)}  "
              f"score={run.get('wins')}/{run.get('played')}", flush=True)

    print(f"[L{a.level} done] {run.get('wins')}/{run.get('played')}", flush=True)


if __name__ == "__main__":
    main()
