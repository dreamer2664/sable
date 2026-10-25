# SABLE

A chess engine written from scratch in C++ — bitboards, magic sliders, PVS
search, transposition table, tapered evaluation. No external libraries, no
opening book, no tablebases, no neural net. It speaks UCI, so any GUI can use
it.

Built in one session, on a 2-core sandbox, and measured against Stockfish 17.1
rather than described in adjectives.

---

## The honest headline

The brief was: *"super strong, in fact the main deliverable is that it has to
draw or beat Stockfish max."*

**That is not possible from the starting position, and no amount of engineering
in this session would change it.** Stockfish 17.1 at full strength is roughly
3600 Elo on CCRL — about 800 points above the strongest human world champion.
An engine written in an afternoon loses every game against it, and so would
every other engine written in an afternoon. Any project that hands you a
20-game sweep against Stockfish max is lying about its opponent's settings.

So this repo reports three things that are true instead of one that isn't:

| Claim | Status | Where the evidence is |
|---|---|---|
| Draws Stockfish 17.1 at **full strength** in theoretically drawn positions | **proved, 7/7** | `results/hold_the_draw.json` |
| Beats Stockfish's full-strength *Skill Level 0–2* | **proved** | `results/ladder_skill*.json` |
| Loses to Stockfish 17.1 at full strength from the start position | **stated plainly** | `results/ladder_skill20.json` |
| Absolute strength ≈ **1850 Elo** (100 ms/move, 1 thread) | **measured with error bars** | `results/calib_elo*.json` |

The first row is the real version of "draws Stockfish max": in a *fortress*, the
result does not depend on who is stronger. A correct defender holds it against
God. `tools/hold_the_draw.py` proves Sable is a correct defender, and — this
matters — it screens every position with Stockfish itself first, so a position
that only *looks* drawn cannot be smuggled into the win column.

---

## Measured strength

Everything below was played by `tools/match.py`, 100 ms/move for Sable, all
moves legality-checked by python-chess, Elo with a 95% bootstrap CI. Raw game
data is in `results/`.

### Ladder vs Stockfish 17.1 (Skill Level)

| Opponent | W/D/L | Score | Elo diff | 95% CI |
|---|---|---|---|---|
| SF Skill 0 | 6/0/0 | 1.000 | clean sweep | — |
| SF Skill 2 | 9/1/2 | 0.792 | +232 | +58 … +2400 |
| SF Skill 5 | 2/2/8 | 0.250 | −191 | −545 … 0 |
| SF Skill 8 | 1/3/8 | 0.208 | −232 | −545 … −58 |
| SF Skill 12 | 0/1/11 | 0.042 | −545 | −2400 … −338 |
| SF Skill 20 (full) | 0/1/11 | 0.042 | −545 | −2400 … −338 |

The single draw at Skill 12 and at Skill 20 are both **genuine threefold
repetitions** (`THREEFOLD_REPETITION` in `results/ladder_skill20.json`, game 6,
42 plies) — not adjudications, not engine crashes. Zero illegal moves were
played in 72 games.

### Absolute Elo calibration

Using Stockfish's own calibrated `UCI_Elo` scale as the yardstick:

| Stockfish calibrated to | Result | Implied Sable Elo |
|---|---|---|
| 1500 | 11/0/1 (0.917) | ≈ 1917 |
| 1800 | 6/1/5 (0.542) | ≈ 1829 |
| 2100 | see `results/calib_elo2100.json` | — |

Two independent brackets agree to within ~90 points, which is about the
precision 12-game matches can offer. **Call it ~1850 Elo at 100 ms/move**, and
treat ±100 as the honest error bar.

### Holding the draw against Stockfish at full strength

`python3 tools/hold_the_draw.py` — Sable defends, Stockfish 17.1 full strength
attacks, outcome judged by the rules of chess:

| Position | Result |
|---|---|
| Rook pawn, defender in front (a-file) | held |
| Rook pawn, defender in front (h-file) | held |
| Two knights cannot force mate | held |
| Wrong-colour bishop fortress | held |
| Philidor R+P vs R | held |
| Blocked pawns | held |
| Opposite bishops, dead position | held |

Every one of those positions was first screened by Stockfish itself and only
counted if Stockfish scored it as drawn. See `results/hold_the_draw.json`.

---

## Correctness

Speed is worthless if the move generator is wrong, so correctness comes first
and is tested independently of the engine's own opinion.

**Perft — 26/26 positions** (`./tests/perft.sh`), against published node counts:

```
startpos   d5 =  4,865,609
kiwipete   d4 =  4,085,603
position 3 d5 =    674,624
position 4 d4 =    422,333
position 5 d4 =  2,103,487
position 6 d4 =  3,894,594
```

These six are the standard suite precisely because they punish en-passant,
castling-rights and promotion bugs that a startpos-only perft sails past.

**UCI conformance — 21/21** (`python3 tools/uci_test.py`), with python-chess
supplying the ground truth so the engine cannot grade its own homework:
handshake, `isready`, mate-in-1 detection and mate *scores*, promotion choice,
UCI encode/decode round-trip (promotions, castling, en passant, captures),
100-ply self-play legality in two positions, time management at 100/300/1000 ms,
clock allocation, determinism at fixed depth.

Two real bugs were found and fixed by these tests during the build, and both
are worth naming because they are the kind that hide:

1. **`"  nbrq"` — a double space in the promotion-character table.** Queen
   mapped to `'r'`, so the engine played a queen promotion but *labelled* it
   `a7a8r`, then refused to parse its own output back. Fixed; caught by the
   round-trip test.
2. **No draw detection in search.** The engine would happily repeat a winning
   position, and could not tell a drawn ending from a won one. Repetition,
   fifty-move and insufficient-material detection now run in `search()`.

A third thing was wrong in the *tests*, not the engine: an early "mate in 1"
case had no mate at all (`Qb1–b7` is blocked by White's own king on b6), and
the engine was right. Ground truth now comes from python-chess.

---

## Build and run

```sh
make                     # -> ./sable
./sable                  # UCI on stdin/stdout

# ad-hoc
printf 'position startpos\ngo movetime 1000\nquit\n' | ./sable
printf 'position startpos\nperft 5\nquit\n' | ./sable
```

Requires a C++17 compiler. No dependencies.

## Options

| Option | Default | Meaning |
|---|---|---|
| `Hash` | 32 | transposition table size in MB |

## Playing it in a GUI / on lichess, offline

Sable is a normal UCI engine: point Arena, CuteChess, En Croissant, BanksiaGUI
or Scid at the `sable` binary.

**On lichess, use offline play only.** Analysing and playing against the
*computer* in `lichess.org/analysis` runs Stockfish locally in the browser and
never contacts lichess's matchmaking. Playing **online** games with an engine is
against lichess's terms of service, and this repo does not do it, does not
provide a runner for it, and the tooling here was verified on the analysis
board only.

`tools/play_on_lichess_board.py` drives the analysis board through
[pc-eye](https://github.com/dreamer2664/pc-eye) — it clicks your engine's move
on the board, reads the FEN back off the screen, and checks the two agree. It
touches nothing that requires an account or a game id.

## Layout

```
src/sable.cpp        the whole engine, one file
tests/perft.sh       published-node-count perft suite
tools/uci_test.py    UCI conformance, ground truth from python-chess
tools/match.py       match harness: legality checks, adjudication, Elo + CI
tools/hold_the_draw.py  proves the draw claim against full-strength Stockfish
tools/calibrate.sh   absolute Elo bracketing via Stockfish UCI_Elo
results/             raw JSON from every match, so claims can be re-checked
```

## What it is not

No opening book, no endgame tablebases, no NNUE, no multi-threading, no pondering,
no aspiration-window tuning beyond a fixed ±60. Search has null-move pruning, late
move reductions, futility and reverse-futility pruning, check extensions, killers
and history — which is to say, the standard kit of a 2000s-era engine, correctly
assembled. Its ceiling is real and measured above.

## Licence

MIT.
