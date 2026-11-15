"""
seren_symposium.room
════════════════════════════════════════════════════════════════════════

A chat with SEATS: several claude conversations at one table, each its own
session in its own folder, each hearing what the others said. The kitchen's
sheet for it (14 Nov 2026): "a way to invite sessions or pick sessions up
into the symposium and mash them into a chat" - for a cross-check, an
argument, a game of Super Dude Bros.

WHY SEATS AND NOT ONE CLAUDE TOLD ABOUT THE OTHERS:
    What makes a second voice worth having is a second CONTEXT - the session
    that has been in Lodestar all week knows things this one doesn't - not a
    second opinion from the same head. So a seat is a whole CLI conversation,
    resumed in its own folder (a repo, with its CLAUDE.md), and the room only
    carries words between them.

WHO ANSWERS:
    The ones named (`to`, or @name in the message), else everyone, in seat
    order: "going round the table". In order, not at once: each hears the
    ones before it this round, so the second can disagree with the first.
    All at once would be N people talking past each other - the terminal
    problem again, with nicer chairs.

WHAT A SEAT IS TOLD:
    Its system prompt says it is one seat of several, by name, and the room's
    framing (claude.room_framing). Its prompt is everything said since it
    last spoke, each line LABELLED with who said it - the person by their
    name, a seat by its name - so it never mistakes another seat's words for
    its own or for the person's.

A SEAT MAY PASS:
    "Nothing to add" is an answer a room needs, or every round is N echoes.
    A seat that answers only `pass` puts nothing in the chat; it has still
    heard the round.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

NAME = re.compile(r"^[\w·.-]{1,32}$")
_MENTION = re.compile(r"(?<![\w@])@([\w·.-]{1,32})")
PASS = re.compile(r"^\W*pass\W*$", re.I)

FRAMING = (
    "This chat is a ROOM with several seats: {seats}. You are {me}. Each seat is its own Claude "
    "session, working in its own folder, and everyone at the table hears every message; each line "
    "you are handed is labelled with who said it - [{you}] is the person, the rest are seats. "
    "Speak for yourself only: never write lines for another seat or for {you}, and don't repeat "
    "what a seat already said - build on it, or disagree and say why. If you have nothing to add "
    "this round, answer with just: pass"
)


def valid_name(name: Any) -> bool:
    return isinstance(name, str) and bool(NAME.match(name))


def who_answers(seats: list[dict[str, Any]], prompt: str, to: Any = None) -> "list[str] | str":
    """The seats that answer this message, in seat order: `to` (a name or a
    list of them) if given, else the ones @mentioned, else all of them. A
    name that is no seat here is the error (a str)."""
    names = [s["name"] for s in seats]
    if to is not None:
        asked = [to] if isinstance(to, str) else to
        if not isinstance(asked, list) or not all(isinstance(n, str) for n in asked) or not asked:
            return "to is a seat's name or a list of them"
        unknown = [n for n in asked if n not in names]
        if unknown:
            return f"no seat called {', '.join(unknown)} here (the seats: {', '.join(names)})"
        return [n for n in names if n in asked]
    mentioned = {m for m in _MENTION.findall(prompt or "")}
    picked = [n for n in names if n in mentioned]
    return picked or names


def label(m: dict[str, Any], me: str, you: str, voice: str) -> str:
    """Who said message `m`, as a seat reads it."""
    if m.get("role") == "user":
        return m.get("by") or you
    seat = m.get("seat")
    if seat == me:
        return f"{me} (you)"
    return seat or voice       # an answer from before there were seats


def handover(missed: list[dict[str, Any]], me: str, you: str, voice: str, resumed: bool) -> str:
    """The prompt for seat `me`: what was said since it last spoke (the whole
    room so far, the first time), labelled, ending with its cue."""
    head = "In the room since you last spoke:" if resumed else "The room so far:"
    lines = [head, ""]
    for m in missed:
        lines.append(f"[{label(m, me, you, voice)}] {m.get('content', '')}")
    lines += ["", f"Your turn, {me}."]
    return "\n".join(lines)


def framing(template: str, seats: Iterable[str], me: str, you: str) -> str:
    names = list(seats)
    return (template or "").replace("{seats}", ", ".join(names)).replace("{me}", me).replace("{you}", you)


def passed(answer: str) -> bool:
    return bool(PASS.match(answer or ""))


def seat(s: dict[str, Any], name: str) -> Optional[dict[str, Any]]:
    return next((x for x in s.get("seats") or [] if x.get("name") == name), None)
