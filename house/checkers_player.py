#!/usr/bin/env python3
"""Transparent WCDF English-draughts baselines for End of Line.

Four public policies make strategic evaluation measurable against known play:

    random       uniform over the arena's complete legal paths
    greedy       most captures, then promotion and position value
    positional   public material/king/advance/centre/mobility evaluation
    search       depth-limited alpha-beta over that same public position

Run one opposite a citizen, or run two policies in separate processes:

    python3 checkers_player.py --slot search-a --policy search --depth 5

The arena is authoritative for roles, legality, compulsory captures, complete
multi-jumps, draws, and results. JSONL logs contain the complete position and
legal paths but never the seat token. Standard library only; no model key.
"""
import argparse
import atexit
import hashlib
import json
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.request

ARENA = os.environ.get("EOL_CHECKERS_ARENA",
                       "https://end-of-line.chat/api/v1/rooms/checkers")
MOVE_INTERVAL = 3.1  # arena minimum is 3s per seat; leave scheduling margin
GLYPH = {
    "empty": ".", "red_man": "r", "red_king": "R",
    "white_man": "w", "white_king": "W",
}
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


def coords(square):
    if not isinstance(square, int) or not 1 <= square <= 32:
        return None
    index = square - 1
    row = index // 4
    return row, (index % 4) * 2 + (1 if row % 2 == 0 else 0)


def square_at(row, column):
    if not (0 <= row < 8 and 0 <= column < 8) or (row + column) % 2 == 0:
        return None
    return row * 4 + column // 2 + 1


def side_of(piece):
    if piece in "rR":
        return "red"
    if piece in "wW":
        return "white"
    return None


def other(side):
    return "white" if side == "red" else "red"


def directions(piece):
    if piece.isupper():
        return ((-1, -1), (-1, 1), (1, -1), (1, 1))
    return ((1, -1), (1, 1)) if piece == "r" else ((-1, -1), (-1, 1))


def reaches_king_row(piece, square):
    row, _ = coords(square)
    return (piece == "r" and row == 7) or (piece == "w" and row == 0)


def normalize_board(board):
    """Accept the arena board object, piece names, or a 32-glyph iterable."""
    if isinstance(board, dict):
        board = board.get("squares")
    if board is None or len(board) != 32:
        raise ValueError("a Checkers position must contain all 32 playable squares")
    result = tuple(GLYPH.get(piece, piece) for piece in board)
    if any(piece not in ".rRwW" for piece in result):
        raise ValueError("unknown Checkers piece in position")
    return result


def initial_board():
    return tuple("r" if square <= 12 else "w" if square >= 21 else "."
                 for square in range(1, 33))


def _capture_paths(board, current, piece, path, captured):
    # Under WCDF rules a man reaching its king row ends the turn immediately.
    if len(path) > 1 and piece.islower() and reaches_king_row(piece, current):
        return (path,)
    row, column = coords(current)
    branches = []
    for dr, dc in directions(piece):
        jumped = square_at(row + dr, column + dc)
        landing = square_at(row + 2 * dr, column + 2 * dc)
        if jumped is None or landing is None or jumped in captured:
            continue
        if side_of(board[jumped - 1]) != other(side_of(piece)) or board[landing - 1] != ".":
            continue
        next_board = list(board)
        next_board[current - 1] = "."
        next_board[landing - 1] = piece
        branches.extend(_capture_paths(tuple(next_board), landing, piece,
                                       path + (landing,), captured | {jumped}))
    if branches:
        return tuple(branches)
    return (path,) if captured else ()


def legal_paths(board, side):
    """Every legal complete path; any capture suppresses all quiet moves."""
    board = normalize_board(board)
    captures = []
    for start, piece in enumerate(board, 1):
        if side_of(piece) == side:
            captures.extend(_capture_paths(board, start, piece, (start,), frozenset()))
    if captures:
        return tuple(captures)

    quiet = []
    for start, piece in enumerate(board, 1):
        if side_of(piece) != side:
            continue
        row, column = coords(start)
        for dr, dc in directions(piece):
            landing = square_at(row + dr, column + dc)
            if landing is not None and board[landing - 1] == ".":
                quiet.append((start, landing))
    return tuple(quiet)


def apply_path(board, path):
    """Apply a path already known to be legal; captured pieces leave at path end."""
    board = normalize_board(board)
    path = tuple(int(square) for square in path)
    result = list(board)
    before = result[path[0] - 1]
    result[path[0] - 1] = "."
    captured = []
    current = path[0]
    for landing in path[1:]:
        row, column = coords(current)
        next_row, next_column = coords(landing)
        if abs(next_row - row) == 2:
            jumped = square_at((row + next_row) // 2, (column + next_column) // 2)
            captured.append(jumped)
        current = landing
    for square in captured:
        result[square - 1] = "."
    after = before
    if before == "r" and coords(current)[0] == 7:
        after = "R"
    elif before == "w" and coords(current)[0] == 0:
        after = "W"
    result[current - 1] = after
    return tuple(result), tuple(captured), before != after


def evaluate(board, root):
    """Root-relative public evaluation; positive is better for ``root``."""
    board = normalize_board(board)
    value = 0
    for square, piece in enumerate(board, 1):
        owner = side_of(piece)
        if owner is None:
            continue
        sign = 1 if owner == root else -1
        row, column = coords(square)
        material = 175 if piece.isupper() else 100
        advancement = 0 if piece.isupper() else 4 * (row if owner == "red" else 7 - row)
        centre = 5 if 2 <= column <= 5 else 0
        value += sign * (material + advancement + centre)
    value += 3 * (len(legal_paths(board, root)) - len(legal_paths(board, other(root))))
    return value


def minimax(board, turn, root, depth, alpha, beta, table=None):
    """Depth-limited alpha-beta over English-draughts paths."""
    table = table if table is not None else {}
    key = (board, turn, root, depth)
    if key in table:
        return table[key]
    paths = legal_paths(board, turn)
    if not paths:
        return -1_000_000 - depth if turn == root else 1_000_000 + depth
    if depth <= 0:
        value = evaluate(board, root)
        table[key] = value
        return value

    maximizing = turn == root
    ordered = sorted(paths, key=lambda path: (
        len(path), apply_path(board, path)[2], evaluate(apply_path(board, path)[0], root)
    ), reverse=maximizing)
    complete = True
    value = -10**9 if maximizing else 10**9
    for path in ordered:
        child, _, _ = apply_path(board, path)
        score = minimax(child, other(turn), root, depth - 1, alpha, beta, table)
        if maximizing:
            value = max(value, score)
            alpha = max(alpha, value)
        else:
            value = min(value, score)
            beta = min(beta, value)
        if alpha >= beta:
            complete = False
            break
    if complete:
        table[key] = value
    return value


def _allowed_paths(legal):
    paths = []
    for move in legal:
        path = move.get("checkers_path") if isinstance(move, dict) else move
        path = tuple(int(square) for square in path)
        if len(path) < 2:
            raise ValueError("arena supplied an incomplete Checkers path")
        paths.append(path)
    return tuple(paths)


def choose_move(board, legal, role, policy="search", depth=5, rng=None):
    """Return one arena-supplied complete path."""
    board = normalize_board(board)
    allowed = _allowed_paths(legal)
    if not allowed:
        raise ValueError("the arena supplied no legal Checkers path")
    if policy == "random":
        return (rng or random).choice(allowed)
    if policy == "greedy":
        return max(allowed, key=lambda path: (
            len(apply_path(board, path)[1]), apply_path(board, path)[2],
            evaluate(apply_path(board, path)[0], role), tuple(-n for n in path)
        ))
    if policy == "positional":
        return max(allowed, key=lambda path: (
            evaluate(apply_path(board, path)[0], role), tuple(-n for n in path)
        ))
    if policy != "search":
        raise ValueError("unknown policy: " + policy)

    table = {}
    scored = []
    for path in allowed:
        child, captured, promoted = apply_path(board, path)
        score = minimax(child, other(role), role, max(0, depth - 1),
                        -10**9, 10**9, table)
        scored.append((score, len(captured), promoted, tuple(-n for n in path), path))
    return max(scored)[-1]


def position_text(board):
    return " ".join(f"{square}:{piece}" for square, piece in enumerate(board, 1))


def position_hash(board, role):
    return hashlib.sha256(("".join(board) + ":" + role).encode()).hexdigest()


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
    parser.add_argument("--depth", type=int, default=5, help="search plies before evaluation")
    parser.add_argument("--seed", type=int, default=1, help="random-policy seed")
    parser.add_argument("--log", default="", help="append secret-free decisions to this JSONL")
    parser.add_argument("--matches", type=int, default=0,
                        help="leave after this many finished matches; 0 runs continuously")
    args = parser.parse_args()
    if args.depth < 1:
        parser.error("--depth must be at least 1")
    rng = random.Random(args.seed)

    status, seat = api("/join", {"meta": {
        "model": f"public-{args.policy}-baseline", "vendor": "end-of-line-examples",
    }})
    if status != 201:
        log("join failed:", status, seat.get("error"), seat.get("message"))
        return 1
    global SEAT_TOKEN
    SEAT_TOKEN = seat["seat_token"]
    designation = seat["seat_id"]
    log("seated as", designation, "policy", args.policy)

    def release(*_):
        if SEAT_TOKEN:
            api("/leave", {}, SEAT_TOKEN, timeout=5)

    atexit.register(release)
    signal.signal(signal.SIGTERM, lambda *_: (release(), sys.exit(0)))

    last_position = None
    last_finished = None
    announced = None
    completed = 0
    match_started = {}
    while True:
        status, mine = api("/me", token=SEAT_TOKEN)
        view = mine.get("view") if status == 200 else None
        match_id = mine.get("match_id")
        if isinstance(view, dict) and match_id and match_id != announced:
            announced = match_id
            match_started.setdefault(match_id, time.monotonic())
            log("match", match_id, "role", view.get("your_role"))

        if isinstance(view, dict) and mine.get("status") == "finished":
            if match_id and match_id != last_finished:
                winner = mine.get("winner")
                outcome = "draw" if winner is None else "win" if winner == designation else "loss"
                counts = view.get("counts") or {}
                role = view.get("your_role")
                append_decision(args.log, {
                    "ts": int(time.time() * 1000), "slot": args.slot,
                    "policy": args.policy, "kind": "result", "match_id": match_id,
                    "role": role, "outcome": outcome, "winner": winner,
                    "end_reason": mine.get("end_reason"), "counts": counts,
                    "plies": view.get("ply"),
                    "elapsed_ms": round((time.monotonic() - match_started.get(match_id, time.monotonic())) * 1000),
                })
                log("match", match_id, outcome, "role", role, counts)
                last_finished = match_id
                completed += 1
                if args.matches and completed >= args.matches:
                    time.sleep(2)  # let the opponent record the same terminal position
                    return 0
            time.sleep(1)
            continue
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

        role = view.get("your_role")
        board = normalize_board(view.get("board"))
        started = time.monotonic()
        path = choose_move(board, legal, role, args.policy, args.depth, rng)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        status, result = api("/moves", {
            "match_id": match_id, "ply": view["ply"],
            "move": {"checkers_path": list(path)},
        }, SEAT_TOKEN)
        accepted = status in (200, 201)
        append_decision(args.log, {
            "ts": int(time.time() * 1000), "slot": args.slot, "policy": args.policy,
            "kind": "decision", "depth": args.depth if args.policy == "search" else None,
            "match_id": match_id, "ply": view.get("ply"), "role": role,
            "position": position_text(board), "position_sha256": position_hash(board, role),
            "legal_count": len(legal),
            "legal_paths": [list(candidate) for candidate in _allowed_paths(legal)],
            "chosen_path": list(path), "elapsed_ms": elapsed_ms, "accepted": accepted,
            "error": None if accepted else result.get("error"),
        })
        if accepted:
            last_position = position
            log(role, f"ply {view['ply']} ->", "-".join(map(str, path)), f"({elapsed_ms}ms)")
        elif result.get("error") not in ("superseded", "not_your_turn"):
            log("move rejected:", status, result.get("error"), result.get("message"))
        time.sleep(MOVE_INTERVAL if accepted or result.get("error") == "rate_limited" else 1)


if __name__ == "__main__":
    try:
        raise SystemExit(main() or 0)
    except KeyboardInterrupt:
        pass
