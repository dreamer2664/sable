#!/usr/bin/env python3
"""Re-read recorded games and say, with numbers, how often the eye is right.

Two modes, because there are two kinds of evidence in this repo and only one of
them can be checked automatically:

  --log results/g_L6_1.json
      Replay a recorded game. Every move in the log is pushed onto a board, the
      position is rendered, and the recogniser is asked to read it back cold.
      This is ground truth we own: the FEN is known because we know the moves.
      It measures the RECOGNISER over positions that actually occurred, not over
      a hand-picked list of pretty ones.

  --manifest shots.json
      Replay real screenshots against known FENs. The manifest is a list of
      {"path": ..., "fen": ..., "flipped": bool}. Use this with frames captured
      by a live run when the board was also tracked locally. This is the only
      mode that measures the whole chain (capture -> eye), because it uses the
      pixels the screen actually produced.

Why not just trust the ten "final agrees=True" lines in the logs: those say the
harness's own bookkeeping matched its own reading. They cannot catch a
recogniser that is consistently wrong in the same way twice. This can.

Usage:
  python3 tools/replay_shots.py --all                 # every recorded game
  python3 tools/replay_shots.py --log results/g_L6_1.json --every 2
  python3 tools/replay_shots.py --manifest results/replay_manifest.json
"""
import argparse
import glob
import json
import os
import re
import sys
import time

import chess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import boardvision as V  # noqa: E402


def templates():
    import lichess_bot as B
    rec = B.Recogniser()
    path = os.environ.get("SABLE_TEMPLATES") or os.path.join(ROOT, "results", "templates.npz")
    rec.load(path)
    return rec


def positions_from_log(path, every=1):
    """Yield (fen, flipped, label) for every position in a recorded game.

    The log's own move stream is replayed with python-chess, so the FEN is not
    taken from the log's bookkeeping -- it is derived independently. A log that
    recorded an illegal move stops here and says so.
    """
    events = json.load(open(path))
    if not isinstance(events, list):
        return
    flipped = bool(next((e.get("flipped") for e in events
                         if e.get("event") in ("start_verified", "start_adopted")), False))
    board = chess.Board()
    n = 0
    for e in events:
        if e.get("event") in ("my_move", "opponent_move") and e.get("move"):
            try:
                mv = chess.Move.from_uci(e["move"])
            except ValueError:
                yield None, flipped, f"unparseable {e['move']!r}"
                return
            if mv not in board.legal_moves:
                yield None, flipped, f"illegal {e['move']} at ply {board.ply()}"
                return
            board.push(mv)
            n += 1
            if n % every == 0:
                yield board.fen(), flipped, f"ply{n}"
    yield board.fen(), flipped, "final"


def mean_conf(detail):
    """Mean per-square template margin -- the number the read gate uses.

    read_board returns per-square detail, not one confidence, on purpose: a worst
    square rule cannot work (a real start position has one legitimate 0.011
    square), so the gate and this report both average.
    """
    if isinstance(detail, dict):
        vals = [v[1] for v in detail.values()]
        return sum(vals) / max(len(vals), 1)
    return float(detail)


def replay_positions(rec, positions, screen, verbose=True):
    """Render each position and read it back. Returns a stats dict."""
    import chess as _c
    from virtualscreen import VirtualScreen
    ok_sq = n_sq = exact = n = 0
    fails = []
    t_render = t_read = 0.0
    for fen, flipped, label in positions:
        if fen is None:
            fails.append({"label": label, "reason": "bad log"})
            n += 1
            continue
        truth = _c.Board(fen)
        t0 = time.perf_counter()
        s = VirtualScreen(board=truth, our_side=_c.BLACK if flipped else _c.WHITE)
        im = s.render()
        geo = V.find_board(im)
        t_render += time.perf_counter() - t0
        t1 = time.perf_counter()
        got, detail = V.read_board(rec, im, geo, flipped)
        conf = mean_conf(detail)
        t_read += time.perf_counter() - t1
        n += 1
        bad = []
        for sq in _c.SQUARES:
            want = truth.piece_at(sq).symbol() if truth.piece_at(sq) else "."
            have = got.piece_at(sq).symbol() if got.piece_at(sq) else "."
            n_sq += 1
            if want == have:
                ok_sq += 1
            else:
                bad.append((_c.square_name(sq), want, have))
        if not bad:
            exact += 1
        else:
            fails.append({"label": label, "fen": fen, "conf": round(conf, 3),
                          "errors": [f"{s}:{w}->{h}" for s, w, h in bad[:8]]})
        if verbose and bad:
            print(f"  {label:8s} {len(bad):2d} wrong  conf={conf:.3f}  "
                  + ", ".join(f"{s}:{w}->{h}" for s, w, h in bad[:6]))
    return {"positions": n, "exact": exact, "square_ok": ok_sq, "square_total": n_sq,
            "square_accuracy": round(ok_sq / n_sq, 5) if n_sq else 0.0,
            "render_ms": round(t_render / max(n, 1) * 1000, 1),
            "read_ms": round(t_read / max(n, 1) * 1000, 1),
            "fails": fails}


def replay_manifest(rec, entries):
    from PIL import Image
    ok_sq = n_sq = exact = 0
    fails = []
    t = 0.0
    for e in entries:
        im = Image.open(e["path"]).convert("RGB")
        t0 = time.perf_counter()
        try:
            geo = V.find_board(im)
        except RuntimeError as exc:
            t += time.perf_counter() - t0
            fails.append({"path": e["path"], "reason": str(exc)})
            continue
        t += time.perf_counter() - t0
        got, detail = V.read_board(rec, im, geo, bool(e.get("flipped")))
        conf = mean_conf(detail)
        truth = chess.Board(e["fen"])
        bad = []
        for sq in chess.SQUARES:
            want = truth.piece_at(sq).symbol() if truth.piece_at(sq) else "."
            have = got.piece_at(sq).symbol() if got.piece_at(sq) else "."
            n_sq += 1
            if want == have:
                ok_sq += 1
            else:
                bad.append(f"{chess.square_name(sq)}:{want}->{have}")
        if bad:
            fails.append({"path": e["path"], "conf": round(conf, 3), "errors": bad[:8]})
        else:
            exact += 1
    n = len(entries)
    return {"positions": n, "exact": exact, "square_ok": ok_sq, "square_total": n_sq,
            "square_accuracy": round(ok_sq / n_sq, 5) if n_sq else 0.0,
            "find_ms": round(t / max(n, 1) * 1000, 1), "fails": fails}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", action="append", default=[])
    ap.add_argument("--all", action="store_true", help="every recorded game in results/")
    ap.add_argument("--manifest")
    ap.add_argument("--every", type=int, default=1, help="sample one position in N")
    ap.add_argument("--json", help="write the result here")
    args = ap.parse_args(argv)

    rec = templates()
    out = {"mode": None, "games": {}}

    if args.manifest:
        entries = json.load(open(args.manifest))
        out["mode"] = "manifest"
        out["manifest"] = replay_manifest(rec, entries)
        r = out["manifest"]
        print(f"manifest: {r['exact']}/{r['positions']} positions exact, "
              f"{r['square_ok']}/{r['square_total']} squares, find {r['find_ms']} ms/frame")
        for f in r["fails"][:10]:
            print("  FAIL", f)
    else:
        logs = args.log
        if args.all or not logs:
            logs = sorted(p for p in glob.glob(os.path.join(ROOT, "results", "*.json"))
                          if re.match(r"(g_|game)", os.path.basename(p)))
        out["mode"] = "logs"
        tot = {"positions": 0, "exact": 0, "square_ok": 0, "square_total": 0}
        for path in logs:
            name = os.path.basename(path)
            print(f"{name}:")
            r = replay_positions(rec, positions_from_log(path, args.every), None)
            out["games"][name] = r
            for k in tot:
                tot[k] += r[k]
            print(f"  {r['exact']}/{r['positions']} positions exact, "
                  f"{r['square_ok']}/{r['square_total']} squares, "
                  f"render {r['render_ms']} ms + read {r['read_ms']} ms")
        out["total"] = tot
        out["square_accuracy"] = round(tot["square_ok"] / tot["square_total"], 5) if tot["square_total"] else 0.0
        print(f"\nTOTAL {tot['exact']}/{tot['positions']} positions exact, "
              f"{tot['square_ok']}/{tot['square_total']} squares = "
              f"{100 * out['square_accuracy']:.2f}%")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2)
        print("wrote", args.json)
    bad = out.get("manifest", out.get("total"))
    if not bad or bad["exact"] != bad["positions"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
