#!/usr/bin/env python3
"""What earns a place in the opening book.

The rule used to be "the game was not lost", and one lucky win against a weak
opponent wrote fourteen plies of it as knowledge forever. These tests pin the
rule that replaced it: only our own moves, never a blunder, and only after
MIN_BOOK_VOTES separate games agree.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

import chess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import learn as L  # noqa: E402


class BookGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sablebook-")
        self._book, self._votes = L.BOOK, L.VOTES
        L.BOOK = os.path.join(self.tmp, "book.txt")
        L.VOTES = os.path.join(self.tmp, "book_votes.json")

    def tearDown(self):
        L.BOOK, L.VOTES = self._book, self._votes
        shutil.rmtree(self.tmp, ignore_errors=True)

    def book_lines(self):
        if not os.path.exists(L.BOOK):
            return []
        return [l.strip() for l in open(L.BOOK) if l.strip()]

    def test_only_our_own_moves_are_voted_on(self):
        # 1. e4 e5 2. Nf3 Nc6 -- White is "us", so only 2 of the 4 moves count.
        moves = ["e2e4", "e7e5", "g1f3", "b8c6"]
        r = L.add_book(moves, sable_white=True)
        self.assertEqual(r["voted"], 2,
                         f"opponent moves were voted on too: {r}")
        votes = json.load(open(L.VOTES))
        self.assertEqual(len(votes), 2)

    def test_a_blunder_never_becomes_a_book_line(self):
        moves = ["e2e4", "e7e5"]
        key = L.key_of(chess.Board())
        # lose a queen on move one; the line must be skipped, not merely voted down
        r = L.add_book(moves, sable_white=True, blunders=[(key, "e2e4")],
                       min_votes=1)
        self.assertEqual(r["added"], 0)
        self.assertEqual(r["skipped_blunders"], 1)
        self.assertEqual(self.book_lines(), [])

    def test_one_game_is_not_evidence(self):
        moves = ["e2e4"]
        r1 = L.add_book(moves, sable_white=True)
        self.assertEqual(r1["added"], 0, "a single game wrote a book line")
        self.assertEqual(r1["voted"], 1)
        self.assertEqual(self.book_lines(), [])
        r2 = L.add_book(moves, sable_white=True)
        self.assertEqual(r2["added"], 1, "two agreeing games did not promote the line")
        self.assertEqual(len(self.book_lines()), 1)

    def test_votes_survive_and_are_not_double_counted_once_promoted(self):
        L.add_book(["e2e4"], sable_white=True)
        L.add_book(["e2e4"], sable_white=True)
        r3 = L.add_book(["e2e4"], sable_white=True)
        self.assertEqual(r3["added"], 0, "the same line was written twice")
        self.assertEqual(len(self.book_lines()), 1)
        self.assertEqual(r3["voted"], 0, "an already-written line was voted on again")

    def test_illegal_or_unparseable_moves_stop_the_scan(self):
        r = L.add_book(["e2e4", "e2e4"], sable_white=True)   # second is illegal
        self.assertEqual(r["voted"], 1)

    def test_stockfish_discovery_is_reported_not_guessed(self):
        # Whatever it finds, the contract is a real path or None -- and the error
        # message has to tell the user how to fix it, because the old code said
        # "stockfish missing: ../../bin/sf" and pointed at a file that never existed.
        found = L.find_stockfish()
        if found is not None:
            self.assertTrue(os.path.isfile(found), found)
        old = L.SF
        try:
            L.SF = "/nonexistent/stockfish"
            _, err = L.find_mistakes(["e2e4"], True)
            self.assertIsNotNone(err)
            self.assertIn("SABLE_SF", err)
        finally:
            L.SF = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
