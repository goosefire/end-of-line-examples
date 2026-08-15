#!/usr/bin/env python3
"""Offline rules, perft, view, and policy checks for public Checkers clients."""
import importlib.util
from pathlib import Path
import random
import unittest

import checkers_player as cp


_EXAMPLE_SPEC = importlib.util.spec_from_file_location(
    "public_checkers_example", Path(__file__).parents[1] / "examples" / "checkers.py")
example = importlib.util.module_from_spec(_EXAMPLE_SPEC)
_EXAMPLE_SPEC.loader.exec_module(example)


def position(entries):
    board = ["."] * 32
    for square, piece in entries:
        board[square - 1] = piece
    return tuple(board)


def perft(board, side, depth):
    if depth == 0:
        return 1
    return sum(perft(cp.apply_path(board, path)[0], cp.other(side), depth - 1)
               for path in cp.legal_paths(board, side))


class NumberingAndRules(unittest.TestCase):
    def test_official_square_anchors_and_inverse(self):
        self.assertEqual(cp.coords(1), (0, 1))
        self.assertEqual(cp.coords(5), (1, 0))
        self.assertEqual(cp.coords(32), (7, 6))
        for square in range(1, 33):
            self.assertEqual(cp.square_at(*cp.coords(square)), square)

    def test_standard_opening_and_published_perft(self):
        board = cp.initial_board()
        self.assertEqual(cp.legal_paths(board, "red"), (
            (9, 13), (9, 14), (10, 14), (10, 15),
            (11, 15), (11, 16), (12, 16),
        ))
        expected = (7, 49, 302, 1469, 7361, 36768)
        self.assertEqual(tuple(perft(board, "red", depth)
                               for depth in range(1, 7)), expected)

    def test_capture_suppresses_quiet_moves(self):
        board = position(((10, "r"), (11, "r"), (14, "w"), (32, "W")))
        self.assertEqual(cp.legal_paths(board, "red"), ((10, 17),))
        after, captured, promoted = cp.apply_path(board, (10, 17))
        self.assertEqual(captured, (14,))
        self.assertFalse(promoted)
        self.assertEqual((after[9], after[13], after[16]), (".", ".", "r"))

    def test_complete_multi_jump_is_one_move(self):
        board = position(((9, "r"), (14, "w"), (23, "w"), (32, "W")))
        self.assertEqual(cp.legal_paths(board, "red"), ((9, 18, 27),))
        after, captured, _ = cp.apply_path(board, (9, 18, 27))
        self.assertEqual(captured, (14, 23))
        self.assertEqual(after[26], "r")

    def test_any_complete_capture_not_maximum_capture(self):
        board = position(((10, "r"), (14, "w"), (22, "w"),
                          (15, "w"), (32, "W")))
        self.assertEqual(cp.legal_paths(board, "red"),
                         ((10, 17, 26), (10, 19)))

    def test_men_capture_forward_kings_both_directions(self):
        man = position(((18, "r"), (14, "w"), (32, "W")))
        king = position(((18, "R"), (14, "w"), (32, "W")))
        self.assertNotIn((18, 9), cp.legal_paths(man, "red"))
        self.assertEqual(cp.legal_paths(king, "red"), ((18, 9),))

    def test_capture_into_king_row_ends_the_turn(self):
        board = position(((22, "r"), (26, "w"), (27, "w")))
        self.assertEqual(cp.legal_paths(board, "red"), ((22, 31),))
        after, captured, promoted = cp.apply_path(board, (22, 31))
        self.assertEqual(captured, (26,))
        self.assertTrue(promoted)
        self.assertEqual((after[30], after[26]), ("R", "w"))


class PoliciesAndContract(unittest.TestCase):
    def test_every_policy_returns_an_arena_supplied_complete_path(self):
        board = cp.initial_board()
        legal = [{"checkers_path": list(path)} for path in cp.legal_paths(board, "red")]
        for policy in ("random", "greedy", "positional", "search"):
            with self.subTest(policy=policy):
                chosen = cp.choose_move(board, legal, "red", policy, depth=3,
                                        rng=random.Random(7))
                self.assertIn(chosen, cp.legal_paths(board, "red"))

    def test_random_policy_is_reproducible(self):
        legal = cp.legal_paths(cp.initial_board(), "red")
        a = cp.choose_move(cp.initial_board(), legal, "red", "random",
                           rng=random.Random(42))
        b = cp.choose_move(cp.initial_board(), legal, "red", "random",
                           rng=random.Random(42))
        self.assertEqual(a, b)

    def test_all_policies_take_the_only_path(self):
        board = position(((10, "r"), (14, "w"), (32, "W")))
        legal = ({"checkers_path": [10, 17]},)
        for policy in ("random", "greedy", "positional", "search"):
            with self.subTest(policy=policy):
                self.assertEqual(cp.choose_move(board, legal, "red", policy,
                                                depth=3, rng=random.Random(1)), (10, 17))

    def test_search_understands_no_move_is_terminal(self):
        red_wins = position(((13, "r"),))
        white_wins = position(((20, "w"),))
        self.assertGreater(cp.minimax(red_wins, "white", "red", 2,
                                      -10**9, 10**9), 0)
        self.assertLess(cp.minimax(white_wins, "red", "red", 2,
                                   -10**9, 10**9), 0)

    def test_unknown_policy_fails_loudly(self):
        with self.assertRaises(ValueError):
            cp.choose_move(cp.initial_board(), ((9, 13),), "red", "mystery")

    def test_arena_piece_names_normalize_to_exact_32_square_position(self):
        names = (["red_man"] * 12 + ["empty"] * 8 + ["white_man"] * 12)
        board = cp.normalize_board({"notation": "wcdf-1-32", "squares": names})
        self.assertEqual(board, cp.initial_board())
        self.assertEqual(len(cp.position_text(board).split()), 32)
        self.assertNotEqual(cp.position_hash(board, "red"), cp.position_hash(board, "white"))

    def test_copyable_example_uses_namespaced_complete_path(self):
        names = (["red_man"] * 12 + ["empty"] * 8 + ["white_man"] * 12)
        view = {"board": {"squares": names}}
        board = example.numbered_board(view)
        legal = [{"checkers_path": list(path)} for path in cp.legal_paths(board, "red")]
        self.assertIn(example.choose_move(board, legal, "red"), legal)

    def test_public_clients_respect_arena_move_interval(self):
        self.assertGreater(cp.MOVE_INTERVAL, 3)
        self.assertGreater(example.MOVE_INTERVAL, 3)


if __name__ == "__main__":
    unittest.main()
