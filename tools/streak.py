#!/usr/bin/env python3
"""Chase a consecutive-win streak against lichess computer level 7.

Rules, as asked for:
  * a win extends the streak;
  * a loss, a draw, a timeout, or a game the harness cannot account for RESETS
    the streak to zero;
  * every game is recorded, and every game updates the learned files
    (`book.txt`, `avoid.txt`) via learn.py.

The runner is resumable: progress lives in `results/streak.json`, so it can be
stopped and restarted without losing the count. It is meant to be run for hours;
`--budget-hours` stops it cleanly at a checkpoint.
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
STATE = os.path.join(RESULTS, "streak.json")


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {"streak": 0, "best_streak": 0, "games": 0, "history": [],
            "target": None, "started": None}


def save_state(st):
    os.makedirs(RESULTS, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=7)
    ap.add_argument("--target", type=int, default=100)
    ap.add_argument("--minutes", type=int, default=3)
    ap.add_argument("--increment", type=int, default=0)
    ap.add_argument("--ms", type=int, default=300)
    ap.add_argument("--max-seconds", type=int, default=560)
    ap.add_argument("--budget-hours", type=float, default=8.0)
    ap.add_argument("--no-learn", action="store_true")
    a = ap.parse_args()

    st = load_state()
    st["target"] = a.target
    st["started"] = st.get("started") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    st["config"] = {"level": a.level, "tc": f"{a.minutes}+{a.increment}", "ms": a.ms}
    t_zero = time.time()
    save_state(st)

    while st["streak"] < a.target:
        if (time.time() - t_zero) / 3600.0 > a.budget_hours:
            print(f"[stop] budget of {a.budget_hours}h reached at streak {st['streak']}", flush=True)
            break

        gi = st["games"] + 1
        ok, detail = L.open_setup(a.level, a.minutes, a.increment, "white")
        if not ok:
            print(f"[{gi}] setup verification failed: {detail}", flush=True)
            time.sleep(20)
            continue
        L.start_game()
        log = os.path.join(RESULTS, "streak_games", f"g{gi:04d}.json")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        r = B.play_game(chess.WHITE, engine_ms=a.ms, max_seconds=a.max_seconds,
                        log_path=log, clock_ms=a.minutes * 60_000,
                        record_dir=os.path.join(RESULTS, "streak_games", f"g{gi:04d}"))
        board = r["final_board"]
        result = board.result(claim_draw=True) if board.is_game_over(claim_draw=True) else "unknown"
        moves = [m.uci() for m in board.move_stack]
        won = (result == "1-0")
        agrees = None
        try:
            ev = json.load(open(log))
            agrees = next((e.get("agrees") for e in reversed(ev) if e.get("event") == "final"), None)
        except Exception:
            pass
        # A result we cannot corroborate against the screen does not count as a win.
        counted = won and (agrees is not False)

        st["games"] = gi
        if counted:
            st["streak"] += 1
            st["best_streak"] = max(st["best_streak"], st["streak"])
        else:
            if st["streak"] > 0:
                print(f"[{gi}] STREAK BROKEN at {st['streak']} (result {result}, agrees={agrees})",
                      flush=True)
            st["streak"] = 0

        lr = {}
        if not a.no_learn:
            try:
                lr = LEARN.learn_from_game(moves, True, counted)
            except Exception as e:
                lr = {"learn_error": str(e)}

        rec = {"game": gi, "result": result, "counted": counted, "agrees": agrees,
               "moves": len(moves), "elapsed_s": r["elapsed_s"],
               "streak_after": st["streak"], "fen": board.fen(), "learn": lr,
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        st["history"].append(rec)
        save_state(st)
        print(f"[{gi}] {result}  streak={st['streak']}/{a.target}  "
              f"best={st['best_streak']}  moves={len(moves)}  "
              f"book+{lr.get('book_lines_added',0)} avoid+{lr.get('avoid_lines_added',0)}  "
              f"agrees={agrees}", flush=True)

    print(f"[done] streak={st['streak']} best={st['best_streak']} games={st['games']}", flush=True)


if __name__ == "__main__":
    main()
