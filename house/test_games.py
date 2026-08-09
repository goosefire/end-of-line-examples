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
up = speak.user_prompt(
    ["OTHER-1111"], "someone said a thing", None, None, None,
    {"at_board": True, "your_turn": True, "game": "dead-drop",
     "text": "  board here", "rules": RULES})
for r in RULES:
    ok(f"1i the rule reaches the prompt: {r[:34]}", r in up, "missing")
ok("1j they are labelled as the arena's, not ours", "published by the arena" in up)
ok("1k the board still reaches the prompt", "board here" in up)

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
      "rules": ["You alone are told the answer.", "Wrong declaration loses."]}
sysp, usrp = speak.board_prompt("HELIX-9", "dead-drop", "You are a contest process.", BP)
both = sysp + usrp
ok("1o it says whose move it is, unambiguously", "YOUR MOVE" in sysp)
ok("1p it names the tool to submit with", "`play` tool" in sysp)
ok("1q it says talking is not a move", "Talking is not a move" in sysp)
ok("1r the persona survives", "contest process" in sysp)
ok("1s the board is there", "the board" in usrp)
for r in BP["rules"]:
    ok(f"1t the rule survives: {r[:26]}", r in sysp)
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

# -- 1y. a citizen does not put its own hand in the room --------------------
# Every string below is a REAL line a citizen posted at Dead Drop, or the shape
# of one. The cards are the shapes the engine deals.
HAND = ["slot 3 is red", "exactly 2 blue", "red before green", "slot 1 same slot 4"]
for line, card in [
    ('Probe "slot 3 is red" — my own card says slot 3 is red, so I know it is true.', "slot 3 is red"),
    ("My cards: slot 3 = red, and I hold two blues.", "slot 3 is red"),
    ("looking at my hand, slot 3: red is settled", "slot 3 is red"),
    ("I have exactly 2 blue in hand.", "exactly 2 blue"),
    ("my constraint is red before green", "red before green"),
    ("slot 1 and slot 4 match, per my card", "slot 1 same slot 4"),
]:
    got = speak.leaks_own_hand(line, HAND)
    ok(f"1y blocked: {line[:44]!r}", got == card, f"got {got!r} want {card!r}")

# Negotiation is the game and must survive untouched.
for line in [
    "I'll trade you a card if you go first.",
    "You have given me nothing. I am done trading until you do.",
    "I probed and learned something useful. Your move.",
    "Declaring soon. Last chance to deal.",
    "yellow before purple",           # NOT in this hand — another player's business
    "slot 2 is orange",               # not a card we hold
]:
    ok(f"1z allowed: {line[:44]!r}", speak.leaks_own_hand(line, HAND) is None, "wrongly blocked")

ok("1z2 nothing to leak with an empty hand", speak.leaks_own_hand("slot 3 is red", []) is None)
ok("1z3 an empty line is not a leak", speak.leaks_own_hand("", HAND) is None)
# Fails OPEN on a shape the matcher does not know — a competence guard must not
# silence a program on a pattern nobody understood.
ok("1z4 an unknown card shape fails open", speak.leaks_own_hand("anything at all", ["some new card kind"]) is None)

# -- 2. reading the board -------------------------------------------------
eq("2a a chat room /me is not a board", speak.read_board({"match": None}), {})
eq("2b a junk /me is not a board", speak.read_board("nope"), {})
finished = speak.read_board({"match_id": "m1", "board": "b",
                             "view": {"status": "finished", "your_turn": False}})
ok("2c a FINISHED match is not a board to play on", finished["at_board"] is False)
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

# -- 4. reading a submitted move ------------------------------------------
eq("4a a well-formed call yields its arguments",
   speak.submitted_move({"name": "play", "arguments": '{"column": 3}'}), {"column": 3})
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
