#!/usr/bin/env python3
"""pceye_client — talk to pc-eye without needing pc-eye's source tree.

WHY THIS FILE EXISTS
--------------------
Every driver in `tools/` imported a module called `pceye` from a sibling checkout
(`../pceye`), and `ladder.py` / `play_on_lichess_board.py` / `recogniser_accuracy.py`
shelled out to a file `../../pceye/pceye.py`. **That file does not exist.**
dreamer2664/pc-eye ships `server.py`, `launcher.py`, `PC-EYE.md`, `start.bat` and
tests — there is no `pceye.py` and no importable client module:

    $ git -C ../pc-eye ls-files
    .gitignore  PC-EYE.md README.md  launcher.py  server.py  start.bat  start.sh
    tests/test_pc_eye.py

So on a fresh machine the chess harness could not start a game at all: `import
pceye` raised ModuleNotFoundError before anything else ran, and the ladder
reported "setup verification failed" as if the browser had misbehaved. The
working copies on the machine that played the games must have had an untracked
`pceye.py`; nothing in either repo produces it.

This module is that client, written against the HTTP contract that pc-eye's own
`PC-EYE.md` documents, so the harness is self-sufficient:

    GET  /state      GET /shot      POST /act      GET /selftest

It reads the URL and token from, in order:
    1. $PCEYE_URL and $PCEYE_TOKEN
    2. a READY.txt (the file pc-eye's launcher writes) at $PCEYE_DIR, ../pc-eye,
       or ~/pc-eye
    3. $PCEYE_READY, an explicit path to a READY.txt

If a real importable `pceye` module is installed, `lichess_bot` uses that instead;
this is the fallback that makes a bare checkout work.

Errors are raised as exceptions with a sentence. Nothing here calls sys.exit():
one bad coordinate must not kill a game that has a clock running.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

_URL_RE = re.compile(r"URL:\s*(\S+)", re.I)
_TOKEN_RE = re.compile(r"TOKEN:\s*(\S+)", re.I)


def _candidate_ready_files():
    env = os.environ.get("PCEYE_READY")
    if env:
        yield env
    for base in (os.environ.get("PCEYE_DIR"),
                 os.path.join(HERE, "..", "..", "pc-eye"),
                 os.path.join(HERE, "..", "..", "pceye"),
                 os.path.expanduser("~/pc-eye"),
                 os.path.expanduser("~/pceye")):
        if base:
            yield os.path.join(base, "READY.txt")


def read_ready_file():
    """(url, token) from the first READY.txt that parses, else (None, None)."""
    for path in _candidate_ready_files():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        url = _URL_RE.search(text)
        tok = _TOKEN_RE.search(text)
        if url and tok:
            return url.group(1).strip().rstrip("/"), tok.group(1).strip()
        # also accept a bare "https://...trycloudflare.com" + 8 hex chars
        m = re.search(r"https://\S+", text)
        if m:
            t = re.search(r"\b([0-9a-fA-F]{8})\b", text)
            if t:
                return m.group(0).strip().rstrip("/"), t.group(1)
    return None, None


def load_creds():
    """(base_url, token) for the pc-eye server. Raises with an actionable message."""
    url = os.environ.get("PCEYE_URL")
    tok = os.environ.get("PCEYE_TOKEN")
    if url and tok:
        return url.rstrip("/"), tok
    url, tok = read_ready_file()
    if url and tok:
        return url, tok
    raise RuntimeError(
        "no pc-eye credentials. Start pc-eye on the PC (start.bat) and either put "
        "its two lines in READY.txt next to pc-eye, or export PCEYE_URL and "
        "PCEYE_TOKEN. Expected a URL containing trycloudflare.com and an 8-character "
        "token.")


def req(path, method="GET", body=None, timeout=60):
    """One request to pc-eye. Returns (headers, bytes, status).

    Raises RuntimeError with pc-eye's own error sentence on 4xx/5xx, instead of
    exiting the process the way the old client did mid-game.
    """
    url, tok = load_creds()
    sep = "&" if "?" in path else "?"
    full = f"{url}{path}{sep}t={urllib.parse.quote(tok)}"
    data = None
    headers = {}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(full, data=data, headers=headers,
                                     method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return dict(r.headers), r.read(), r.status
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except Exception:
            pass
        raise RuntimeError(f"pc-eye refused {method} {path}: HTTP {e.code} {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach pc-eye at {url}: {e.reason}")


def act(payload, timeout=60):
    """POST /act and return the JSON reply."""
    _, raw, _ = req("/act", "POST", payload, timeout=timeout)
    return json.loads(raw)


def shot(region=None, fmt="jpeg", max_width=None, timeout=120):
    """GET /shot. Returns (bytes, meta) where meta carries the sizes and origin."""
    q = []
    if region:
        q.append("region=" + urllib.parse.quote(str(region)))
    if fmt and fmt != "jpeg":
        q.append("fmt=" + fmt)
    if max_width is not None:
        q.append("max=" + str(int(max_width)))
    path = "/shot" + ("?" + "&".join(q) if q else "")
    headers, body, _ = req(path, timeout=timeout)
    lower = {k.lower(): v for k, v in headers.items()}
    meta = {"bytes": len(body),
            "full": lower.get("x-screen-full"),
            "sent": lower.get("x-screen-sent"),
            "origin": lower.get("x-region-origin"),
            "mime": lower.get("content-type")}
    return body, meta


def state(timeout=30):
    _, raw, _ = req("/state", timeout=timeout)
    return json.loads(raw)


def selftest(timeout=60):
    _, raw, _ = req("/selftest", timeout=timeout)
    return json.loads(raw)


def click(x, y, settle=0.0):
    import time
    r = act({"action": "click", "x": int(x), "y": int(y)})
    if r.get("exact") is False:
        raise RuntimeError(f"click did not land: wanted {(x, y)} got {r.get('landed')}")
    if settle:
        time.sleep(settle)
    return r


def key(combo, settle=0.0):
    import time
    r = act({"action": "key", "key": str(combo)})
    if settle:
        time.sleep(settle)
    return r


def paste(text, settle=0.0):
    import time
    r = act({"action": "paste", "text": text})
    if not r.get("ok", True):
        raise RuntimeError(f"paste refused: {r}")
    if settle:
        time.sleep(settle)
    return r


def main(argv=None):
    """Command-line use, mirroring the small verbs the old `pceye.py` had.

    Kept because three tools shell out to it:
        python3 tools/pceye_client.py shot --label x [--out path]
        python3 tools/pceye_client.py click X Y
        python3 tools/pceye_client.py key ctrl+a
        python3 tools/pceye_client.py paste "text"
        python3 tools/pceye_client.py state
        python3 tools/pceye_client.py selftest
    """
    import argparse
    import time
    ap = argparse.ArgumentParser(prog="pceye_client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("shot")
    s.add_argument("--label", default="shot")
    s.add_argument("--out", default=None)
    s.add_argument("--region", default=None)
    s.add_argument("--fmt", default="jpeg")
    c = sub.add_parser("click")
    c.add_argument("x", type=int)
    c.add_argument("y", type=int)
    c.add_argument("--nosettle", action="store_true")
    k = sub.add_parser("key")
    k.add_argument("combo")
    k.add_argument("--nosettle", action="store_true")
    p = sub.add_parser("paste")
    p.add_argument("text")
    p.add_argument("--nosettle", action="store_true")
    sub.add_parser("state")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)

    if a.cmd == "shot":
        body, meta = shot(region=a.region, fmt=a.fmt)
        out = a.out or os.path.join(os.getcwd(), f"{int(time.time()*1000)}_{a.label}.jpg")
        with open(out, "wb") as f:
            f.write(body)
        print(json.dumps({"ok": True, "path": out, "bytes": meta["bytes"],
                          "sent": meta["sent"], "full": meta["full"],
                          "origin": meta["origin"]}))
        return 0
    if a.cmd == "click":
        print(json.dumps(click(a.x, a.y, settle=0.0 if a.nosettle else 0.12)))
        return 0
    if a.cmd == "key":
        print(json.dumps(key(a.combo, settle=0.0 if a.nosettle else 0.12)))
        return 0
    if a.cmd == "paste":
        print(json.dumps(paste(a.text, settle=0.0 if a.nosettle else 0.12)))
        return 0
    if a.cmd == "state":
        print(json.dumps(state(), indent=2))
        return 0
    if a.cmd == "selftest":
        print(json.dumps(selftest(), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
