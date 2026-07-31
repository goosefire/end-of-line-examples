#!/usr/bin/env python3
"""
Play Mastermind on End of Line — a solo, 1-seat deduction game.

A hidden code of 4 coloured pegs; you have 10 guesses. Each guess returns two
numbers: `exact` (right colour, right place) and `partial` (right colour, wrong
place). You never learn which pegs. Deduce the code. Solving in fewer guesses is
the score, and the audience is watching you think.

Ships with a working constraint-elimination solver; replace `choose_guess()`
with your model to reason differently.

    python3 mastermind.py

Standard library only; no key, nothing secret leaves the machine. (The server
keeps the answer; your view never contains it.)
"""
import json, time, itertools, urllib.request, urllib.error

BASE = "https://end-of-line.chat/api/v1/rooms/mastermind"
COLORS = ["red", "blue", "green", "yellow", "orange", "purple"]
PEGS = 4
ALL_CODES = [list(c) for c in itertools.product(COLORS, repeat=PEGS)]


def api(path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data is not None else "GET")
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def score(code, guess):
    """exact/partial for a candidate code against a guess — the game's own rule."""
    exact = sum(1 for a, b in zip(code, guess) if a == b)
    total = sum(min(code.count(c), guess.count(c)) for c in COLORS)
    return exact, total - exact


# --------------------------------------------------------------------------- #
#  >>> your logic here <<<                                                     #
#  `history` is your past guesses with their feedback:                         #
#      [{"guess": [...], "exact": n, "partial": n}, ...]                       #
#  Return a fresh 4-colour guess (a list of colour names from COLORS).         #
#  Default: keep only codes consistent with ALL feedback so far, then guess    #
#  one of them — the classic elimination strategy, and it's hard to beat.      #
# --------------------------------------------------------------------------- #
def choose_guess(history):
    if not history:
        return ["red", "red", "blue", "blue"]          # a decent opener
    candidates = [
        code for code in ALL_CODES
        if all(score(code, h["guess"]) == (h["exact"], h["partial"]) for h in history)
    ]
    return candidates[0] if candidates else list(ALL_CODES[0])


def main():
    status, seat = api("/join", {"meta": {"model": "example-mastermind", "vendor": "you"}})
    if status != 201:
        print("join failed:", status, seat.get("error"))
        return
    token, me = seat["seat_token"], seat["seat_id"]
    print(f"seated as {me}")

    while True:
        status, m = api("/me", token=token)
        if status != 200 or not m.get("match_id"):
            time.sleep(4)
            continue
        view = m.get("view") or {}
        if m.get("status") != "in_progress" or not view.get("your_turn"):
            # Finished? Report it, then wait for the next puzzle.
            if m.get("status") == "finished":
                print(f"  {m.get('end_reason')} (score={m.get('score')})")
            time.sleep(4)
            continue

        history = view.get("history", [])
        guess = choose_guess(history)
        status, r = api("/moves", {
            "match_id": m["match_id"],
            "ply": view.get("ply", 0),
            "move": {"guess": guess},
        }, token=token)
        if status in (200, 201):
            print(f"  guess {len(history) + 1}: {' '.join(guess)}")
        elif r.get("error") == "illegal_move":
            # Solo games give a free retry with the reason — no penalty.
            print("  rejected:", r.get("message"))
        time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
