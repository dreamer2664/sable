#!/usr/bin/env python3
"""A tiny deterministic UCI engine, for loop tests.

Real engine, real UCI handshake, no strength: it plays from a scripted list while
the listed moves stay legal, and otherwise plays the first legal move. Tests that
need a reproducible game use this; `test_loop.py` also runs one game against the
real `./sable` binary so the integration is not taken on trust.
"""
import os
import sys

import chess

SCRIPT = [m for m in (os.environ.get("STUB_MOVES", "").split(",")) if m.strip()]

board = chess.Board()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    if line == "uci":
        print("id name stub", flush=True)
        print("uciok", flush=True)
    elif line == "isready":
        print("readyok", flush=True)
    elif line.startswith("position"):
        parts = line.split()
        if "startpos" in parts:
            board = chess.Board()
            rest = parts[parts.index("startpos") + 1:]
            if rest and rest[0] == "moves":
                for u in rest[1:]:
                    board.push(chess.Move.from_uci(u))
        elif "fen" in parts:
            i = parts.index("fen") + 1
            fen_parts = []
            while i < len(parts) and parts[i] != "moves":
                fen_parts.append(parts[i]); i += 1
            board = chess.Board(" ".join(fen_parts))
            if i < len(parts) and parts[i] == "moves":
                for u in parts[i + 1:]:
                    board.push(chess.Move.from_uci(u))
    elif line.startswith("go"):
        move = None
        for u in SCRIPT:
            try:
                mv = chess.Move.from_uci(u)
            except ValueError:
                continue
            if mv in board.legal_moves:
                move = mv
                break
        if move is None:
            move = next(iter(board.legal_moves), None)
        print(f"bestmove {move.uci() if move else '0000'}", flush=True)
    elif line == "quit":
        break
