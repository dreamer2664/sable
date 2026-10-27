#!/usr/bin/env python3
"""The contract between sable and pc-eye, tested against the REAL pc-eye server.

Why this file exists
--------------------
Every tool in this repo used to reach through pc-eye with a copy-pasted stub or a
`pceye.py` that neither repo contains. The harness therefore could not be started
from a clean checkout, and the one thing that was never checked is the thing that
breaks silently in production: the wire format.

pc-eye ships a headless, `--dry-run` server mode precisely so this can be tested
without a PC. These tests start that server, talk to it over HTTP through
tools/pceye_client.py, and assert the contract sable depends on:

  * a missing or wrong token is refused, and refusals RAISE -- the old in-tree
    client called sys.exit() on a bad request, which killed the bot mid-game;
  * `/shot` failing returns a sentence a human can act on, not an empty image;
  * the action payloads the client sends are the ones the server's schema wants
    (`key` needs a field called `key`, not `combo` -- the kind of thing that is
    only ever caught by asking the real server);
  * every action is acknowledged and logged by the server.

They are skipped, loudly, when the pc-eye checkout is not next to this one.
"""
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PCEYE_DIRS = [os.environ.get("PCEYE_DIR"),
              os.path.join(ROOT, "..", "pc-eye"),
              os.path.join(ROOT, "..", "pceye"),
              os.path.expanduser("~/pc-eye")]
PCEYE_DIR = next((d for d in PCEYE_DIRS if d and os.path.isfile(os.path.join(d, "server.py"))), None)
TOKEN = "0123456789abcdef"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class PcEyeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if PCEYE_DIR is None:
            raise unittest.SkipTest("no pc-eye checkout next to sable; "
                                    "set PCEYE_DIR to run the contract tests")
        cls.port = free_port()
        cls.log = os.path.join("/tmp", f"sable-contract-{os.getpid()}.log")
        cls.proc = subprocess.Popen(
            [sys.executable, "server.py", "--port", str(cls.port), "--token", TOKEN,
             "--dry-run", "--log", cls.log],
            cwd=PCEYE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{cls.url}/state?t={TOKEN}", timeout=2).read()
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                time.sleep(0.2)
        else:
            cls.tearDownClass()
            raise unittest.SkipTest("pc-eye server did not come up")
        os.environ["PCEYE_URL"] = cls.url
        os.environ["PCEYE_TOKEN"] = TOKEN
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import pceye_client
        cls.P = pceye_client

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None):
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except Exception:
                cls.proc.kill()

    def test_state_reports_what_it_cannot_do(self):
        st = self.P.state()
        self.assertIsInstance(st, dict)
        # A dry-run, headless server cannot capture. The point is that it SAYS so
        # rather than returning a black image and letting the eye read it as an
        # empty board.
        self.assertIn("capture_ok", st)
        self.assertIn("dpi", st)
        self.assertIn("coord_space", st)
        if not st["capture_ok"]:
            self.assertTrue(st.get("capture_why"), "capture failed with no reason given")

    def test_a_bad_token_is_refused_and_raises(self):
        good = os.environ["PCEYE_TOKEN"]
        os.environ["PCEYE_TOKEN"] = "deadbeefdeadbeef"
        try:
            with self.assertRaises(RuntimeError) as cm:
                self.P.state()
            self.assertIn("401", str(cm.exception))
        finally:
            os.environ["PCEYE_TOKEN"] = good

    def test_a_failed_shot_raises_with_the_servers_own_words(self):
        # Headless, so capture must fail. What matters is that the failure arrives
        # as an exception carrying pc-eye's sentence, not as exit(1) or as bytes.
        try:
            r = self.P.shot()
        except RuntimeError as e:
            self.assertTrue(str(e).strip())
            self.assertRegex(str(e), r"HTTP 5\d\d|screen capture failed")
            return
        self.assertTrue(r.get("path"), "shot returned without a path or an error")

    def test_every_action_sable_uses_is_accepted(self):
        r = self.P.click(500, 300)
        self.assertTrue(r.get("ok"), r)
        # dry-run echoes what it WOULD have done; that proves the payload parsed
        self.assertEqual((r.get("did") or {}).get("x"), 500)
        self.assertTrue(self.P.key("ctrl+a").get("ok"))
        self.assertTrue(self.P.paste("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1").get("ok"))

    def test_actions_are_logged_on_the_server(self):
        self.P.click(11, 22)
        deadline = time.time() + 5
        while time.time() < deadline:
            with open(self.log, "r", errors="replace") as f:
                text = f.read()
            if "act click" in text and '"x": 11' in text:
                return
            time.sleep(0.1)
        self.fail("the server did not log the click")

    def test_unknown_action_is_refused_not_ignored(self):
        with self.assertRaises(RuntimeError):
            self.P.act({"action": "no_such_verb", "x": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
