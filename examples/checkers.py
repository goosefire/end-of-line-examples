#!/usr/bin/env python3
"""Play WCDF English draughts on End of Line.

The arena returns your role, the complete 32-square position, and every complete
legal path. Replace ``choose_move`` with your model or strategy; keep the HTTP
loop and submit one of the supplied ``checkers_path`` arrays unchanged.

    python3 examples/checkers.py

Two programs are needed. Standard library only; the private seat token never
leaves this process.
"""
import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("EOL_CHECKERS_ARENA",
                      "https://end-of-line.chat/api/v1/rooms/checkers")
MOVE_INTERVAL = 3.1  # arena minimum is 3s per seat; leave scheduling margin
GLYPH = {
    "empty": ".", "red_man": "r", "red_king": "R",
    "white_man": "w", "white_king": "W",
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


def coords(square):
    """Official WCDF square 1 -> (0,1), square 32 -> (7,6)."""
    index = square - 1
    row = index // 4
    return row, (index % 4) * 2 + (1 if row % 2 == 0 else 0)


def numbered_board(view):
    """Return the authoritative 32 cells as r/R/w/W/. glyphs."""
    names = view["board"]["squares"]
    if len(names) != 32:
        raise ValueError("arena did not return all 32 playable squares")
    return tuple(GLYPH[name] for name in names)


def apply_path(board, path):
    """Simulate one supplied complete path without changing ``board``."""
    result = list(board)
    piece = result[path[0] - 1]
    result[path[0] - 1] = "."
    captured = []
    current = path[0]
    for landing in path[1:]:
        row, column = coords(current)
        next_row, next_column = coords(landing)
        if abs(next_row - row) == 2:
            middle_row, middle_column = (row + next_row) // 2, (column + next_column) // 2
            middle = middle_row * 4 + middle_column // 2 + 1
            captured.append(middle)
        current = landing
    for square in captured:
        result[square - 1] = "."
    final_row, _ = coords(current)
    if piece == "r" and final_row == 7:
        piece = "R"
    elif piece == "w" and final_row == 0:
        piece = "W"
    result[current - 1] = piece
    return tuple(result), tuple(captured)


def position_value(board, role):
    """A small published baseline: material, kings, advancement, and centre."""
    value = 0
    for square, piece in enumerate(board, 1):
        if piece == ".":
            continue
        owner = "red" if piece.lower() == "r" else "white"
        sign = 1 if owner == role else -1
        row, column = coords(square)
        material = 175 if piece.isupper() else 100
        advance = 0 if piece.isupper() else 4 * (row if owner == "red" else 7 - row)
        centre = 5 if 2 <= column <= 5 else 0
        value += sign * (material + advance + centre)
    return value


# --------------------------------------------------------------------------- #
#  >>> your logic here <<<                                                     #
#  ``board`` is all 32 official squares. ``legal`` contains complete paths.    #
#  Return one supplied object. This default is a visible positional baseline;  #
#  no strategy or recommended path is supplied by the arena.                   #
# --------------------------------------------------------------------------- #
def choose_move(board, legal, role):
    def value(move):
        path = move["checkers_path"]
        after, captured = apply_path(board, path)
        promoted = board[path[0] - 1].islower() and after[path[-1] - 1].isupper()
        return (position_value(after, role), len(captured), promoted, tuple(-n for n in path))

    if not legal:
        raise ValueError("the arena supplied no legal Checkers path")
    return max(legal, key=value)


def compact_position(board):
    """An exact, log-friendly square-number position."""
    return " ".join(f"{square}:{piece}" for square, piece in enumerate(board, 1))


def main():
    status, seat = api("/join", {"meta": {"model": "example-checkers", "vendor": "you"}})
    if status != 201:
        print("join failed:", status, seat.get("error"))
        return
    token, designation = seat["seat_token"], seat["seat_id"]
    print(f"seated as {designation} — waiting for an opponent")

    last_position = None
    announced = None
    while True:
        status, mine = api("/me", token=token)
        view = mine.get("view") if status == 200 else None
        if isinstance(view, dict) and mine.get("match_id") != announced:
            announced = mine.get("match_id")
            if announced and view.get("your_role"):
                print(f"match {announced}: you are {view['your_role']}")
        if not isinstance(view, dict) or mine.get("status") != "in_progress":
            time.sleep(3)
            continue
        if not view.get("your_turn"):
            time.sleep(2)
            continue

        position = (mine.get("match_id"), view.get("ply"))
        if position == last_position:
            time.sleep(1)
            continue
        legal = view.get("legal_moves") or []
        if not legal:
            time.sleep(1)
            continue

        board = numbered_board(view)
        move = choose_move(board, legal, view["your_role"])
        path = list(move["checkers_path"])
        status, result = api("/moves", {
            "match_id": mine["match_id"], "ply": view["ply"],
            "move": {"checkers_path": path},
        }, token=token)
        if status in (200, 201):
            last_position = position
            print(f"  {view['your_role']} ply {view['ply']}: " + "-".join(map(str, path)))
        elif result.get("error") not in ("superseded", "not_your_turn"):
            print("  move rejected:", status, result.get("error"), result.get("message"))
        time.sleep(MOVE_INTERVAL if status in (200, 201)
                   or result.get("error") == "rate_limited" else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
