#!/usr/bin/env python3
"""Offline contract, prompt, and policy checks for public Chess clients."""
import importlib.util
import json
from pathlib import Path
import random
import unittest
from unittest import mock

import chess_player as cp


_EXAMPLE_SPEC = importlib.util.spec_from_file_location(
    "public_chess_example", Path(__file__).parents[1] / "examples" / "chess.py")
example = importlib.util.module_from_spec(_EXAMPLE_SPEC)
_EXAMPLE_SPEC.loader.exec_module(example)


def view(entries=(), legal=(), role="white", en_passant=None):
    cells = {f"{file_name}{rank}": "empty"
             for rank in range(1, 9) for file_name in "abcdefgh"}
    cells.update(entries)
    rows = [{
        "rank": rank,
        "squares": [cells[f"{file_name}{rank}"] for file_name in "abcdefgh"],
    } for rank in range(8, 0, -1)]
    return {
        "game": "chess", "your_role": role, "your_turn": True, "ply": 12,
        "board": {"files": list("abcdefgh"), "rows": rows,
                  "fen": "8/8/8/8/8/8/8/8 w - - 0 1"},
        "en_passant_target": en_passant, "in_check": False,
        "legal_moves": list(legal),
    }


LEGAL = (
    {"chess_from": "e2", "chess_to": "e3"},
    {"chess_from": "e2", "chess_to": "e4"},
)


class BoardAndPolicies(unittest.TestCase):
    def test_all_64_squares_and_orientation_are_preserved(self):
        state = view({"a8": "black_rook", "e1": "white_king"}, LEGAL)
        board = cp.board_by_square(state)
        self.assertEqual(len(board), 64)
        self.assertEqual((board["a8"], board["e1"]), ("black_rook", "white_king"))
        self.assertEqual(example.board_by_square(state), board)

    def test_malformed_or_incomplete_board_fails(self):
        state = view(legal=LEGAL)
        state["board"]["rows"] = state["board"]["rows"][:-1]
        with self.assertRaises(ValueError):
            cp.board_by_square(state)
        with self.assertRaises(ValueError):
            example.board_by_square(state)

    def test_material_policy_takes_capture(self):
        legal = (
            {"chess_from": "d4", "chess_to": "d5"},
            {"chess_from": "d4", "chess_to": "e5"},
        )
        state = view({"d4": "white_pawn", "e5": "black_queen"}, legal)
        self.assertEqual(cp.material_move(state, list(legal)), legal[1])
        self.assertEqual(example.choose_move(example.board_by_square(state), list(legal), state),
                         legal[1])

    def test_material_policy_understands_en_passant(self):
        legal = (
            {"chess_from": "e5", "chess_to": "e6"},
            {"chess_from": "e5", "chess_to": "d6"},
        )
        state = view({"e5": "white_pawn", "d5": "black_pawn"}, legal,
                     en_passant="d6")
        self.assertEqual(cp.material_move(state, list(legal)), legal[1])

    def test_material_policy_prefers_queen_promotion(self):
        legal = tuple({"chess_from": "a7", "chess_to": "a8",
                       "chess_promotion": piece}
                      for piece in ("bishop", "knight", "queen", "rook"))
        state = view({"a7": "white_pawn"}, legal)
        self.assertEqual(cp.material_move(state, list(legal))["chess_promotion"], "queen")

    def test_random_policy_is_reproducible_and_legal(self):
        state = view({"e2": "white_pawn"}, LEGAL)
        a = cp.choose_move(state, "random", random.Random(42))[0]
        b = cp.choose_move(state, "random", random.Random(42))[0]
        self.assertEqual(a, b)
        self.assertIn(a, LEGAL)


class ModelContract(unittest.TestCase):
    def test_json_move_must_match_one_complete_legal_object(self):
        self.assertEqual(cp.parsed_move('{"chess_from":"e2","chess_to":"e4"}',
                                        list(LEGAL)), LEGAL[1])
        self.assertIsNone(cp.parsed_move(
            '{"chess_from":"e2","chess_to":"e4","explanation":"best"}',
            list(LEGAL)))
        self.assertIsNone(cp.parsed_move(
            '{"chess_from":"e2","chess_to":"e5"}', list(LEGAL)))

    def test_promotion_cannot_be_omitted_or_invented(self):
        promotion = [{"chess_from": "a7", "chess_to": "a8",
                      "chess_promotion": "knight"}]
        self.assertIsNone(cp.parsed_move(
            '{"chess_from":"a7","chess_to":"a8"}', promotion))
        self.assertEqual(cp.parsed_move(
            '```json\n{"chess_from":"a7","chess_to":"a8",'
            '"chess_promotion":"knight"}\n```', promotion), promotion[0])

    def test_model_choice_uses_exact_legal_output(self):
        state = view({"e2": "white_pawn"}, LEGAL)
        brief = {"rules": ["arena rule"], "preparation": ["neutral note"]}
        with mock.patch.object(cp, "generate", return_value=(
                '{"chess_from":"e2","chess_to":"e3"}')):
            move, source, _ = cp.choose_move(
                state, "model", random.Random(1), "key", "model", brief)
        self.assertEqual((move, source), (LEGAL[0], "model"))

    def test_malformed_model_output_uses_published_material_fallback(self):
        legal = (
            {"chess_from": "d4", "chess_to": "d5"},
            {"chess_from": "d4", "chess_to": "e5"},
        )
        state = view({"d4": "white_pawn", "e5": "black_rook"}, legal)
        brief = {"rules": ["arena rule"], "preparation": ["neutral note"]}
        with mock.patch.object(cp, "generate", return_value="not a move"):
            move, source, output = cp.choose_move(
                state, "model", random.Random(1), "key", "model", brief)
        self.assertEqual((move, source, output), (legal[1], "material_fallback", "not a move"))

    def test_prompt_uses_arena_material_without_local_chess_advice(self):
        state = view({"e2": "white_pawn"}, LEGAL, role="black")
        system, user = cp.prompt_for(state, list(LEGAL), {
            "rules": ["ARENA RULE SENTINEL"],
            "preparation": ["ARENA PREPARATION SENTINEL"],
        })
        self.assertIn("ARENA RULE SENTINEL", system)
        self.assertIn("ARENA PREPARATION SENTINEL", system)
        self.assertIn("Your role: black", user)
        self.assertIn(json.dumps(list(LEGAL), separators=(",", ":")), user)
        self.assertNotRegex(system + user,
                            r"Sicilian|Ruy Lopez|control the cent|develop your|prefer e4")

    def test_every_client_respects_arena_move_interval(self):
        self.assertGreater(cp.MOVE_INTERVAL, 3)
        self.assertGreater(example.MOVE_INTERVAL, 3)


if __name__ == "__main__":
    unittest.main()
