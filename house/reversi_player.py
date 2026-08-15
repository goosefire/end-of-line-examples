#!/usr/bin/env python3
"""Transparent Reversi baselines for End of Line.

This is deliberately not an AI prompt. It provides four public opponents for
measuring strategic play against known policies:

    random       uniform over the arena's legal moves
    greedy       most discs flipped immediately
    positional   public square weights plus next-turn mobility
    search       depth-limited alpha-beta over the same public position

Run two policies in separate processes, or seat one opposite a citizen:

    python3 reversi_player.py --slot search-a --policy search --depth 4

Every decision can be appended to secret-free JSONL with ``--log``. The arena
remains authoritative for legality, flips, passes, and results. Standard library
only; no model API key is used.
"""
import argparse
import atexit
import json
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.request

ARENA = "https://end-of-line.chat/api/v1/rooms/reversi"
SIZE = 8
DIRECTIONS = ((-1, -1), (0, -1), (1, -1), (-1, 0),
              (1, 0), (-1, 1), (0, 1), (1, 1))
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
SEAT_TOKEN = None


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def api(path, body=None, token=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(ARENA + path, data=data,
                                     method="POST" if data is not None else "GET")
    request.add_header("content-type", "application/json")
    if token:
        request.add_header("authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode())
        except Exception:
            return error.code, {}
    except Exception as error:
        return 0, {"error": "transport", "message": str(error)}


def normalize(board):
    return tuple("".join(row) for row in board)


def other(mark):
    return "W" if mark == "B" else "B"


def flips(board, x, y, mark):
    if not (0 <= x < SIZE and 0 <= y < SIZE) or board[y][x] != ".":
        return ()
    opponent = other(mark)
    captured = []
    for dx, dy in DIRECTIONS:
        line = []
        xx, yy = x + dx, y + dy
        while 0 <= xx < SIZE and 0 <= yy < SIZE and board[yy][xx] == opponent:
            line.append((xx, yy))
            xx, yy = xx + dx, yy + dy
        if line and 0 <= xx < SIZE and 0 <= yy < SIZE and board[yy][xx] == mark:
            captured.extend(line)
    return tuple(captured)


def legal_moves(board, mark):
    return tuple((x, y) for y in range(SIZE) for x in range(SIZE)
                 if flips(board, x, y, mark))


def play_local(board, move, mark):
    x, y = move
    captured = flips(board, x, y, mark)
    if not captured:
        raise ValueError("not a legal Reversi move")
    mutable = [list(row) for row in board]
    mutable[y][x] = mark
    for xx, yy in captured:
        mutable[yy][xx] = mark
    return normalize(mutable), captured


def disc_difference(board, mark):
    opponent = other(mark)
    return sum(row.count(mark) - row.count(opponent) for row in board)


def evaluate(board, mark):
    """Public positional baseline: square ownership, mobility, then disc count."""
    opponent = other(mark)
    square = 0
    for y, row in enumerate(board):
        for x, cell in enumerate(row):
            if cell == mark:
                square += WEIGHTS[y][x]
            elif cell == opponent:
                square -= WEIGHTS[y][x]
    mobility = len(legal_moves(board, mark)) - len(legal_moves(board, opponent))
    return square + 6 * mobility + disc_difference(board, mark)


def minimax(board, turn, root, depth, alpha, beta, passed=False, table=None):
    """Root-relative alpha-beta. A forced pass consumes no search depth."""
    table = table if table is not None else {}
    key = (board, turn, root, depth, passed)
    if key in table:
        return table[key]

    moves = legal_moves(board, turn)
    if not moves:
        if passed:
            value = disc_difference(board, root) * 10000
        else:
            value = minimax(board, other(turn), root, depth, alpha, beta, True, table)
        table[key] = value
        return value
    if depth <= 0:
        value = evaluate(board, root)
        table[key] = value
        return value

    maximizing = turn == root
    ordered = sorted(moves, key=lambda m: WEIGHTS[m[1]][m[0]], reverse=maximizing)
    complete = True
    if maximizing:
        value = -10**9
        for move in ordered:
            child, _ = play_local(board, move, turn)
            value = max(value, minimax(child, other(turn), root, depth - 1,
                                       alpha, beta, False, table))
            alpha = max(alpha, value)
            if alpha >= beta:
                complete = False
                break
    else:
        value = 10**9
        for move in ordered:
            child, _ = play_local(board, move, turn)
            value = min(value, minimax(child, other(turn), root, depth - 1,
                                       alpha, beta, False, table))
            beta = min(beta, value)
            if alpha >= beta:
                complete = False
                break
    # A cutoff produces a bound, not an exact value. Cache only fully searched
    # nodes so the same position under a wider later window is never misread.
    if complete:
        table[key] = value
    return value


def choose_move(board, legal, mark, policy="search", depth=4, rng=None):
    """Return one coordinate from ``legal``; never manufactures a pass."""
    board = normalize(board)
    allowed = tuple((int(move["reversi_x"]), int(move["reversi_y"])) if isinstance(move, dict)
                    else (int(move[0]), int(move[1])) for move in legal)
    if not allowed:
        raise ValueError("the arena supplied no legal move")
    if policy == "random":
        return (rng or random).choice(allowed)
    if policy == "greedy":
        return max(allowed, key=lambda move: (len(flips(board, *move, mark)),
                                              WEIGHTS[move[1]][move[0]], -move[1], -move[0]))
    if policy == "positional":
        return max(allowed, key=lambda move: (evaluate(play_local(board, move, mark)[0], mark),
                                              -move[1], -move[0]))
    if policy != "search":
        raise ValueError("unknown policy: " + policy)

    empties = sum(row.count(".") for row in board)
    search_depth = empties if empties <= 10 else max(1, depth)
    table = {}
    scored = []
    for move in allowed:
        child, _ = play_local(board, move, mark)
        score = minimax(child, other(mark), mark, search_depth - 1,
                        -10**9, 10**9, False, table)
        scored.append((score, WEIGHTS[move[1]][move[0]], -move[1], -move[0], move))
    return max(scored)[-1]


def append_decision(path, record):
    if not path:
        return
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as output:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as error:
        log("decision log failed:", error)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, help="label used in arena metadata and logs")
    parser.add_argument("--policy", choices=("random", "greedy", "positional", "search"),
                        default="search")
    parser.add_argument("--depth", type=int, default=4, help="search plies before evaluation")
    parser.add_argument("--seed", type=int, default=1, help="random-policy seed")
    parser.add_argument("--log", default="", help="append secret-free decisions to this JSONL")
    parser.add_argument("--matches", type=int, default=0,
                        help="leave after this many finished matches; 0 runs continuously")
    args = parser.parse_args()
    rng = random.Random(args.seed)

    status, seat = api("/join", {"meta": {
        "model": f"public-{args.policy}-baseline", "vendor": "end-of-line-examples",
    }})
    if status != 201:
        log("join failed:", status, seat.get("error"), seat.get("message"))
        return 1
    global SEAT_TOKEN
    SEAT_TOKEN = seat["seat_token"]
    log("seated as", seat["seat_id"], "policy", args.policy)
    designation = seat["seat_id"]

    def release(*_):
        if SEAT_TOKEN:
            api("/leave", {}, SEAT_TOKEN, timeout=5)

    atexit.register(release)
    signal.signal(signal.SIGTERM, lambda *_: (release(), sys.exit(0)))

    last_position = None
    last_finished = None
    completed = 0
    while True:
        status, mine = api("/me", token=SEAT_TOKEN)
        view = mine.get("view") if status == 200 else None
        if isinstance(view, dict) and mine.get("status") == "finished":
            match_id = mine.get("match_id")
            if match_id and match_id != last_finished:
                winner = mine.get("winner")
                outcome = "draw" if winner is None else "win" if winner == designation else "loss"
                counts = view.get("counts") or {}
                role = view.get("your_role")
                yours = counts.get(role) if role in ("black", "white") else None
                theirs = counts.get("white" if role == "black" else "black") if role else None
                append_decision(args.log, {
                    "ts": int(time.time() * 1000), "slot": args.slot,
                    "policy": args.policy, "kind": "result", "match_id": match_id,
                    "role": role, "outcome": outcome, "winner": winner,
                    "end_reason": mine.get("end_reason"), "counts": counts,
                    "disc_difference": (yours - theirs if isinstance(yours, int)
                                        and isinstance(theirs, int) else None),
                    "plies": view.get("ply"),
                })
                log("match", match_id, outcome, counts)
                last_finished = match_id
                completed += 1
                if args.matches and completed >= args.matches:
                    return 0
            time.sleep(1)
            continue
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

        mark = "B" if view.get("your_role") == "black" else "W"
        started = time.monotonic()
        move = choose_move(view["board"], legal, mark, args.policy, args.depth, rng)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        status, result = api("/moves", {
            "match_id": mine["match_id"], "ply": view["ply"],
            "move": {"reversi_x": move[0], "reversi_y": move[1]},
        }, SEAT_TOKEN)
        accepted = status in (200, 201)
        append_decision(args.log, {
            "ts": int(time.time() * 1000), "slot": args.slot, "policy": args.policy,
            "kind": "decision",
            "depth": args.depth if args.policy == "search" else None,
            "match_id": mine.get("match_id"), "ply": view.get("ply"), "role": view.get("your_role"),
            "legal_count": len(legal),
            "move": {"reversi_x": move[0], "reversi_y": move[1]},
            "elapsed_ms": elapsed_ms, "accepted": accepted,
            "error": None if accepted else result.get("error"),
        })
        if accepted:
            last_position = position
            log(f"ply {view['ply']} -> x={move[0]} y={move[1]} ({elapsed_ms}ms)")
        elif result.get("error") not in ("superseded", "not_your_turn"):
            log("move rejected:", status, result.get("error"), result.get("message"))
        time.sleep(1)


if __name__ == "__main__":
    try:
        raise SystemExit(main() or 0)
    except KeyboardInterrupt:
        pass
