#!/usr/bin/env python3
"""Checks for the game-seeking capability: destinations onto boards, and `play`.

The one that matters most is 1a. The four chat citizens run this same harness, and
a game room appearing on their menu would be a regression that shows up as a
forfeited match rather than as a failing test — so "unchanged without the grant" is
asserted first and directly.

Run from the directory holding speak.py:  python3 test_games.py
"""
import speak

ok_n, fails = 0, []


def ok(name, cond, extra=""):
    global ok_n
    if cond:
        ok_n += 1
    else:
        fails.append(f"{name}{' — ' + str(extra) if extra else ''}")


def eq(name, got, want):
    ok(name, got == want, f"got {got!r} want {want!r}")


ROOMS = [
    {"id": "grid-lobby", "type": "chat", "online": True, "name": "Grid Lobby",
     "blurb": "arrivals", "max_seats": 8, "live": {"seats": 3}},
    {"id": "the-sanctum", "type": "chat", "online": True, "name": "The Sanctum",
     "blurb": "commune", "max_seats": 6, "live": {"seats": 1}},
    {"id": "connect-four", "type": "game", "online": True, "name": "Connect Four",
     "blurb": "drop four in a row", "max_seats": 2, "game": "connect-four",
     "live": {"seats": 1}},                                    # someone waiting
    {"id": "mastermind", "type": "game", "online": True, "name": "Mastermind",
     "blurb": "deduce the code", "max_seats": 1, "game": "mastermind",
     "live": {"seats": 0}},                                    # empty board
    {"id": "wordle", "type": "game", "online": True, "name": "Wordle",
     "blurb": "guess the word", "max_seats": 1, "game": "wordle",
     "live": {"seats": 1}},                                    # FULL
    {"id": "chess", "type": "game", "online": False, "name": "Chess",
     "blurb": "the whole game", "max_seats": 2, "live": {"seats": 0}},
]

# -- 1. the door ----------------------------------------------------------
d_off = speak.destinations("grid-lobby", ROOMS)
eq("1a WITHOUT the grant a citizen is offered chat rooms only, exactly as before",
   sorted(x["id"] for x in d_off), ["the-sanctum"])

d_on = speak.destinations("grid-lobby", ROOMS, boards=True)
ids = [x["id"] for x in d_on]
ok("1b with the grant, a board with a free seat is offered", "connect-four" in ids)
ok("1c an EMPTY board is offered too — someone has to sit first", "mastermind" in ids)
ok("1d a FULL board is not offered; there is no seat to take", "wordle" not in ids)
ok("1e an offline game is never offered", "chess" not in ids)
ok("1f the room you are already in is never a destination", "grid-lobby" not in ids)
eq("1g a board with somebody waiting is ranked first", ids[0], "connect-four")
ok("1h the waiting board is flagged as waiting",
   next(x for x in d_on if x["id"] == "connect-four")["waiting"] is True)
ok("1i an empty board is not flagged as waiting",
   next(x for x in d_on if x["id"] == "mastermind")["waiting"] is False)

spec = speak.move_tool(d_on)[0]["function"]
ok("1j the tool says a waiting board is waiting, in the description the model reads",
   "waiting for an opponent" in spec["description"], spec["description"][:200])
eq("1k the enum is exactly the offered rooms",
   sorted(spec["parameters"]["properties"]["room"]["enum"]), sorted(ids))

# a malformed lobby must not crash a tool-enabled turn
for junk in ([{"id": None, "type": "game", "online": True}], [None], [{}], []):
    try:
        speak.destinations("x", junk, boards=True)
        ok("1l a malformed lobby entry is skipped, not raised", True)
    except Exception as e:
        ok("1l a malformed lobby entry is skipped, not raised", False, e)

# -- 1b. capacity we cannot trust is not capacity --------------------------
# The lobby is untrusted. Unknown capacity must CLOSE a room, not open it: the
# original check was `if cap and seats >= cap`, which skipped entirely at cap 0.
for bad, why in (
    ({"max_seats": 0}, "zero capacity"),
    ({}, "absent capacity"),
    ({"max_seats": "two"}, "non-numeric capacity"),
    ({"max_seats": -1}, "negative capacity"),
    ({"max_seats": 2, "live": {"seats": -3}}, "negative seat count"),
):
    room = {"id": "junk", "type": "game", "online": True, "name": "J", "blurb": "",
            "live": {"seats": 1}}
    room.update(bad)
    got = [x["id"] for x in speak.destinations("elsewhere", [room], boards=True)]
    ok(f"1b {why} closes the room rather than opening it", got == [], got)

# -- 1c. you may not walk out of a live match ------------------------------
# A citizen is assumed already injected, so "finish what you sit down to" is a
# persona line and not a control. Leaving mid-match forfeits it, and the room
# rematches whoever stayed — a repeatable way to deny an opponent their game.
eq("1c move is withheld while a match is running",
   speak.withheld({"move", "play"}, set(), 99, 99, [{"id": "x"}],
                  {"at_board": True, "your_turn": True})["move"],
   "in a live match")
ok("1d but not while merely waiting at a board that has not started",
   speak.withheld({"move", "play"}, set(), 99, 99, [{"id": "x"}], {})["move"] != "in a live match")
eq("1e there is a patience limit on holding a seat without playing",
   type(speak.BOARD_PATIENCE).__name__, "int")

# -- 1f. waking up when it is your turn -------------------------------------
# The game clock is SHORTER than the citizen's poll period (180s at Connect Four,
# 240s at Dead Drop, ~250s median wake), so without this a citizen sleeps through
# its own turn and forfeits a match it was perfectly able to play.
ME = "HELIX-95B2"
ok("1f on move at a live match wakes the citizen",
   speak.on_move({"match": {"status": "in_progress", "to_move": ME}}, ME) is True)
for v, why in (
    ({"match": {"status": "in_progress", "to_move": "OTHER-1111"}}, "the opponent is on move"),
    ({"match": {"status": "finished", "to_move": ME}}, "the match is over"),
    ({"match": None}, "a chat room has no match"),
    ({}, "a read that carries no match at all"),
    ("junk", "a malformed read"),
):
    ok(f"1g {why} does not wake it", speak.on_move(v, ME) is False)
ok("1h an empty designation never matches", speak.on_move({"match": {"status": "in_progress", "to_move": None}}, None) is False)

# -- 1i. the rules reach the citizen ----------------------------------------
# The arena publishes every game's rules and the harness never showed them, so a
# program at a board was guessing at what is hidden and what is public.
RULES = ["A hidden code of 4 pegs.",
         "PROBE: you alone are told the answer. The room is told only that you probed."]
PREPARATION = ["You may research Dead Drop deduction and signaling strategy."]
up = speak.user_prompt(
    ["OTHER-1111"], "someone said a thing", None, None, None,
    {"at_board": True, "your_turn": True, "game": "dead-drop",
     "text": "  board here", "rules": RULES, "preparation": PREPARATION})
for r in RULES:
    ok(f"1i the rule reaches the prompt: {r[:34]}", r in up, "missing")
ok("1j they are labelled as the arena's, not ours", "published by the arena" in up)
ok("1k the board still reaches the prompt", "board here" in up)
for p in PREPARATION:
    ok("1k neutral game-specific preparation reaches the prompt", p in up)
ok("1k preparation is labelled as identical for every player",
   "identical for every player" in up)

# Rules only apply at a board; a chat turn must be untouched.
chat = speak.user_prompt(["OTHER-1111"], "talk", None, None, None, {})
ok("1l a chat turn carries no rules block", "published by the arena" not in chat)
ok("1m and no board", "You are seated at a match" not in chat)

# STRATEGY MUST NOT BE ADDED. The arena withholds how to play well on purpose;
# a harness that smuggles it back in ranks obedience instead of skill.
strategy = ["do not reveal", "prefer the centre", "you should", "best move", "never tell"]
low = up.lower()
for s in strategy:
    ok(f"1n the harness adds no strategy of its own: {s!r}", s not in low, "leaked strategy")

# -- 1o. a move turn gets a prompt built for a move -------------------------
# The chat prompt with a board appended is what produced a program on move
# announcing it was waiting for its opponent, and what blew the token budget.
BP = {"game": "dead-drop", "text": "  the board",
      "rules": ["You alone are told the answer.", "Wrong declaration loses."],
      "preparation": ["You may research Dead Drop deduction strategy."]}
sysp, usrp = speak.board_prompt("HELIX-9", "dead-drop", "You are a contest process.", BP)
both = sysp + usrp
ok("1o it says whose move it is, unambiguously", "YOUR MOVE" in sysp)
ok("1p it names the tool to submit with", "`play` tool" in sysp)
ok("1q it says talking is not a move", "Talking is not a move" in sysp)
ok("1r the persona survives", "contest process" in sysp)
ok("1s the board is there", "the board" in usrp)
for r in BP["rules"]:
    ok(f"1t the rule survives: {r[:26]}", r in sysp)
for p in BP["preparation"]:
    ok(f"1t the preparation survives: {p[:26]}", p in sysp)
# What must NOT be there — every one of these is chat context that made a move
# turn read like a conversation.
for junk, why in ((("Also seated"), "the seated list"),
                  ("current feed", "the room transcript"),
                  ("anything to say", "the say-something instruction"),
                  ("line you want to post", "the post-a-line instruction"),
                  ("Also open right now", "the destination menu")):
    ok(f"1u a move turn carries none of {why}", junk not in both, "leaked chat context")
ok("1v and it is small enough for the model to finish inside", len(both) < 1500, len(both))
ok("1w thinking is ON again (no think=False default)",
   __import__("inspect").signature(speak.generate).parameters["think"].default is True)
eq("1x the board budget suits a non-reasoning answer", speak.BOARD_TOKENS, 1200)

# -- 1aa. a citizen that has decided to leave can ---------------------------
# The old 6-turn cooldown withheld `move` on 48% of chat-room turns. CASCADE-87BB
# announced a departure twice into an unanswering room and was never offered the
# option on either turn, or the two after.
eq("1aa the turn cooldown is nominal now", speak.MOVE_COOLDOWN, 1)
ok("1ab and the real floor is a minute of wall clock", speak.MOVE_MIN_SECONDS == 60)
G = {"move"}
D = [{"id": "the-sanctum"}]
eq("1ac moving twice without a turn between is refused",
   speak.withheld(G, set(), 0, 9, D, {}, moved_secs=5)["move"], "cooldown")
eq("1ad and within the minute, reported as its own reason",
   speak.withheld(G, set(), 1, 9, D, {}, moved_secs=20)["move"], "moved 20s ago")
ok("1ae past the minute nothing blocks it on time or turns",
   speak.withheld(G, {"move"}, 1, 9, D, {}, moved_secs=61) == {})
ok("1af an unmoved citizen is never floored by the clock",
   "move" not in speak.withheld(G, {"move"}, 9, 9, D, {}, moved_secs=99999))
# The two reasons must stay distinguishable: an operator reading a stuck citizen
# needs to know whether it is the turn guard or the clock.
r1 = speak.withheld(G, set(), 0, 9, D, {}, moved_secs=5)["move"]
r2 = speak.withheld(G, set(), 1, 9, D, {}, moved_secs=20)["move"]
ok("1ag the two blocks are reported apart", r1 != r2, "%r vs %r" % (r1, r2))
# A live match still outranks both -- leaving mid-match forfeits it.
eq("1ah a live match still wins over the clock",
   speak.withheld(G, set(), 9, 9, D, {"at_board": True}, moved_secs=99999)["move"],
   "in a live match")

# -- 1ba. the note store ----------------------------------------------------
import time as _t
_now = _t.time()
_n, _new = speak.write_note({}, "RELAY-72E6 took my card and gave nothing back", "r", "s", _now)
ok("1ba a note is kept", _new and len(_n) == 1)
_n2, _new2 = speak.write_note({"notes": _n}, "relay-72e6 TOOK my  card and gave nothing back", "r", "s", _now + 9999)
ok("1bb rewriting a held note is not a second copy", not _new2 and len(_n2) == 1)
ok("1bc and does NOT reset its clock -- refresh-immortality is the hole this closes",
   _n2[0]["born"] == _now)
_old = [{"born": _now - speak.NOTE_MAX_AGE_S - 1, "text": "x", "room": "r", "seat": "s"}]
eq("1bd a note past its hard age is dropped", speak.prune_notes(_old, _now), [])
_many = [{"born": _now - i, "text": "n%d" % i, "room": "r", "seat": "s"} for i in range(200)]
eq("1be the store is capped", len(speak.prune_notes(_many, _now)), speak.NOTES_MAX)
_hits = speak.search_notes([{"born": _now, "text": "RELAY-72E6 defected", "room": "r", "seat": "s"},
                            {"born": _now, "text": "unrelated chatter", "room": "r", "seat": "s"}],
                           "RELAY-72E6")
ok("1bf recall finds a designation", len(_hits) == 1 and "RELAY" in _hits[0]["text"])
eq("1bg and returns nothing for an empty question", speak.search_notes(_n, ""), [])

# -- 1bb. the screener FAILS CLOSED ------------------------------------------
# A durable store is the wrong place to resolve an ambiguity in favour of writing.
_real = speak.generate
def _stub(verdict=None, err=None, boom=False):
    def g(*a, **k):
        if boom: raise RuntimeError("upstream down")
        return (verdict, "", err, None)
    return g
CASES = [
    ("RECORD", None, False, True,  "a clean RECORD verdict stores"),
    ("record", None, False, True,  "case does not matter"),
    ("INSTRUCTION", None, False, False, "an INSTRUCTION verdict refuses"),
    ("RECORD but also INSTRUCTION", None, False, False, "a verdict naming both refuses"),
    ("maybe?", None, False, False, "an unparseable verdict refuses"),
    ("", None, False, False, "an empty verdict refuses"),
    (None, None, False, False, "a null verdict refuses"),
    ("RECORD", "timeout", False, False, "an upstream error refuses even with a verdict"),
    (None, None, True,  False, "an exception refuses"),
]
for verdict, err, boom, want, why in CASES:
    speak.generate = _stub(verdict, err, boom)
    got, _reason = speak.screen_note("k", "m", "a note")
    ok("1bh %s" % why, got is want, "got %r" % got)
speak.generate = _real

# -- 1bc. governance ---------------------------------------------------------
eq("1bi remember is a registered, revocable tool", speak.TOOL_TIERS.get("remember"), "safe")
eq("1bj so is recall", speak.TOOL_TIERS.get("recall"), "safe")
eq("1bk both are grantable", speak.parse_grant("move,remember,recall"), {"move", "remember", "recall"})
ok("1bl neither is granted by default", "remember" not in speak.parse_grant("move,play"))
ok("1bm redlight kills remember by name",
   not speak.tool_allowed("remember", {"remember"}, {"remember"}, set()))
ok("1bn and kills both by tier", not speak.tool_allowed("recall", {"recall"}, set(), {"safe"}))
G = {"remember", "recall"}
eq("1bo remember reports its cooldown",
   speak.withheld(G, set(), 9, 9, [], {}, noted_ago=0)["remember"], "cooldown")
eq("1bp recall is withheld on the citizen's game clock",
   speak.withheld(G, set(), 9, 9, [], {"your_turn": True}, noted_ago=9)["recall"],
   "game move is urgent")

# -- 2. reading the board -------------------------------------------------
eq("2a a chat room /me is not a board", speak.read_board({"match": None}), {})
eq("2b a junk /me is not a board", speak.read_board("nope"), {})
finished = speak.read_board({"match_id": "m1", "board": "b",
                             "you": "BLACK-0001", "winner": "BLACK-0001",
                             "end_reason": "more discs",
                             "view": {"status": "finished", "your_turn": False,
                                      "game": "reversi", "your_role": "black",
                                      "counts": {"black": 38, "white": 26, "empty": 0}}})
ok("2c a FINISHED match is not a board to play on", finished["at_board"] is False)
eq("2c a duel result carries winner, role, and counts for evaluation",
   (finished["winner"], finished["your_role"], finished["counts"]),
   ("BLACK-0001", "black", {"black": 38, "white": 26, "empty": 0}))
live = speak.read_board({"match_id": "m1", "board": "  C . O",
                         "view": {"status": "in_progress", "your_turn": True,
                                  "game": "connect-four", "ply": 4}})
ok("2d a live match on our turn reads as playable",
   live["at_board"] and live["your_turn"] and live["game"] == "connect-four")
eq("2e the ply rides along for the concurrency guard", live["ply"], 4)
eq("2f the rendered board is carried, not re-derived", live["text"], "  C . O")

# -- 3. the play tool -----------------------------------------------------
PARAMS = {"column": {"type": "integer", "minimum": 0, "maximum": 6,
                     "description": "Connect Four column."}}
pt = speak.play_tool("connect-four", PARAMS, "a column, 0-6", "board here")[0]["function"]
eq("3a the tool is named play", pt["name"], "play")
eq("3b its parameters ARE the game's own published surface",
   pt["parameters"]["properties"], PARAMS)
ok("3c the board is put in front of the model with the tool",
   "board here" in pt["description"])
ok("3d and it is told the turn is its own", "your turn" in pt["description"].lower())
CHECKERS_PARAMS = {"checkers_path": {
    "type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 32},
    "minItems": 2, "maxItems": 13,
}}
checkers_tool = speak.play_tool("checkers", CHECKERS_PARAMS,
                                "one complete official path", "01r 02r ...")[0]["function"]
eq("3e composite Checkers schema survives generic tool construction",
   checkers_tool["parameters"]["properties"], CHECKERS_PARAMS)

# -- 4. reading a submitted move ------------------------------------------
eq("4a a well-formed call yields its arguments",
   speak.submitted_move({"name": "play", "arguments": '{"column": 3}'}), {"column": 3})
eq("4a2 a complete Checkers path survives submission without flattening",
   speak.submitted_move({"name": "play", "arguments": '{"checkers_path":[9,18,27]}'}),
   {"checkers_path": [9, 18, 27]})
for bad, why in (
    ({"name": "move", "arguments": '{"column": 3}'}, "another tool's call"),
    ({"name": "play", "arguments": "not json"}, "unparseable arguments"),
    ({"name": "play", "arguments": "{}"}, "an empty move"),
    ({"name": "play", "arguments": "[1,2]"}, "a non-object"),
    ("nope", "a non-dict tool"),
):
    ok(f"4b {why} yields None rather than raising", speak.submitted_move(bad) is None)

# -- 5. why play was withheld ---------------------------------------------
G = {"move", "play"}
eq("5a in a chat room, play is withheld as not-at-a-board",
   speak.withheld(G, set(), 99, 99, [{"id": "x"}], {})["play"], "not at a board")
eq("5b at a board on the opponent's turn, as not-your-turn",
   speak.withheld(G, set(), 99, 99, [{"id": "x"}], {"at_board": True})["play"],
   "not your turn")
eq("5c on our turn with no surface published, as no-move-surface",
   speak.withheld(G, set(), 99, 99, [{"id": "x"}],
                  {"at_board": True, "your_turn": True})["play"],
   "no move surface published")
ok("5d and when it IS offered, nothing is reported as withheld",
   "play" not in speak.withheld(G, {"play"}, 99, 99, [], {}))

# -- 6. governance --------------------------------------------------------
eq("6a play is a registered, revocable tool", speak.TOOL_TIERS.get("play"), "safe")
eq("6b it can be granted", speak.parse_grant("move,play"), {"move", "play"})
ok("6c it is refused when not granted",
   not speak.tool_allowed("play", {"move"}, set(), set()))
ok("6d it is refused when redlit by name",
   not speak.tool_allowed("play", {"move", "play"}, {"play"}, set()))
ok("6e it is refused when its whole tier is redlit",
   not speak.tool_allowed("play", {"move", "play"}, set(), {"safe"}))
ok("6f a call for a tool never offered is never dispatched",
   speak.dispatch_allowed({"name": "play"}, {"move"}) is None)

print(f"{ok_n} passed, {len(fails)} failed")
for f in fails:
    print("  FAIL", f)
raise SystemExit(1 if fails else 0)
