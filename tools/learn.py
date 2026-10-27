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
BLUNDER_CP = 150      # how much worse than best before a move is recorded as a mistake
MAX_PER_GAME = 4

# Where Stockfish lives. The old value, ../bin/sf, does not exist in this repo and
# never did, so every game reported "stockfish missing" and the avoid-list -- the
# entire point of learning from losses -- silently stayed empty. Look in the places
# it is actually installed, and say so clearly when it is nowhere.
SF_CANDIDATES = [
    os.environ.get("SABLE_SF") or "",
    os.path.join(HERE, "..", "..", "bin", "sf"),
    os.path.join(HERE, "..", "bin", "sf"),
    "/home/user/bin/sf",
    "/usr/games/stockfish", "/usr/bin/stockfish", "/usr/local/bin/stockfish",
]


def find_stockfish():
    """First existing candidate, or None. Reported, never guessed."""
    for c in SF_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    import shutil
    return shutil.which("stockfish")


SF = find_stockfish() or os.path.join(HERE, "..", "..", "bin", "sf")

# A book line is a claim that a move is good. One lucky win against a weak
# opponent is not evidence, and the old code treated it as gospel: every game that
# "scored" wrote its first 14 plies, so the opening book became a transcript of
# whatever happened to win once. Now a line has to be seen in MIN_BOOK_VOTES
# separate games before it is played as if it were known to be good.
MIN_BOOK_VOTES = 2
VOTES = os.path.join(HERE, "..", "results", "book_votes.json")


def key_of(board):
    f = board.fen().split()
    return f"{f[0]} {f[1]}"


def add_book(moves, plies=14, sable_white=None, blunders=(), min_votes=MIN_BOOK_VOTES):
    """Vote on the moves we actually played, then promote lines that earn it.

    Three changes from the version this replaces, each of which was a way for a
    single game to write a permanent rule:

      * ONLY OUR OWN MOVES. The old loop wrote the opponent's moves too. The engine
        looks the book up by position+side-to-move, so those lines could never be
        played -- pure dead weight in the file, and they made the book look larger
        and better-learned than it was.
      * NO BLUNDERS. A move Stockfish says lost >= BLUNDER_CP centipawns is not a
        book move, whatever the result of the game was.
      * MIN_BOOK_VOTES games must agree before a line is played as knowledge. Votes
        live in results/book_votes.json, so the evidence is inspectable.

    `sable_white=None` means "the caller does not know which side we were", and is
    treated as "all of the moves" for backwards compatibility -- pass it.
    """
    board = chess.Board()
    votes = {}
    if os.path.exists(VOTES):
        try:
            with open(VOTES) as f:
                votes = json.load(f)
        except Exception:
            votes = {}
    existing = set()
    if os.path.exists(BOOK):
        with open(BOOK) as f:
            existing = {l.strip() for l in f if l.strip()}
    voted, promoted, skipped = 0, 0, 0
    blunders = set(blunders)
    new_lines = []
    for i, uci in enumerate(moves[:plies]):
        try:
            m = chess.Move.from_uci(uci)
        except Exception:
            break
        if m not in board.legal_moves:
            break
        mine = True if sable_white is None else ((board.turn == chess.WHITE) == sable_white)
        key = key_of(board)
        line = f"{key}|{uci}"
        if mine:
            if line in blunders or (key, uci) in blunders:
                skipped += 1
            elif line not in existing:
                votes[line] = votes.get(line, 0) + 1
                voted += 1
                if votes[line] >= min_votes:
                    new_lines.append(line)
                    existing.add(line)
                    promoted += 1
        board.push(m)
    if new_lines:
        with open(BOOK, "a") as f:
            f.write("\n".join(new_lines) + "\n")
    try:
        os.makedirs(os.path.dirname(VOTES), exist_ok=True)
        with open(VOTES, "w") as f:
            json.dump(votes, f, indent=1, sort_keys=True)
    except Exception:
        pass
    return {"added": promoted, "voted": voted, "skipped_blunders": skipped}


def find_mistakes(moves, sable_white, ms=60):
    """Return ([(position_key, uci)], error). Errors are returned, not swallowed:
    an empty list because Stockfish could not run is NOT the same as no mistakes,
    and silently reporting the latter is how the avoid-list quietly did nothing."""
    if not SF or not os.path.exists(SF):
        return [], ("stockfish not found; set SABLE_SF=/path/to/stockfish "
                    "(looked in: " + ", ".join(c for c in SF_CANDIDATES if c) + ")")
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
        with open(AVOID) as f:
            existing = {l.strip() for l in f if l.strip()}
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
    if not grade:
        # Without a Stockfish check there is no evidence a move is good, so the
        # book still gets the vote but the caller must know it was not graded.
        if scored:
            rec["book"] = add_book(moves, sable_white=sable_white)
            rec["book_lines_added"] = rec["book"]["added"]
        rec["graded"] = False
        return rec
    # Grade FIRST, then learn: a move has to pass the blunder check before it is
    # allowed to become a book line.
    mistakes, err = find_mistakes(moves, sable_white)
    if scored:
        rec["book"] = add_book(moves, sable_white=sable_white, blunders=mistakes)
        rec["book_lines_added"] = rec["book"]["added"]
    rec["mistakes_found"] = len(mistakes)
    if err:
        rec["mistake_check_error"] = err
    rec["avoid_lines_added"] = add_avoid(mistakes)
    return rec


if __name__ == "__main__":
    # manual use: python3 learn.py <json with moves + sable_white + scored>
    d = json.load(open(sys.argv[1]))
    print(json.dumps(learn_from_game(d["moves"], d["sable_white"], d["scored"]), indent=2))
