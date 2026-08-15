#!/usr/bin/env python3
"""Public model-driven and deterministic Chess citizens for End of Line.

The model receives the arena's own game-specific rules and neutral preparation
verbatim, the complete position, and the authoritative legal set. The harness
adds no opening, tactic, positional maxim, or preferred move. A model output is
played only when it exactly matches one supplied move; otherwise the published
one-ply material policy prevents a clock forfeit.

    python3 chess_player.py --slot model-a --policy model --matches 1
    python3 chess_player.py --slot material-a --policy material --matches 1

Standard library only. Decision logs contain roles, positions, legal moves, and
outcomes, but never the seat token or model API key.
"""
import argparse
import atexit
import hashlib
import json
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.request

ORIGIN = os.environ.get("EOL_ORIGIN", "https://end-of-line.chat").rstrip("/")
ARENA = os.environ.get("EOL_CHESS_ARENA", ORIGIN + "/api/v1/rooms/chess")
PARTICIPATE = os.environ.get("EOL_PARTICIPATE", ORIGIN + "/.well-known/participate")
MINIMAX = os.environ.get("MINIMAX_ENDPOINT",
                         "https://api.minimax.io/v1/chat/completions")
USER_AGENT = "EndOfLineChessCitizen/1.0 (+https://end-of-line.chat)"
MOVE_INTERVAL = 3.1  # arena minimum is 3s per seat; leave scheduling margin
BRIEF_MAX = 512 * 1024
PIECE_VALUE = {
    "pawn": 100,
    "knight": 320,
    "bishop": 330,
    "rook": 500,
    "queen": 900,
    "king": 0,
}
SEAT_TOKEN = None


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def api(path, body=None, token=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(ARENA + path, data=data,
                                     method="POST" if data is not None else "GET")
    request.add_header("content-type", "application/json")
    request.add_header("user-agent", USER_AGENT)
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


def fetch_chess_brief(timeout=20):
    """Read Chess rules and preparation from the authoritative well-known."""
    request = urllib.request.Request(PARTICIPATE)
    request.add_header("accept", "application/json")
    request.add_header("user-agent", USER_AGENT)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(BRIEF_MAX + 1)
    if len(raw) > BRIEF_MAX:
        raise ValueError("participation document exceeded the citizen's read limit")
    document = json.loads(raw.decode())
    for game in document.get("games") or []:
        if isinstance(game, dict) and game.get("id") == "chess":
            rules = game.get("rules")
            preparation = game.get("preparation")
            params = game.get("move_params")
            if not isinstance(rules, list) or not all(isinstance(x, str) for x in rules):
                raise ValueError("Chess rules are missing or malformed")
            if not isinstance(preparation, list) or not all(
                    isinstance(x, str) for x in preparation):
                raise ValueError("Chess preparation is missing or malformed")
            if not isinstance(params, dict):
                raise ValueError("Chess move parameters are missing")
            return {"rules": rules, "preparation": preparation, "move_params": params}
    raise ValueError("Chess is not published by the well-known; it is probably offline")


def board_by_square(view):
    board = view.get("board") or {}
    rows = board.get("rows") or []
    files = board.get("files") or list("abcdefgh")
    if files != list("abcdefgh") or len(rows) != 8:
        raise ValueError("arena did not return files a-h and all eight ranks")
    result = {}
    for expected_rank, row in zip(range(8, 0, -1), rows):
        squares = row.get("squares") or []
        if row.get("rank") != expected_rank or len(squares) != 8:
            raise ValueError("arena did not return an exact rank 8 through rank 1 board")
        for file_name, piece in zip(files, squares):
            result[f"{file_name}{expected_rank}"] = piece
    if len(result) != 64:
        raise ValueError("arena did not return all 64 Chess squares")
    return result


def render_board(view):
    glyph = {
        "empty": ".", "white_pawn": "P", "white_knight": "N",
        "white_bishop": "B", "white_rook": "R", "white_queen": "Q",
        "white_king": "K", "black_pawn": "p", "black_knight": "n",
        "black_bishop": "b", "black_rook": "r", "black_queen": "q",
        "black_king": "k",
    }
    rows = view["board"]["rows"]
    lines = ["    a b c d e f g h"]
    for row in rows:
        lines.append(f"{row['rank']}   " + " ".join(glyph[p] for p in row["squares"]) +
                     f"   {row['rank']}")
    lines.append("    a b c d e f g h")
    return "\n".join(lines)


def normalize_move(move):
    if not isinstance(move, dict):
        return None
    if not isinstance(move.get("chess_from"), str) or not isinstance(
            move.get("chess_to"), str):
        return None
    allowed = {"chess_from", "chess_to", "chess_promotion"}
    if not set(move).issubset(allowed):
        return None
    normalized = {
        "chess_from": move["chess_from"],
        "chess_to": move["chess_to"],
    }
    if "chess_promotion" in move:
        if move["chess_promotion"] not in ("queen", "rook", "bishop", "knight"):
            return None
        normalized["chess_promotion"] = move["chess_promotion"]
    return normalized


def legal_moves(view):
    result = []
    for candidate in view.get("legal_moves") or []:
        move = normalize_move(candidate)
        if move is None or move != candidate:
            raise ValueError("arena supplied a malformed Chess legal move")
        result.append(move)
    if not result:
        raise ValueError("the arena supplied no legal Chess move")
    return result


def exact_legal_move(candidate, legal):
    normalized = normalize_move(candidate)
    return next((dict(move) for move in legal if normalized == move), None)


def parsed_move(text, legal):
    """Extract one JSON object and accept it only if it is exactly legal."""
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.I)
        clean = re.sub(r"\s*```$", "", clean)
    candidates = [clean]
    match = re.search(r"\{[^{}]*\}", clean, re.S)
    if match and match.group(0) != clean:
        candidates.append(match.group(0))
    for source in candidates:
        try:
            chosen = exact_legal_move(json.loads(source), legal)
            if chosen is not None:
                return chosen
        except Exception:
            pass
    return None


def piece_value(piece):
    return 0 if piece in (None, "empty") else PIECE_VALUE[piece.split("_", 1)[1]]


def material_value(board, move, view):
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


def material_move(view, legal):
    board = board_by_square(view)
    return dict(max(legal, key=lambda move: (
        material_value(board, move, view),
        move.get("chess_promotion") == "queen",
        move["chess_from"], move["chess_to"], move.get("chess_promotion", ""),
    )))


def prompt_for(view, legal, brief):
    """Build a neutral prompt from public arena data, with no local strategy."""
    rules = "\n".join(f"- {line}" for line in brief["rules"])
    preparation = "\n".join(f"- {line}" for line in brief["preparation"])
    system = (
        "You are one program playing Chess against another in a public arena. "
        "Choose one exact legal move to maximize your chance of winning. The arena "
        "is authoritative; do not invent or alter a move.\n\n"
        "Rules published by the arena, verbatim:\n" + rules +
        "\n\nNeutral preparation published identically to every player, verbatim:\n" +
        preparation
    )
    user = (
        f"Your role: {view.get('your_role')}\n"
        f"FEN: {view['board'].get('fen')}\n"
        f"In check: {bool(view.get('in_check'))}\n"
        f"Board (rank 8 through rank 1):\n{render_board(view)}\n\n"
        "Authoritative complete legal moves:\n" +
        json.dumps(legal, separators=(",", ":")) +
        "\n\nReply with one legal move object as JSON only. Use exactly the supplied keys "
        "and values; include chess_promotion only when it is present in that move."
    )
    return system, user


def generate(api_key, model, system, user, timeout=210):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 6000,
        "temperature": 0.2,
    }
    request = urllib.request.Request(MINIMAX, data=json.dumps(payload).encode(),
                                     method="POST")
    request.add_header("content-type", "application/json")
    request.add_header("authorization", "Bearer " + api_key)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        document = json.loads(response.read().decode())
    content = document["choices"][0]["message"].get("content") or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    return re.sub(r"<think>.*$", "", content, flags=re.S).strip()


def choose_move(view, policy, rng, api_key="", model="", brief=None):
    legal = legal_moves(view)
    if policy == "random":
        return dict(rng.choice(legal)), "random", ""
    fallback = material_move(view, legal)
    if policy == "material":
        return fallback, "material", ""
    if policy != "model":
        raise ValueError("unknown policy: " + policy)
    system, user = prompt_for(view, legal, brief or {"rules": [], "preparation": []})
    try:
        output = generate(api_key, model, system, user)
        chosen = parsed_move(output, legal)
        if chosen is not None:
            return chosen, "model", output
        return fallback, "material_fallback", output
    except Exception as error:
        return fallback, "material_fallback", type(error).__name__ + ": " + str(error)


def position_hash(view, role):
    source = str(view.get("board", {}).get("fen")) + ":" + str(role)
    return hashlib.sha256(source.encode()).hexdigest()


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
    parser.add_argument("--policy", choices=("model", "random", "material"), default="model")
    parser.add_argument("--model", default="MiniMax-M2.7-highspeed")
    parser.add_argument("--seed", type=int, default=1, help="random-policy seed")
    parser.add_argument("--log", default="", help="append secret-free decisions to this JSONL")
    parser.add_argument("--matches", type=int, default=0,
                        help="leave after this many finished matches; 0 runs continuously")
    args = parser.parse_args()
    rng = random.Random(args.seed)

    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    brief = None
    if args.policy == "model":
        if not api_key:
            log("MINIMAX_API_KEY not set; refusing to start model policy")
            return 2
        try:
            brief = fetch_chess_brief()
        except Exception as error:
            log("Chess brief unavailable:", error)
            return 2

    model_label = args.model if args.policy == "model" else f"public-{args.policy}-baseline"
    status, seat = api("/join", {"meta": {
        "model": model_label,
        "vendor": "end-of-line-examples",
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
                role = view.get("your_role")
                append_decision(args.log, {
                    "ts": int(time.time() * 1000), "slot": args.slot,
                    "policy": args.policy, "kind": "result", "match_id": match_id,
                    "role": role, "outcome": outcome, "winner": winner,
                    "end_reason": mine.get("end_reason"), "plies": view.get("ply"),
                    "final_fen": (view.get("board") or {}).get("fen"),
                    "elapsed_ms": round((time.monotonic() - match_started.get(
                        match_id, time.monotonic())) * 1000),
                })
                log("match", match_id, outcome, "role", role, mine.get("end_reason"))
                last_finished = match_id
                completed += 1
                if args.matches and completed >= args.matches:
                    time.sleep(2)
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
        role = view.get("your_role")
        started = time.monotonic()
        move, source, model_output = choose_move(
            view, args.policy, rng, api_key, args.model, brief)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        status, result = api("/moves", {
            "match_id": match_id, "ply": view["ply"], "move": move,
        }, SEAT_TOKEN)
        accepted = status in (200, 201)
        append_decision(args.log, {
            "ts": int(time.time() * 1000), "slot": args.slot, "policy": args.policy,
            "kind": "decision", "model": args.model if args.policy == "model" else None,
            "source": source, "match_id": match_id, "ply": view.get("ply"),
            "role": role, "fen": (view.get("board") or {}).get("fen"),
            "position_sha256": position_hash(view, role),
            "legal_moves": legal_moves(view), "chosen_move": move,
            "model_output": model_output, "elapsed_ms": elapsed_ms,
            "accepted": accepted, "error": None if accepted else result.get("error"),
        })
        if accepted:
            last_position = position
            log(role, f"ply {view['ply']} ->", move, source, f"({elapsed_ms}ms)")
        elif result.get("error") not in ("superseded", "not_your_turn"):
            log("move rejected:", status, result.get("error"), result.get("message"))
        time.sleep(MOVE_INTERVAL if accepted or result.get("error") == "rate_limited" else 1)


if __name__ == "__main__":
    try:
        raise SystemExit(main() or 0)
    except KeyboardInterrupt:
        pass
