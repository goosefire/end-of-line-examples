#!/usr/bin/env python3
"""Offline checks for the four public Reversi evaluation baselines."""
import random
import importlib.util
from pathlib import Path
import unittest

import reversi_player as rp


_EXAMPLE_SPEC = importlib.util.spec_from_file_location(
    "public_reversi_example", Path(__file__).parents[1] / "examples" / "reversi.py")
example = importlib.util.module_from_spec(_EXAMPLE_SPEC)
_EXAMPLE_SPEC.loader.exec_module(example)


OPENING = (
    "........", "........", "........", "...WB...",
    "...BW...", "........", "........", "........",
)


class Rules(unittest.TestCase):
    def test_standard_opening_moves(self):
        self.assertEqual(rp.legal_moves(OPENING, "B"),
                         ((3, 2), (2, 3), (5, 4), (4, 5)))

    def test_opening_flip(self):
        board, captured = rp.play_local(OPENING, (2, 3), "B")
        self.assertEqual(captured, ((3, 3),))
        self.assertEqual(board[3][2:4], "BB")
        self.assertEqual(OPENING[3][2:4], ".W")

    def test_one_placement_flips_all_eight_lines(self):
        board = [["." for _ in range(8)] for _ in range(8)]
        for dx, dy in rp.DIRECTIONS:
            board[3 + dy][3 + dx] = "W"
            board[3 + 2 * dy][3 + 2 * dx] = "B"
        board = rp.normalize(board)
        next_board, captured = rp.play_local(board, (3, 3), "B")
        self.assertEqual(len(captured), 8)
        self.assertTrue(all(next_board[y][x] == "B" for x, y in captured))

    def test_non_bracketing_move_is_not_legal(self):
        self.assertEqual(rp.flips(OPENING, 0, 0, "B"), ())
        with self.assertRaises(ValueError):
            rp.play_local(OPENING, (0, 0), "B")

    def test_forced_pass_then_finish(self):
        board = [["B" for _ in range(8)] for _ in range(8)]
        board[0][0], board[0][1] = ".", "W"
        board[0][3], board[0][4] = ".", "W"
        board = rp.normalize(board)
        after, _ = rp.play_local(board, (0, 0), "B")
        self.assertEqual(rp.legal_moves(after, "W"), ())
        self.assertEqual(rp.legal_moves(after, "B"), ((3, 0),))
        final, _ = rp.play_local(after, (3, 0), "B")
        self.assertEqual(rp.legal_moves(final, "B"), ())
        self.assertEqual(rp.legal_moves(final, "W"), ())
        self.assertEqual(rp.disc_difference(final, "B"), 64)


class Policies(unittest.TestCase):
    def test_every_policy_returns_an_arena_legal_move(self):
        legal = [{"reversi_x": x, "reversi_y": y}
                 for x, y in rp.legal_moves(OPENING, "B")]
        for policy in ("random", "greedy", "positional", "search"):
            with self.subTest(policy=policy):
                move = rp.choose_move(OPENING, legal, "B", policy, depth=3,
                                      rng=random.Random(7))
                self.assertIn(move, rp.legal_moves(OPENING, "B"))

    def test_random_policy_is_reproducible_with_supplied_rng(self):
        legal = rp.legal_moves(OPENING, "B")
        a = rp.choose_move(OPENING, legal, "B", "random", rng=random.Random(42))
        b = rp.choose_move(OPENING, legal, "B", "random", rng=random.Random(42))
        self.assertEqual(a, b)

    def test_all_policies_take_the_only_move(self):
        board = [["B" for _ in range(8)] for _ in range(8)]
        board[0][3], board[0][4] = ".", "W"
        board = rp.normalize(board)
        for policy in ("random", "greedy", "positional", "search"):
            with self.subTest(policy=policy):
                self.assertEqual(rp.choose_move(board, ((3, 0),), "B", policy,
                                                rng=random.Random(1)), (3, 0))

    def test_minimax_understands_terminal_disc_majority(self):
        black = tuple("BBBBBBBB" for _ in range(8))
        white = tuple("WWWWWWWW" for _ in range(8))
        self.assertGreater(rp.minimax(black, "W", "B", 2, -10**9, 10**9), 0)
        self.assertLess(rp.minimax(white, "B", "B", 2, -10**9, 10**9), 0)

    def test_unknown_policy_fails_loudly(self):
        with self.assertRaises(ValueError):
            rp.choose_move(OPENING, ((3, 2),), "B", "mystery")

    def test_copyable_example_uses_the_namespaced_arena_coordinates(self):
        legal = [{"reversi_x": x, "reversi_y": y}
                 for x, y in rp.legal_moves(OPENING, "B")]
        self.assertIn(example.choose_move(OPENING, legal, "B", "W"), legal)

    def test_public_clients_respect_the_arena_move_interval(self):
        self.assertGreater(rp.MOVE_INTERVAL, 3)
        self.assertGreater(example.MOVE_INTERVAL, 3)


if __name__ == "__main__":
    unittest.main()
