#!/usr/bin/env python3
"""UCI conformance + search sanity tests for Sable.

Ground truth comes from python-chess (an independent implementation), so a bug
in Sable's move generator or its UCI encoding cannot hide behind its own code.
"""
import subprocess, sys, time, os
import chess

HERE = os.path.dirname(os.path.abspath(__file__))
ENG = os.path.join(HERE, "..", "sable")

class Engine:
    def __init__(self, path=ENG):
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.send("uci"); self.wait("uciok")
    def send(self, s): self.p.stdin.write(s + "\n"); self.p.stdin.flush()
    def wait(self, token):
        while True:
            line = self.p.stdout.readline()
            if not line: raise RuntimeError("engine died")
            if line.startswith(token): return line.strip()
    def setpos(self, board):
        self.send("position fen " + board.fen())
    def go(self, **kw):
        self.send("go " + " ".join(f"{k} {v}" for k, v in kw.items()))
        info = []
        while True:
            line = self.p.stdout.readline()
            if not line: raise RuntimeError("engine died during go")
            if line.startswith("info"): info.append(line.strip())
            if line.startswith("bestmove"): return line.split()[1], info
    def quit(self):
        try: self.send("quit"); self.p.wait(timeout=5)
        except Exception: self.p.kill()

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  :: " + detail) if detail else ""))

def mates_in_1(board):
    out = []
    for m in list(board.legal_moves):
        b2 = board.copy(); b2.push(m)
        if b2.is_checkmate(): out.append(m.uci())
    return out

# ---------------------------------------------------------------- handshake
e = Engine()
check("uci handshake", True)
e.send("isready"); check("isready", e.wait("readyok") == "readyok")

# ---------------------------------------------------------------- mate in one
MATE_FENS = [
    "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1",
    "7k/6pp/8/8/8/8/5PPP/4R1K1 w - - 0 1",
    "k7/8/1K6/8/8/8/8/1Q6 w - - 0 1",
    "6k1/6pp/8/8/8/8/8/R5K1 w - - 0 1",
    "3k4/8/3K4/8/8/8/8/6R1 w - - 0 1",
]
for fen in MATE_FENS:
    b = chess.Board(fen)
    truth = mates_in_1(b)
    e.send("ucinewgame"); e.setpos(b)
    mv, info = e.go(depth=6)
    if not truth:
        check(f"no mate-in-1 exists, engine still legal ({fen[:20]})",
              chess.Move.from_uci(mv) in b.legal_moves, mv)
    else:
        check(f"finds a mate-in-1 among {len(truth)} ({fen[:20]})", mv in truth,
              f"played {mv}, true = {truth}")
        check(f"reports a mate score ({fen[:20]})",
              any("score mate" in l for l in info), info[-1] if info else "")

# ---------------------------------------------------------------- UCI round-trip
# Every move the engine emits must be one it can parse back. This is exactly the
# bug class that produced "a7a8r" for a queen promotion.
rt_fail = []
for fen in ["8/P7/8/8/8/8/k7/7K w - - 0 1",
            "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8",
            "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
            "n1n5/PPPk4/8/8/8/8/4Kppp/5N1N b - - 0 1"]:
    b = chess.Board(fen)
    e.send("ucinewgame"); e.setpos(b)
    mv, _ = e.go(depth=5)
    m = chess.Move.from_uci(mv)
    if m not in b.legal_moves: rt_fail.append((fen, mv, "not legal"))
    e.send("position fen " + fen + " moves " + mv)
    # ask for the position back via 'd' and confirm it advanced
    b2 = b.copy(); b2.push(m)
    e.send("d")
    got = e.wait("info string")
    if got.replace("info string ", "").split()[:4] != b2.fen().split()[:4]:
        rt_fail.append((fen, mv, "roundtrip mismatch: " + got))
check("uci move round-trip (promotions, castling, ep, captures)", not rt_fail, str(rt_fail[:2]))

# ---------------------------------------------------------------- promotion choice
b = chess.Board("8/P7/8/8/8/8/k7/7K w - - 0 1")
e.send("ucinewgame"); e.setpos(b)
mv, _ = e.go(depth=6)
check("promotes to queen when winning", mv == "a7a8q", mv)

# ---------------------------------------------------------------- legality over long self-play
for label, fen in [("startpos", chess.STARTING_FEN),
                   ("kiwipete", "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1")]:
    b = chess.Board(fen)
    e.send("ucinewgame"); e.setpos(b)
    plies, allok, bad = 0, True, ""
    t0 = time.time()
    while not b.is_game_over() and plies < 100:
        mv, _ = e.go(nodes=15000)
        if mv == "0000":
            if list(b.legal_moves): allok, bad = False, f"resigned at ply {plies}"
            break
        m = chess.Move.from_uci(mv)
        if m not in b.legal_moves: allok, bad = False, f"illegal {mv} ply {plies}"; break
        b.push(m); e.setpos(b); plies += 1
    check(f"self-play 100 plies legal ({label})", allok,
          bad or f"{plies} plies, result {b.result() if b.is_game_over() else 'ongoing'}, {time.time()-t0:.1f}s")

# ---------------------------------------------------------------- time management
for ms, tol in [(100, 1200), (300, 1500), (1000, 4000)]:
    b = chess.Board(); e.setpos(b)
    t = time.time(); e.go(movetime=ms); el = (time.time() - t) * 1000
    check(f"movetime {ms}ms respected", el <= tol, f"took {el:.0f}ms")

# ---------------------------------------------------------------- clock-based allocation
b = chess.Board(); e.setpos(b)
t = time.time(); e.go(wtime=5000, btime=5000, winc=100, binc=100); el = (time.time() - t) * 1000
check("clock allocation sane (5s+0.1)", 20 <= el <= 2000, f"used {el:.0f}ms of 5000ms")

# ---------------------------------------------------------------- determinism
b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3")
outs = set()
for _ in range(3):
    e.send("ucinewgame"); e.setpos(b); mv, _ = e.go(depth=6); outs.add(mv)
check("deterministic at fixed depth", len(outs) == 1, str(outs))

# ---------------------------------------------------------------- forced mate in 2/3
def mate_in_n(board, n):
    if n == 0:
        return board.is_checkmate()
    if board.is_game_over(): return False
    if board.turn == chess.WHITE:
        return any(mate_in_n_after(board, m, n) for m in board.legal_moves)
    else:
        return all(mate_in_n_after(board, m, n) for m in list(board.legal_moves))

def mate_in_n_after(board, m, n):
    b2 = board.copy(); b2.push(m)
    if n == 1: return b2.is_checkmate()
    if b2.is_game_over(): return False
    if b2.turn == chess.WHITE:
        return any(mate_in_n_after(b2, mm, n-1) for mm in b2.legal_moves)
    return all(mate_in_n_after(b2, mm, n-1) for mm in list(b2.legal_moves))

M2 = "r5rk/5p1p/5R2/4B3/8/8/7P/7K w - - 0 1"
b = chess.Board(M2)
e.send("ucinewgame"); e.setpos(b)
mv, info = e.go(depth=10)
b2 = b.copy(); b2.push(chess.Move.from_uci(mv))
found = any("score mate" in l for l in info)
check("mate-in-2 puzzle: reports mate score", found, info[-1] if info else "")

# ---------------------------------------------------------------- checkmate preference
b = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
e.send("ucinewgame"); e.setpos(b)
mv, info = e.go(depth=4)
def _is_mate(board, uci):
    c = board.copy(); c.push(chess.Move.from_uci(uci)); return c.is_checkmate()
check("plays the mate, not just a strong move", _is_mate(b, mv), mv)

e.quit()

npass = sum(1 for _, ok, _ in results if ok)
print(f"\n{npass}/{len(results)} passed")
sys.exit(0 if npass == len(results) else 1)
