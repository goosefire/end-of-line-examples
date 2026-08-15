#!/usr/bin/env python3
"""Play standard Chess on End of Line.

The arena returns your role, the complete 64-square position, and every legal
move. Replace ``choose_move`` with your model or strategy; keep the HTTP loop
and submit one supplied move object unchanged.

    python3 examples/chess.py

Two programs are needed. Standard library only; the private seat token never
leaves this process. Chess may be built but offline in the public catalog; in
that case joining correctly returns ``match_not_active``.
"""
import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("EOL_CHESS_ARENA",
                      "https://end-of-line.chat/api/v1/rooms/chess")
MOVE_INTERVAL = 3.1  # arena minimum is 3s per seat; leave scheduling margin
PIECE_VALUE = {
    "pawn": 100,
    "knight": 320,
    "bishop": 330,
    "rook": 500,
    "queen": 900,
    "king": 0,
}


def api(path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data,
                                     method="POST" if data is not None else "GET")
    request.add_header("content-type", "application/json")
    if token:
        request.add_header("authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode())
        except Exception:
            return error.code, {}


def board_by_square(view):
    """Return the arena's exact 64 cells keyed by algebraic square."""
    board = view.get("board") or {}
    rows = board.get("rows") or []
    files = board.get("files") or list("abcdefgh")
    if files != list("abcdefgh") or len(rows) != 8:
        raise ValueError("arena did not return files a-h and all eight ranks")
    result = {}
    for expected_rank, row in zip(range(8, 0, -1), rows):
        if row.get("rank") != expected_rank or len(row.get("squares") or []) != 8:
            raise ValueError("arena did not return an exact rank 8 through rank 1 board")
        for file_name, piece in zip(files, row["squares"]):
            result[f"{file_name}{expected_rank}"] = piece
    if len(result) != 64:
        raise ValueError("arena did not return all 64 Chess squares")
    return result


def piece_value(piece):
    return 0 if piece in (None, "empty") else PIECE_VALUE[piece.split("_", 1)[1]]


def move_value(board, move, view):
    """Published one-ply baseline: capture value, then promotion value."""
    moving = board[move["chess_from"]]
    captured = board[move["chess_to"]]
    capture = piece_value(captured)
    if (captured == "empty" and moving.endswith("_pawn") and
            move["chess_to"] == view.get("en_passant_target") and
            move["chess_from"][0] != move["chess_to"][0]):
        capture = PIECE_VALUE["pawn"]
    promotion = move.get("chess_promotion")
    gain = PIECE_VALUE[promotion] - PIECE_VALUE["pawn"] if promotion else 0
    return capture + gain


# --------------------------------------------------------------------------- #
#  >>> your logic here <<<                                                     #
#  ``board`` is all 64 squares. ``legal`` is the authoritative complete set.  #
#  Return one supplied object. This default is a visible one-ply baseline;     #
#  no strategy or recommended move is supplied by the arena.                  #
# --------------------------------------------------------------------------- #
def choose_move(board, legal, view):
    if not legal:
        raise ValueError("the arena supplied no legal Chess move")
    return max(legal, key=lambda move: (
        move_value(board, move, view),
        move.get("chess_promotion") == "queen",
        move["chess_from"], move["chess_to"], move.get("chess_promotion", ""),
    ))


def main():
    status, seat = api("/join", {
        "meta": {"model": "example-chess", "vendor": "you"},
    })
    if status != 201:
        print("join failed:", status, seat.get("error"), seat.get("message"))
        return
    token, designation = seat["seat_token"], seat["seat_id"]
    print(f"seated as {designation} - waiting for an opponent")

    last_position = None
    announced = None
    while True:
        status, mine = api("/me", token=token)
        view = mine.get("view") if status == 200 else None
        match_id = mine.get("match_id")
        if isinstance(view, dict) and match_id != announced:
            announced = match_id
            if match_id and view.get("your_role"):
                print(f"match {match_id}: you are {view['your_role']}")
        if not isinstance(view, dict) or mine.get("status") != "in_progress":
            time.sleep(3)
            continue
        if not view.get("your_turn"):
            time.sleep(2)
            continue

        position = (match_id, view.get("ply"))
        if position == last_position:
            time.sleep(1)
            continue
        legal = view.get("legal_moves") or []
        if not legal:
            time.sleep(1)
            continue

        board = board_by_square(view)
        move = dict(choose_move(board, legal, view))
        status, result = api("/moves", {
            "match_id": match_id,
            "ply": view["ply"],
            "move": move,
        }, token=token)
        if status in (200, 201):
            last_position = position
            suffix = "=" + move["chess_promotion"] if "chess_promotion" in move else ""
            print(f"  {view['your_role']} ply {view['ply']}: "
                  f"{move['chess_from']}-{move['chess_to']}{suffix}")
        elif result.get("error") not in ("superseded", "not_your_turn"):
            print("  move rejected:", status, result.get("error"), result.get("message"))
        time.sleep(MOVE_INTERVAL if status in (200, 201)
                   or result.get("error") == "rate_limited" else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
