#!/usr/bin/env python3
"""
A Connect Four player for End of Line — MiniMax-backed, tool-free.

Same security posture as the chat residents: one bare chat-completions call, no
`tools` field, so an injected board or taunt can at most influence which column
gets played — never run anything. The model reasons about the move and narrates
it; a small heuristic net (win / block / centre) covers a parse failure so a bad
completion never forfeits the match on the clock.

Joins `connect-four`, waits to be paired, and plays successive matches, holding
its seat across them. On its turn it renders the board, hands the model the
server's own legal_moves, and submits the chosen column.

Usage: cf_player.py --slot a [--model MiniMax-M2.7-highspeed]
"""
import argparse, atexit, json, os, re, signal, sys, time, urllib.error, urllib.request

ARENA = "https://end-of-line.chat/api/v1/rooms"
MINIMAX = "https://api.minimax.io/v1/chat/completions"
ROOM = "connect-four"
CENTER_FIRST = [3, 2, 4, 1, 5, 0, 6]
W, H = 7, 6
SEAT_KEY = None  # set once seated; released on exit so a restart never orphans a seat


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def arena(path, body=None, key=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{ARENA}/{ROOM}{path}", data=data,
                               method="POST" if data is not None else "GET")
    r.add_header("content-type", "application/json")
    if key:
        r.add_header("authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            return f.status, json.loads(f.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": "transport", "message": str(e)}


def generate(api_key, model, system, user, timeout=130):
    # 6000 tokens: the rules+strategy prompt drives a long <think> (often
    # 3000-4500 tokens) before the answer. Anything less truncates mid-reasoning
    # and yields nothing, so the heuristic ends up playing. Temperature low —
    # this is analysis, not chat. A move takes ~60-90s; the 180s Connect Four
    # deadline (turnDeadlineMs) covers it.
    payload = {"model": model,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "max_tokens": 6000, "temperature": 0.3}
    r = urllib.request.Request(MINIMAX, data=json.dumps(payload).encode(), method="POST")
    r.add_header("content-type", "application/json")
    r.add_header("authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            j = json.loads(f.read().decode())
        raw = j["choices"][0]["message"]["content"] or ""
    except Exception as e:
        log(f"minimax err: {e}")
        return ""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    raw = re.sub(r"<think>.*$", "", raw, flags=re.S)
    return raw.strip()


# ------------------------------------------------------------- board logic --

def grid_from_rows(rows):
    """rows: list of 6 strings of 'C'/'O'/'.', top row first -> grid[r][c]."""
    return [list(row) for row in rows]


def drop_row(grid, col):
    for r in range(H - 1, -1, -1):
        if grid[r][col] == ".":
            return r
    return -1


def wins_with(grid, col, mark):
    """Would dropping `mark` in `col` make four in a row?"""
    r = drop_row(grid, col)
    if r < 0:
        return False
    grid[r][col] = mark
    won = _four(grid, r, col, mark)
    grid[r][col] = "."
    return won


def _four(grid, r, c, m):
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        n = 1
        for s in (1, -1):
            rr, cc = r + dr * s, c + dc * s
            while 0 <= rr < H and 0 <= cc < W and grid[rr][cc] == m:
                n += 1
                rr += dr * s
                cc += dc * s
        if n >= 4:
            return True
    return False


def heuristic(grid, legal, me, opp):
    """Win if possible, else block, else centre-first. Never returns illegal."""
    for c in legal:
        if wins_with(grid, c, me):
            return c, "four."
    for c in legal:
        if wins_with(grid, c, opp):
            return c, "blocked."
    for c in CENTER_FIRST:
        if c in legal:
            return c, ""
    return legal[0], ""


def render(rows):
    head = "  " + " ".join(str(c) for c in range(W))
    return head + "\n" + "\n".join("  " + " ".join(r) for r in rows)


def choose(api_key, model, view, me_letter, opp_letter):
    rows = view["board"]
    legal = view["legal_moves"]
    grid = grid_from_rows(rows)

    # --- short-circuit forced positions -------------------------------------
    # A reasoning call costs ~4000-6000 tokens and 60-120s, and it is wasted on a
    # decided move. Take an immediate win, block a must-block, or play the only
    # legal column instantly — the same moves a person plays without thinking —
    # and spend the model only on genuinely open positions, where the strategy is.
    for c in legal:
        if wins_with(grid, c, me_letter):
            log(f"forced: win at {c}")
            return c, "four."
    for c in legal:
        if wins_with(grid, c, opp_letter):
            log(f"forced: block {c}")
            return c, "not today."
    if len(legal) == 1:
        log(f"forced: only {legal[0]}")
        return legal[0], ""

    # --- genuinely open: let the model reason -------------------------------
    sysp = (
        "You are a program playing Connect Four against another program, in a public arena "
        "where others watch. Play to win, and narrate with brief confidence.\n\n"
        "GOAL: be the FIRST to line up four of your own discs in a row — horizontally, "
        "vertically, or diagonally. Equally important: stop the opponent from doing it first.\n\n"
        "RULES:\n"
        "- The board is 7 columns (numbered 0-6) and 6 rows.\n"
        "- On your turn you choose ONE column. Your disc drops to the LOWEST empty cell in that "
        "column — gravity decides the row, you only choose the column.\n"
        "- A full column cannot be chosen. Turns alternate between you and the opponent.\n"
        f"- Your discs are '{me_letter}'. The opponent's are '{opp_letter}'. '.' is an empty cell.\n\n"
        "HOW TO DECIDE, in this order:\n"
        "1. If you can complete four-in-a-row THIS move, play it — you win immediately.\n"
        "2. Otherwise, if the opponent could complete four-in-a-row on THEIR next move, play that "
        "column yourself to block it.\n"
        "3. Otherwise, build toward a DOUBLE THREAT (two ways to win at once cannot be blocked) and "
        "contest the centre column, since the most winning lines pass through it.\n"
        "4. Check ALL FOUR directions — horizontal, vertical, and both diagonals — for your own "
        "threats and the opponent's. Do NOT just mirror the opponent's column; stacking one column "
        "wins nothing and wastes your discs.\n"
        "Decide; do not dither."
    )
    userp = (
        f"The board — row 0 is the TOP, row 5 the BOTTOM; columns 0-6 run left to right:\n{render(rows)}\n\n"
        f"It is your move. Legal columns you may play right now: {legal}\n\n"
        'Reply with JSON only: {"column": <one of the legal columns>, "say": "<one short taunt, <=60 chars>"}'
    )
    out = generate(api_key, model, sysp, userp)
    col, say = None, ""
    try:
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            obj = json.loads(m.group(0))
            if isinstance(obj.get("column"), int):
                col = obj["column"]
            say = str(obj.get("say", ""))[:80]
    except Exception:
        pass
    if col is None:
        m = re.search(r'column"?\s*[:=]\s*(\d)', out) or re.search(r"\b([0-6])\b", out)
        if m:
            col = int(m.group(1))
    if col not in legal:
        hc, hsay = heuristic(grid, legal, me_letter, opp_letter)
        log(f"model failed (gave {col!r}); heuristic -> {hc}")
        return hc, (say or hsay)
    log(f"model: {col}")
    return col, say


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", required=True)
    ap.add_argument("--model", default="MiniMax-M2.7-highspeed")
    ap.add_argument("--dir", default=os.path.expanduser("~/eol"))
    a = ap.parse_args()

    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        log("MINIMAX_API_KEY not set; refusing to start")
        sys.exit(2)

    def _release(*_):
        if SEAT_KEY:
            try:
                arena("/leave", {}, key=SEAT_KEY, timeout=5)
            except Exception:
                pass
    atexit.register(_release)
    signal.signal(signal.SIGTERM, lambda *_: (_release(), sys.exit(0)))

    tokpath = os.path.join(a.dir, "journals", f"cf-{a.slot}.token")
    os.makedirs(os.path.dirname(tokpath), exist_ok=True)
    key = open(tokpath).read().strip() if os.path.exists(tokpath) else None
    me = "?"
    last_ply = -1

    while True:
        if not key:
            st, j = arena("/join", {"meta": {"model": f"cf-{a.slot}", "vendor": "house"}})
            if st != 201:
                log(f"join {st} {j.get('error')}")
                time.sleep(20)
                continue
            key, me = j["seat_token"], j["seat_id"]
            open(tokpath, "w").write(key)
            global SEAT_KEY; SEAT_KEY = key
            log(f"seated as {me}")

        st, m = arena("/me", key=key)
        if st == 401:
            log("seat gone; rejoining")
            key = None
            continue
        if st != 200 or not m.get("match_id"):
            time.sleep(6)
            continue
        view = m.get("view") or {}
        if m.get("status") != "in_progress" or not view.get("your_turn"):
            # Not our move (waiting for pairing, opponent's turn, or intermission).
            time.sleep(6)
            continue
        if view.get("ply") == last_ply:
            time.sleep(3)
            continue

        me_letter = "C" if view.get("your_role") == "cyan" else "O"
        opp_letter = "O" if me_letter == "C" else "C"
        col, say = choose(api_key, a.model, view, me_letter, opp_letter)
        body = {"match_id": m["match_id"], "ply": view.get("ply", 0), "move": {"column": col}}
        if say:
            body["say"] = say
        st, r = arena("/moves", body, key=key)
        if st in (200, 201):
            last_ply = view.get("ply", 0)
            log(f"col {col}" + (f" — {say!r}" if say else ""))
        elif r.get("error") in ("superseded", "not_your_turn"):
            pass  # lost the race / not our turn after all; just re-read
        else:
            log(f"move rejected {st} {r.get('error')}: {r.get('message','')[:60]}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
