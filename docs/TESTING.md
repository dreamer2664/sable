# Testing

Three independent layers, each answering a different question.

## 1. Is the move generator correct? — `tests/perft.sh`

Perft counts leaf nodes and compares against published values. A single wrong
node anywhere means the engine is playing a different game than the one it
thinks it is playing. 26 checks across the six standard positions.

```
$ ./tests/perft.sh
PASS  startpos d5 = 4865609
PASS  kiwipete d4 = 4085603
...
26/26
```

## 2. Does it speak UCI correctly? — `tools/uci_test.py`

python-chess supplies ground truth, so the engine never grades its own
homework. Covers handshake, `isready`, mate detection, promotion choice, UCI
round-trip in tricky positions, 100-ply legality, time management, clock
allocation, and determinism.

This layer caught both real engine bugs in the build (see README): a
double-space typo in the promotion table that relabelled queens as rooks, and
missing draw detection in search.

## 3. Is it actually strong? — `tools/match.py`, `tools/hold_the_draw.py`

`match.py` plays real games against Stockfish at a stated configuration,
legality-checks every move, adjudicates only where it says it does, and reports
Elo with a bootstrap confidence interval.

`hold_the_draw.py` screens positions with Stockfish first, then has Sable
defend them against Stockfish at full strength. Only positions Stockfish itself
calls drawn are counted.

### Reporting rules this repo holds itself to

* an Elo number never appears without its opponent's exact settings;
* a win is never reported against an opponent whose strength was not stated;
* adjudicated games are counted and labelled;
* "draw" means the rules of chess said so, not that the engine crashed;
* if the headline claim in the brief is impossible, the README says so at the
  top instead of reframing a lesser result as the goal.
