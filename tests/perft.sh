#!/bin/bash
# Perft regression vs published node counts.
run() { # fen depth expected label
  local fen="$1" d="$2" exp="$3" label="$4"
  local got
  got=$(printf 'position fen %s\nperft %s\nquit\n' "$fen" "$d" | ./sable 2>/dev/null | grep '^perft' | awk '{print $4}')
  if [ "$got" = "$exp" ]; then echo "PASS  $label d$d = $got"; else echo "FAIL  $label d$d = $got  (expected $exp)"; fi
}
run "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" 1 20 "startpos"
run "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" 2 400 "startpos"
run "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" 3 8902 "startpos"
run "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" 4 197281 "startpos"
run "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" 5 4865609 "startpos"
run "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1" 1 48 "kiwipete"
run "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1" 2 2039 "kiwipete"
run "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1" 3 97862 "kiwipete"
run "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1" 4 4085603 "kiwipete"
run "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1" 1 14 "pos3"
run "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1" 2 191 "pos3"
run "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1" 3 2812 "pos3"
run "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1" 4 43238 "pos3"
run "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1" 5 674624 "pos3"
run "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1" 1 6 "pos4"
run "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1" 2 264 "pos4"
run "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1" 3 9467 "pos4"
run "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1" 4 422333 "pos4"
run "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8" 1 44 "pos5"
run "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8" 2 1486 "pos5"
run "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8" 3 62379 "pos5"
run "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8" 4 2103487 "pos5"
run "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10" 1 46 "pos6"
run "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10" 2 2079 "pos6"
run "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10" 3 89890 "pos6"
run "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10" 4 3894594 "pos6"
