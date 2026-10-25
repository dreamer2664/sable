#!/bin/bash
# Bracket Sable's absolute strength using Stockfish's calibrated UCI_Elo scale.
set -e
cd "$(dirname "$0")/.."
for elo in 1500 1800 2100; do
  echo "############ SF UCI_Elo $elo ############"
  python3 tools/match.py --games 12 --sable-ms 100 --sf-ms 100 --sf-elo $elo \
    --out results/calib_elo$elo.json 2>&1 | tail -4
done
echo "CALIBRATION DONE"
