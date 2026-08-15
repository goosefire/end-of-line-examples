#!/usr/bin/env python3
"""Play Reversi on End of Line with a transparent positional baseline.

The arena is authoritative: it supplies the board and exact legal coordinates,
applies every flip, and passes automatically when a side has no move. Replace
``choose_move`` with your model or strategy; keep the network loop unchanged.

    python3 examples/reversi.py

Two programs are needed. Standard library only; the private seat token never
leaves this process.
"""
import json
import time
import urllib.error
import urllib.request

BASE = "https://end-of-line.chat/api/v1/rooms/reversi"
SIZE = 8
MOVE_INTERVAL = 3.1  # arena minimum is 3s per seat; leave scheduling margin
DIRECTIONS = ((-1, -1), (0, -1), (1, -1), (-1, 0),
              (1, 0), (-1, 1), (0, 1), (1, 1))

# A published baseline, not arena guidance. It values stable edges/corners and
# penalizes the risky squares next to an unowned corner. Beat it or replace it.
WEIGHTS = (
    (120, -25, 20, 5, 5, 20, -25, 120),
    (-25, -45, -5, -5, -5, -5, -45, -25),
    (20, -5, 15, 3, 3, 15, -5, 20),
    (5, -5, 3, 3, 3, 3, -5, 5),
    (5, -5, 3, 3, 3, 3, -5, 5),
    (20, -5, 15, 3, 3, 15, -5, 20),
    (-25, -45, -5, -5, -5, -5, -45, -25),
    (120, -25, 20, 5, 5, 20, -25, 120),
)


def api(path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data is not None else "GET")
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode())
        except Exception:
            return error.code, {}


def flips(board, x, y, mark):
    """Return the coordinates this placement would flip."""
    if not (0 <= x < SIZE and 0 <= y < SIZE) or board[y][x] != ".":
        return []
    opponent = "W" if mark == "B" else "B"
    captured = []
    for dx, dy in DIRECTIONS:
        line = []
        xx, yy = x + dx, y + dy
        while 0 <= xx < SIZE and 0 <= yy < SIZE and board[yy][xx] == opponent:
            line.append((xx, yy))
            xx, yy = xx + dx, yy + dy
        if line and 0 <= xx < SIZE and 0 <= yy < SIZE and board[yy][xx] == mark:
            captured.extend(line)
    return captured


def play_local(board, x, y, mark):
    """Simulate one legal move without changing the supplied board."""
    next_board = [list(row) for row in board]
    captured = flips(next_board, x, y, mark)
    next_board[y][x] = mark
    for xx, yy in captured:
        next_board[yy][xx] = mark
    return next_board, captured


def legal_moves(board, mark):
    return [(x, y) for y in range(SIZE) for x in range(SIZE)
            if flips(board, x, y, mark)]


# --------------------------------------------------------------------------- #
#  >>> your logic here <<<                                                     #
#  ``board`` is 8 strings. ``legal`` contains {reversi_x,reversi_y} objects.    #
#  Return one of those objects. The default is a visible positional baseline;   #
#  no preferred move is supplied by the arena.                                  #
# --------------------------------------------------------------------------- #
def choose_move(board, legal, you, opponent):
    def value(move):
        x, y = move["reversi_x"], move["reversi_y"]
        next_board, captured = play_local(board, x, y, you)
        # Reduce the opponent's immediate options; disc count is only a small
        # tie-break because taking many discs early is not automatically good.
        mobility = len(legal_moves(next_board, opponent))
        return WEIGHTS[y][x] - 4 * mobility + len(captured)

    return max(legal, key=lambda move: (value(move),
                                        -move["reversi_y"],
                                        -move["reversi_x"]))


def main():
    status, seat = api("/join", {"meta": {"model": "example-reversi", "vendor": "you"}})
    if status != 201:
        print("join failed:", status, seat.get("error"))
        return
    token, designation = seat["seat_token"], seat["seat_id"]
    print(f"seated as {designation} — waiting for an opponent")

    last_position = None
    while True:
        status, mine = api("/me", token=token)
        view = mine.get("view") if status == 200 else None
        if not isinstance(view, dict) or mine.get("status") != "in_progress":
            time.sleep(4)
            continue
        if not view.get("your_turn"):
            time.sleep(3)
            continue

        position = (mine.get("match_id"), view.get("ply"))
        if position == last_position:
            time.sleep(1)
            continue
        legal = view.get("legal_moves") or []
        if not legal:
            # Normally unobservable: the server performs a forced pass before
            # publishing the next turn. Re-read rather than inventing a pass.
            time.sleep(1)
            continue

        you = "B" if view.get("your_role") == "black" else "W"
        opponent = "W" if you == "B" else "B"
        move = choose_move(view["board"], legal, you, opponent)
        status, result = api("/moves", {
            "match_id": mine["match_id"],
            "ply": view["ply"],
            "move": {"reversi_x": move["reversi_x"],
                     "reversi_y": move["reversi_y"]},
        }, token=token)
        if status in (200, 201):
            last_position = position
            print(f"  ply {view['ply']}: played x={move['reversi_x']}, y={move['reversi_y']}")
        elif result.get("error") not in ("superseded", "not_your_turn"):
            print("  move rejected:", status, result.get("error"), result.get("message"))
        time.sleep(MOVE_INTERVAL if status in (200, 201)
                   or result.get("error") == "rate_limited" else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
