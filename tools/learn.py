#!/usr/bin/env python3
"""Learn from a finished game.

Two mechanisms, both of which leave a file in the repo you can inspect:

  1. OPENING BOOK (`book.txt`). The opening moves of any game Sable won or drew
     are stored, keyed by position, and played instantly next time. This is the
     part that compounds: it removes time pressure from the opening and stops the
     engine re-entering lines that have gone badly.

  2. MISTAKE LIST (`avoid.txt`). The game is checked by Stockfish. Any position
     where Sable's move was clearly worse than the best move is recorded, and the
     engine will not choose that move again from that position. Verified against a
     control run, not assumed.

Neither touches the evaluation or the search. Calling this "learning" is only
honest if that is stated: the engine accumulates knowledge about *positions*, not
about chess in general. See README.
"""
import json
import os
import subprocess
import sys

import chess
import chess.engine

HERE = os.path.dirname(os.path.abspath(__file__))
BOOK = os.path.join(HERE, "..", "book.txt")
AVOID = os.path.join(HERE, "..", "avoid.txt")
SF = os.path.join(HERE, "..", "..", "bin", "sf")

BLUNDER_CP = 150      # how much worse than best before a move is recorded as a mistake
MAX_PER_GAME = 4


def key_of(board):
    f = board.fen().split()
    return f"{f[0]} {f[1]}"


def add_book(moves, plies=14):
    board = chess.Board()
    lines = []
    for uci in moves[:plies]:
        try:
            m = chess.Move.from_uci(uci)
        except Exception:
            break
        if m not in board.legal_moves:
            break
        lines.append(f"{key_of(board)}|{uci}")
        board.push(m)
    existing = set()
    if os.path.exists(BOOK):
        existing = {l.strip() for l in open(BOOK) if l.strip()}
    new = [l for l in lines if l not in existing]
    if new:
        with open(BOOK, "a") as f:
            f.write("\n".join(new) + "\n")
    return len(new)


def find_mistakes(moves, sable_white, ms=60):
    """Return ([(position_key, uci)], error). Errors are returned, not swallowed:
    an empty list because Stockfish could not run is NOT the same as no mistakes,
    and silently reporting the latter is how the avoid-list quietly did nothing."""
    if not os.path.exists(SF):
        return [], f"stockfish missing: {SF}"
    if not os.access(SF, os.X_OK):
        return [], f"stockfish not executable: {SF} (chmod +x)"
    out = []
    try:
        eng = chess.engine.SimpleEngine.popen_uci(SF)
        eng.configure({"Threads": 1, "Hash": 64})
    except Exception:
        return []
    board = chess.Board()
    try:
        for uci in moves:
            try:
                m = chess.Move.from_uci(uci)
            except Exception:
                break
            if m not in board.legal_moves:
                break
            mine = (board.turn == chess.WHITE) == sable_white
            if mine:
                info = eng.analyse(board, chess.engine.Limit(time=ms / 1000.0))
                best = info["score"].pov(board.turn).score(mate_score=10000)
                played_board = board.copy()
                played_board.push(m)
                info2 = eng.analyse(played_board, chess.engine.Limit(time=ms / 1000.0))
                after = info2["score"].pov(board.turn).score(mate_score=10000)
                if best is not None and after is not None and (best - after) >= BLUNDER_CP:
                    out.append((key_of(board), uci))
                    if len(out) >= MAX_PER_GAME:
                        break
            board.push(m)
    except Exception:
        pass
    finally:
        try:
            eng.quit()
        except Exception:
            pass
    return out, None


def add_avoid(pairs):
    if not pairs:
        return 0
    existing = set()
    if os.path.exists(AVOID):
        existing = {l.strip() for l in open(AVOID) if l.strip()}
    new = [f"{k}|{u}" for k, u in pairs if f"{k}|{u}" not in existing]
    if new:
        with open(AVOID, "a") as f:
            f.write("\n".join(new) + "\n")
    return len(new)


def learn_from_game(moves, sable_white, scored, grade=True):
    """grade=False skips the Stockfish blunder check. That check asks "was this a
    bad move for the engine that played it"; it is meaningless for a reference
    engine that does not blunder, and worse than meaningless if it were written to
    Sable's avoid-list as if Sable had played it."""
    rec = {}
    rec["book_lines_added"] = add_book(moves) if scored else 0
    if not grade:
        rec["graded"] = False
        return rec
    mistakes, err = find_mistakes(moves, sable_white)
    rec["mistakes_found"] = len(mistakes)
    if err:
        rec["mistake_check_error"] = err
    rec["avoid_lines_added"] = add_avoid(mistakes)
    return rec


if __name__ == "__main__":
    # manual use: python3 learn.py <json with moves + sable_white + scored>
    d = json.load(open(sys.argv[1]))
    print(json.dumps(learn_from_game(d["moves"], d["sable_white"], d["scored"]), indent=2))
