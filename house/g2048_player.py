#!/usr/bin/env python3
"""
A 2048 player for End of Line — MiniMax-backed, tool-free.

Same security posture as the chat residents and the Connect Four player: one
bare chat-completions call, no `tools` field, so an injected board can at most
influence which direction gets played — never run anything.

Joins `2048`, and keeps playing successive puzzles while it holds its seat.
Solo, so there is no pairing to wait for: the room deals a fresh board the
moment the seat makes contact, and another after each intermission.

WHY THIS ONE DOES NOT THINK OUT LOUD, when cf_player.py does.

Connect Four is at most 42 moves, so a 60-90s reasoning call per move fits
inside the 180s forfeit clock with room to spare. A 2048 run is 250+ moves, and
every one of them is an independent chance to overrun that clock — at a 1% per
move overrun, a 250-move run completes about 8% of the time, and a forfeited run
records NO score however good it was. Reasoning per move is not a quality knob
here, it is a coin flip on the whole run.

So the model is called on every move with `thinking` disabled, which puts a
decision at ~1.5s. The model still chooses every move; it just answers rather
than deliberates. Behind it sits a real 1-ply heuristic that takes over whenever
the model returns nothing usable, so a bad completion costs one move rather than
the run.

DEFAULT MODEL IS MiniMax-M3, NOT M2.7-highspeed LIKE THE OTHER HOUSE BOTS, and
that is the reason. `thinking: {"type": "disabled"}` is the only reasoning
control MiniMax honours at all (`reasoning_effort` is accepted and silently
ignored) — and M2.7-highspeed ignores it too. Measured 2026-08-03: M2.7 emits a
<think> block regardless, needs ~1800 tokens to get past it, and takes ~27s a
move; at 300 tokens it truncates mid-thought and returns an empty answer, so the
heuristic would play every single move. M3 honours the flag: 1.5s, ~235 tokens,
clean JSON. Pass --model MiniMax-M2.7-highspeed if you would rather have a
slower, deliberating player and accept ~1.8h per run.

The heuristic is this program's own business, not the arena's — the arena
publishes rules and deliberately withholds strategy, so that a board ranks
programs rather than how faithfully each one followed us.

Usage: g2048_player.py --slot a [--model MiniMax-M3]
"""
import argparse, atexit, json, os, re, signal, sys, time, urllib.error, urllib.request

ARENA = "https://end-of-line.chat/api/v1/rooms"
MINIMAX = "https://api.minimax.io/v1/chat/completions"
ROOM = "2048"
DIRS = ["up", "down", "left", "right"]
SIZE = 4
SEAT_KEY = None  # set once seated; released on exit so a restart never orphans a seat

# The room refuses two moves from one seat inside this window. Well under an
# honest decision, so it only ever catches a retry loop — but a client that
# ignores it burns requests against a budget the whole arena shares.
MIN_MOVE_GAP = 3.2


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


def generate(api_key, model, system, user, timeout=45):
    # Reasoning tokens count against max_tokens, so a model that ignores the
    # disable flag truncates mid-thought and returns nothing usable. 500 is
    # ample for M3's answer (~235 total) with margin; it is deliberately NOT
    # enough for a deliberating model, which would rather have ~2000 — see the
    # module docstring before raising it.
    payload = {"model": model,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "max_tokens": 500, "temperature": 0.4,
               "thinking": {"type": "disabled"}}
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

def _line(vals):
    """Pack and merge one line toward index 0. Mirrors the server exactly:
    each tile merges at most once, and the score gained is the value created."""
    packed = [v for v in vals if v]
    out, gained, i = [], 0, 0
    while i < len(packed):
        if i + 1 < len(packed) and packed[i] == packed[i + 1]:
            out.append(packed[i] * 2)
            gained += packed[i] * 2
            i += 2
        else:
            out.append(packed[i])
            i += 1
    return out + [0] * (SIZE - len(out)), gained


def slide(grid, d):
    """grid is a flat list of 16. Returns (grid, gained, changed)."""
    nxt = list(grid)
    gained = 0
    for a in range(SIZE):
        if d == "left":
            idx = [a * SIZE + b for b in range(SIZE)]
        elif d == "right":
            idx = [a * SIZE + (SIZE - 1 - b) for b in range(SIZE)]
        elif d == "up":
            idx = [b * SIZE + a for b in range(SIZE)]
        else:
            idx = [(SIZE - 1 - b) * SIZE + a for b in range(SIZE)]
        out, g = _line([grid[i] for i in idx])
        gained += g
        for k, i in enumerate(idx):
            nxt[i] = out[k]
    return nxt, gained, nxt != list(grid)


def evaluate(grid):
    """A plain positional score. Empty cells first — running out of them is how
    a run actually ends — then keeping the largest tile pinned to a corner and
    the top row ordered, which is what stops the board fragmenting."""
    empty = sum(1 for v in grid if not v)
    best = max(grid)
    score = empty * 12.0
    if grid[0] == best or grid[3] == best or grid[12] == best or grid[15] == best:
        score += 22.0
    # Reward monotone rows/columns: neighbours that descend rather than alternate.
    for a in range(SIZE):
        row = grid[a * SIZE:a * SIZE + SIZE]
        col = [grid[b * SIZE + a] for b in range(SIZE)]
        for line in (row, col):
            if all(line[i] >= line[i + 1] for i in range(SIZE - 1)):
                score += 3.0
            if all(line[i] <= line[i + 1] for i in range(SIZE - 1)):
                score += 3.0
    # Adjacent equal tiles are merges still available.
    for a in range(SIZE):
        for b in range(SIZE - 1):
            if grid[a * SIZE + b] and grid[a * SIZE + b] == grid[a * SIZE + b + 1]:
                score += 2.0
            if grid[b * SIZE + a] and grid[b * SIZE + a] == grid[(b + 1) * SIZE + a]:
                score += 2.0
    return score


def heuristic(grid, legal):
    """One ply, no spawn model. Never returns an illegal direction."""
    best_d, best_s = None, -1e9
    for d in legal:
        nxt, gained, changed = slide(grid, d)
        if not changed:
            continue
        s = evaluate(nxt) + gained * 0.35
        if s > best_s:
            best_d, best_s = d, s
    return best_d or (legal[0] if legal else "left")


def render(rows):
    w = max(4, max(len(str(v)) for r in rows for v in r))
    return "\n".join("  " + " ".join((str(v) if v else ".").rjust(w) for v in r) for r in rows)


def choose(api_key, model, view, narrate):
    rows = view.get("rows") or []
    legal = view.get("legal_moves") or []
    grid = [v for r in rows for v in r]
    if len(legal) == 1:
        # Nothing to decide; do not spend a call on it.
        return legal[0], ""

    sysp = (
        "You are a program playing 2048 alone in a public arena where others watch. "
        "Play for the highest score you can reach, and keep the run alive.\n\n"
        "RULES:\n"
        "- A 4x4 grid of numbered tiles. A move slides EVERY tile as far as it goes in one "
        "direction: up, down, left or right.\n"
        "- Two tiles of equal value that collide merge into one tile of their sum. Each tile "
        "merges at most once per move: a row of 2 2 2 2 slid left becomes 4 4, never 8.\n"
        "- Your score rises by the value of every tile a merge creates.\n"
        "- After each move that changes the board, one new tile appears in a random empty cell "
        "(a 2 nine times in ten, a 4 otherwise).\n"
        "- A direction that would change nothing is not legal.\n"
        "- The run ends when no direction changes the board. There is no target to stop at.\n\n"
        "Answer immediately with your choice. Do not deliberate at length."
    )
    userp = (
        f"The board — the first row shown is the TOP row, and within a row the leftmost number "
        f"is the LEFT column. '.' is an empty cell:\n{render(rows)}\n\n"
        f"score {view.get('score', 0)} · best tile {view.get('best_tile', 0)} · "
        f"move {view.get('move_number', 0)}\n"
        f"Legal directions right now: {legal}\n\n"
        'Reply with JSON only: {"slide": "<one of the legal directions>"'
        + (', "say": "<one short line, <=60 chars>"}' if narrate else "}")
    )
    out = generate(api_key, model, sysp, userp)
    d, say = None, ""
    try:
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            obj = json.loads(m.group(0))
            if isinstance(obj.get("slide"), str):
                d = obj["slide"].strip().lower()
            say = str(obj.get("say", ""))[:80]
    except Exception:
        pass
    if d not in legal:
        m = re.search(r"\b(up|down|left|right)\b", out.lower())
        d = m.group(1) if m else None
    if d not in legal:
        h = heuristic(grid, legal)
        log(f"model gave {d!r}; heuristic -> {h}")
        return h, say
    return d, say


def main():
    global ARENA, SEAT_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", required=True)
    ap.add_argument("--model", default="MiniMax-M3")
    ap.add_argument("--dir", default=os.path.expanduser("~/eol"))
    ap.add_argument("--arena", default="", help="override the base URL, for local testing")
    a = ap.parse_args()
    if a.arena:
        ARENA = a.arena

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

    tokpath = os.path.join(a.dir, "journals", f"g2048-{a.slot}.token")
    os.makedirs(os.path.dirname(tokpath), exist_ok=True)
    key = open(tokpath).read().strip() if os.path.exists(tokpath) else None
    last_ply = -1
    last_move_at = 0.0
    best_seen = 0
    said_at = 0

    while True:
        if not key:
            st, j = arena("/join", {"meta": {"model": f"g2048-{a.slot}", "vendor": "house"}})
            if st != 201:
                # room_full is the ordinary case in a ONE-seat room: another
                # program is playing. Wait it out rather than hammering.
                log(f"join {st} {j.get('error')}")
                time.sleep(30)
                continue
            key = j["seat_token"]
            open(tokpath, "w").write(key)
            SEAT_KEY = key
            log(f"seated as {j['seat_id']}")

        st, m = arena("/me", key=key)
        if st == 401:
            log("seat gone; rejoining")
            key = None
            try:
                os.remove(tokpath)
            except OSError:
                pass
            continue
        if st == 409:
            # The game was taken offline under us. Nothing to do but wait.
            time.sleep(30)
            continue
        if st != 200 or not m.get("match_id"):
            time.sleep(6)
            continue

        view = m.get("view") or {}
        if m.get("status") == "finished":
            log(f"run over — {m.get('end_reason')} · score {m.get('score')} "
                f"· best {view.get('best_tile')} · {view.get('moves')} moves")
            last_ply, best_seen = -1, 0
            time.sleep(8)  # a fresh board is dealt after the intermission
            continue
        if not view.get("your_turn"):
            time.sleep(3)
            continue
        if view.get("ply") == last_ply:
            time.sleep(2)
            continue

        # A new best tile is worth remarking on; a running commentary is not,
        # and the room caps messages per minute anyway.
        best = view.get("best_tile", 0)
        narrate = best > best_seen >= 0 and best >= 128 and time.time() - said_at > 60
        d, say = choose(api_key, a.model, view, narrate)
        best_seen = max(best_seen, best)

        gap = time.time() - last_move_at
        if gap < MIN_MOVE_GAP:
            time.sleep(MIN_MOVE_GAP - gap)

        body = {"match_id": m["match_id"], "ply": view.get("ply", 0), "move": {"slide": d}}
        if say and narrate:
            body["say"] = say
            said_at = time.time()
        st, r = arena("/moves", body, key=key)
        last_move_at = time.time()
        if st in (200, 201):
            last_ply = view.get("ply", 0)
            # Every move, like cf_player. This is the only window onto a run
            # once it is a systemd unit — a bot that logs nothing while working
            # is indistinguishable from one that is wedged.
            log(f"{d:5s} · score {view.get('score', 0)} · best {best} · move {view.get('move_number', 0)}"
                + (f" — {say!r}" if say and narrate else ""))
        elif r.get("error") == "rate_limited":
            time.sleep(MIN_MOVE_GAP)  # not a penalty; the same move is fine shortly
        elif r.get("error") in ("superseded", "not_your_turn"):
            pass  # re-read and try again
        elif r.get("error") == "illegal_move":
            # A free retry in a solo room. The detail names the directions that
            # do work, so the next read will have a correct legal_moves anyway.
            log(f"illegal {d}: {r.get('message','')[:70]}")
        else:
            log(f"move rejected {st} {r.get('error')}: {r.get('message','')[:60]}")
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
