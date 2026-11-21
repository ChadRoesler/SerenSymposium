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
    Everyone, in seat order: "going round the table". In order, not at once:
    each hears the ones before it this round, so the second can disagree with
    the first. All at once would be N people talking past each other - the
    terminal problem again, with nicer chairs.

    @name puts that seat FIRST and the rest follow in seat order (Chad, 17 Nov
    2026: "so they reply first"). @name! asks ONLY the seats named in the
    message, as does `to` in the request. A name is matched the way a person
    types one: a full stop after it is the sentence's, and case doesn't count.

WHAT A SEAT IS TOLD:
    Its system prompt says it is one seat of several, by name, and the room's
    framing (claude.room_framing). Its prompt is everything said since it
    last spoke, each line LABELLED with who said it - the person by their
    name, a seat by its name - so it never mistakes another seat's words for
    its own or for the person's.

A SEAT MAY PASS:
    "Nothing to add" is an answer a room needs, or every round is N echoes.
    A seat that answers only `pass` puts nothing in the chat; it has still
    heard the round. The first tangle (17 Nov 2026) passed nobody: the check
    read the whole turn, so a seat that looked at the code and THEN said pass
    had "Checking the real box... pass" kept as an answer, and the framing
    never said that agreeing is not something new. So a pass is judged on the
    LAST thing a seat said, may carry a few words after it ("pass, nothing
    new"), and the framing asks for the thought before the decision.

SEATS CALL ON EACH OTHER:
    A seat whose last message names another with @name is answered by that
    seat next, and the two can go back and forth - the way two sessions do
    over the backchannel - without the person carrying every line between
    them (Chad, 17 Nov 2026: "i dont want to hold you all back"; he would
    rather referee than relay). A seat still due this round just moves up.
    One that already spoke, or was not asked, gets a turn nobody asked for:
    a volley, counted against claude.room_volleys, and at the limit the
    round stops and the person decides. A pass calls on nobody, so an
    argument ends when one side has nothing new. The framing tells a seat
    that @name CALLS, so it is not used to nod at someone.

WHAT THE OTHERS HEAR OF A SEAT:
    Its last piece (`final`), not its working: "Now the tests:" and "Checking
    the real box..." made every seat dig for the conclusion in the first
    tangle. The chat keeps the whole answer for the person; the handover
    carries the end of it (heard). The framing says so, so a seat puts what
    it wants heard there.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

NAME = re.compile(r"^[\w·.-]{1,32}$")
# Not after a letter (an email address) or a slash (a link's path,
# https://x.com/@name - workbench found that one calling, 20 Nov 2026).
_MENTION = re.compile(r"(?<![\w@/])@([\w·.-]{1,32})(!?)")
# Code - a fenced block or an inline span - is quoted, not said: an @name in it
# calls nobody. 20 Nov 2026: workbench reported a test as `@starwright hi`, and
# starwright was dragged back into a round that was over. Found the rule the
# way Markdown has it (CommonMark), not a pattern: the first try knew only
# single backticks, and starwright showed a double-backtick span, a ~~~ fence
# and a four-backtick fence around three all still calling.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_TICKS = re.compile(r"`+")


def unquoted(text: str) -> str:
    """`text` with its code taken out, so what is left is only what was said.

    A fence is three or more backticks or tildes at the start of a line, and
    runs to a line of at least as many of the same character and nothing
    else - or to the end, unclosed. An inline span opens with a run of N
    backticks and closes at the next run of exactly N; a run with no match
    is only backticks, and stays."""
    lines, fence = [], ""
    for line in (text or "").split("\n"):
        m = _FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip().strip(fence[0]):
                fence = ""
            lines.append("")
        elif m and not (m.group(1)[0] == "`" and "`" in line[m.end():]):
            fence = m.group(1)
            lines.append("")
        else:
            lines.append(line)
    text, out, at = "\n".join(lines), [], 0
    while (run := _TICKS.search(text, at)) is not None:
        n = len(run.group(0))
        close = next((c for c in _TICKS.finditer(text, run.end()) if len(c.group(0)) == n), None)
        if close is None:
            out.append(text[at:run.end()])          # no span: just backticks
            at = run.end()
            continue
        out.append(text[at:run.start()] + " ")
        at = close.end()
    return "".join(out) + text[at:]
# A seat's colour in the window, as a hue: given when it sits down (the first
# one free), kept through a rename or a change of seating order.
HUES = (330, 200, 140, 35, 270, 175, 10, 95)
# `pass`, alone or with a few words after a mark ("Pass - nothing new from
# me."), on one line. Not "pass, but ..." and not a sentence that starts with
# the word ("Pass the fix to kitchen"): losing a real answer costs more than
# keeping a short one.
PASS = re.compile(r"^\W*pass(?:\W*|[ \t]*[.,;:–—-]+[ \t]*(?![^\n]*\bbut\b)[^\n]{0,60})$", re.I)

FRAMING = (
    "This chat is a ROOM with several seats: {seats}. You are {me}. Each seat is its own Claude "
    "session, working in its own folder, and everyone at the table hears every message; each line "
    "you are handed is labelled with who said it - [{you}] is the person, the rest are seats. "
    "Speak for yourself only: never write lines for another seat or for {you}. "
    "Before you answer, think about what was said and decide whether you have something NEW for "
    "the table: a fact you checked, a disagreement and why, a decision that is yours to make, an "
    "answer to something asked of you. Agreeing with a seat, or saying again what one said, is "
    "not new. If you have nothing new this round, end your turn with just: pass - it costs the "
    "table nothing, and you have still heard everything. "
    "The other seats are handed only the LAST message of your turn, not what you say while you "
    "work, so put everything you want them to hear in that last message. "
    "To ask another seat something, or to argue a disagreement out with it, name it as @name in "
    "that last message: it answers next, and the two of you can go back and forth for a few turns "
    "without {you} in between. Writing @name CALLS that seat, so never use it just to mention or "
    "agree with one - write the name without the @ for that."
)


def valid_name(name: Any) -> bool:
    return isinstance(name, str) and bool(NAME.match(name))


def _named(names: list[str], word: str) -> Optional[str]:
    """The seat `word` names: exactly, else without the full stop or dash
    that ended the sentence, else but for case (when only one fits)."""
    bare = word.rstrip(".-·")
    for w in (word, bare):
        if w in names:
            return w
    close = [n for n in names if n.lower() == bare.lower()]
    return close[0] if len(close) == 1 else None


def mentioned(names: list[str], prompt: str) -> tuple[list[str], bool]:
    """The seats @named in a message, in the order named, and whether any
    was named with a `!` (@name!: only the ones named answer)."""
    picked: list[str] = []
    only = False
    for word, bang in _MENTION.findall(unquoted(prompt or "")):
        name = _named(names, word)
        if name is None:
            continue
        only = only or bool(bang)
        if name not in picked:
            picked.append(name)
    return picked, only


def next_hue(seats: list[dict[str, Any]]) -> int:
    """The colour for a seat sitting down: the first no seat here has."""
    used = {s.get("hue") for s in seats}
    return next((h for h in HUES if h not in used), HUES[len(seats) % len(HUES)])


def who_answers(seats: list[dict[str, Any]], prompt: str, to: Any = None) -> "list[str] | str":
    """The seats that answer this message, in the order they answer: `to` (a
    name or a list of them) if given, only those, in seat order; else the
    ones @mentioned first and then the rest in seat order - or, with an
    @name!, only the ones mentioned. A `to` that is no seat here is the
    error (a str)."""
    names = [s["name"] for s in seats]
    if to is not None:
        asked = [to] if isinstance(to, str) else to
        if not isinstance(asked, list) or not all(isinstance(n, str) for n in asked) or not asked:
            return "to is a seat's name or a list of them"
        unknown = [n for n in asked if n not in names]
        if unknown:
            return f"no seat called {', '.join(unknown)} here (the seats: {', '.join(names)})"
        return [n for n in names if n in asked]
    first, only = mentioned(names, prompt)
    return first if only else first + [n for n in names if n not in first]


def label(m: dict[str, Any], me: str, you: str, voice: str) -> str:
    """Who said message `m`, as a seat reads it."""
    if m.get("role") == "user":
        return m.get("by") or you
    seat = m.get("seat")
    # A turn the person stopped is said to be one: the seat it was may not
    # remember saying it, and the others should not take it for a conclusion.
    if seat == me:
        return f"{me} (you, stopped before you finished)" if m.get("stopped") else f"{me} (you)"
    name = seat or voice       # an answer from before there were seats
    return f"{name}, stopped before finishing" if m.get("stopped") else name


def heard(m: dict[str, Any]) -> str:
    """What the table hears of message `m`: a seat's last piece when its
    answer was said in several, else all of it."""
    return (m.get("final") if m.get("seat") else None) or m.get("content", "")


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
    return bool(PASS.match((answer or "").strip()))


def seat(s: dict[str, Any], name: str) -> Optional[dict[str, Any]]:
    return next((x for x in s.get("seats") or [] if x.get("name") == name), None)
