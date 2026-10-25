#!/usr/bin/env bash
# Rebuild the runtime bits that the workspace snapshot does not preserve:
# pip packages and the execute bit on binaries. Safe to run repeatedly.
set -e
python3 -m pip install --quiet python-chess pillow numpy 2>/dev/null || \
  python3 -m pip install --quiet --break-system-packages python-chess pillow numpy
chmod +x /home/user/bin/sf /home/user/sable/sable 2>/dev/null || true
chmod +x /home/user/sable/tools/*.py 2>/dev/null || true
echo "setup ok: $(python3 -c 'import chess,PIL,numpy;print("chess",chess.__version__)')"
