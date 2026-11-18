# SerenSymposium

**The front door to the Seren constellation.** One window, three tabs: **Chat**, where the talking happens; **Settings**, for Symposium as a whole — you, Claude and its carabiner, the projects (each chat has its own behind ⚙); and **Backend**, a card for every sibling service with a little widget saying whether it's there, which opens that service's own operator page in your browser.

Symposium is not "a chat app," and it is emphatically not another service. It is the **desktop shell** the rest of the family already earned — Memory, Workbench, Lodestar and Probe each serve a perfectly good `/viewer` on the shared Meninges baseplate, and reimplementing any of them inside a client would be building a second copy of something that already works. So Symposium points at them. What it adds is the one surface nobody else owns: a place to actually *talk* to the stack.

---

## Why it's shaped like this

**It is a client, not a service.** Every other member of the family binds a port to be connected *to*. Symposium binds one to serve *itself* and connects outward to exactly one place: [SerenLodestar](https://github.com/ChadRoesler/SerenLodestar), the cluster head. It authenticates nobody. It holds a bearer it *presents*; it never checks one.

**One endpoint, on purpose.** Symposium talks to Lodestar and nothing else. It does not know what a tool is, does not route, does not touch MCP, does not reach a leaf service directly — Lodestar owns all of that. That erosion is gradual and easy to miss in review, so it isn't a convention, it's a **test**:

```python
def test_symposium_exposes_no_route_that_bypasses_lodestar(client):
    paths = {r.path for r in client.app.routes}
    ...
```

Add a route that calls Memory directly and the suite goes red. If logic starts accumulating in the shell, it belonged in the cluster head.

**The other things it does are local, and one fetch.** A chat can be answered by Claude instead of a local model, and Claude lives in the `claude` CLI on your machine — with its own login, MCP servers and hooks; Symposium runs that program as a subprocess ([Voices](#voices-who-answers)). Screen share takes a still of this desktop, here ([Files, images and screen share](#files-images-and-screen-share)). The one outbound call that is not to Lodestar: an answer that shows an image from the web gets it saved into the chat, so the chat still has it when the link rots — `files.fetch_images: false` turns that off and leaves such images as links.

**Why a local server instead of just JavaScript → Lodestar.** Two reasons, both practical rather than architectural:

1. The bearer stays in Python. It is never serialised into a webview's JavaScript, and a test asserts the token never appears in `/api/config`.
2. Lodestar never needs CORS opened for a desktop app.

The cost is one loopback port. That's the whole trade.

**There is no inbound auth, and that is a design constraint, not an omission.** Symposium binds `127.0.0.1` only — anything that can reach it is already on the machine. Widen `ui.host` past loopback and you have not "exposed the UI," you have built a different, unauthenticated service. It refuses to start unless you set `ui.allow_open_lan`, and then says so every boot.

**Loopback keeps the LAN out, not the browser.** Any page open in any browser on the machine can send a request to `127.0.0.1:7426`, and `/api/chat` runs Lodestar's tools with your bearer. So every request is checked: the `Host` must be a loopback name (a DNS-rebound `evil.example` pointing at 127.0.0.1 still says `evil.example`), an `Origin`, when sent, must be Symposium itself, and a POST or PATCH must be `application/json` — which no cross-site form can send without a CORS preflight, and Symposium answers no preflight.

---

## Folders are context boundaries, not filing

The shell calls them **projects**, and a session in one a **chat** — art chats here, programming chats there, the memes channel over there. The config and the API keep the old names.

The easiest thing to mistake in the config. Folders exist to stop **context drift**, and grouping is the side effect rather than the point.

A session opened in `seren stack` already knows what it is about — it inherits a system prompt, a model, a temperature, a set of memory tags. One opened in `general` inherits none of it and doesn't drag the last three hours of infrastructure talk along with it.

Four layers, split by *how often you touch a thing* rather than by what it is:

| Layer | Holds | Where you change it |
|---|---|---|
| per-install | lodestar url, ui port, confirm_destructive | `seren-symposium.yaml` |
| per-folder | voice, system prompt, model, temperature, max tokens, repeat penalty, memory tags | folder preset |
| per-session | overrides of the above | the session's **settings** (⚙ in its title bar, a new chat's too: set before the first message, they go with it) |
| per-message | the message | the composer |

**System prompts add up** rather than override: a chat's own prompt is added after its project's. Untick *after its project's prompt* in the chat's settings and it has only its own.

**The prompt stack** (the layers button in a chat's title bar, or *the whole prompt stack* in its settings) shows what that chat's model is actually given, layer by layer, with sizes (characters, and tokens at about four to one, est.). It's there to find what's quietly eating the context, like tools nobody uses or a memory file that grew. For a Claude chat that's the CLI's own prompt (built in, so it can't be read; a preface replaces it), the project's prompt, the chat's and the framing; then what the CLI reads itself as it starts (CLAUDE.md up from its working folder, the user's, the auto-memory index) and whether a session-start hook adds more; then every tool the CLI said it loaded on the chat's last turn, grouped by MCP server with each server's status, plus its skills, agents, commands and plugins; then the permissions and whether the next turn resumes. Before a chat's first answer, the tools are another chat's. For a local model it's what Lodestar would send, asked of Lodestar itself (`/api/v1/chat/inspect`): its tool descriptions in front of the project's and the chat's prompts, then the history and the sampling settings. A project has one too, from its settings: what its chats start with.

---

## Sessions

Every conversation is a **session**, kept as one JSON file in the sessions folder (`sessions.dir`; blank means a `sessions` folder next to your yaml). A session belongs to a folder, can be renamed, moved to another folder, exported, imported and deleted (with a confirmation while `ui.confirm_destructive` is on), and carries its own settings on top of the folder's — blank a setting and the folder's value shows through again.

They live on disk, in Python, rather than in the webview's `localStorage`, because that belongs to an *origin*, and the origin has the port in it: moving `ui.port` would have silently lost every conversation. A file is also something you can back up and read.

Only a turn that came back is written. A failed one is shown in red, its prompt and files go back in the composer, and it never becomes context that later messages replay. **Retry** asks the last prompt again; the answer it replaces stays on disk until the new one is in.

Every message carries its own tools — the ones the design drew, plus the two an edit is really for:

| On | Tool | Does |
|---|---|---|
| your message | **edit** | goes back to just before it, and puts its words and files back in the composer to change and send again |
| the last answer | **retry** | asks again |
| an earlier answer | **rewind** | keeps the chat up to here; what came after goes |
| any message | **fork** | a new chat in the same project holding everything up to here, files included; the original is untouched |
| any message | **delete** | takes that one message out |

Messages and answers render as **Markdown** — code blocks with highlighting and a copy button, tables, quotes, links (opened outside the window). The renderer ships with Symposium (`ui/vendor`, versions in its README), so it works with the network down, and every bit of HTML it makes is sanitised before it reaches the page.

Answers stream as they're written. When the model reaches for a tool you see `calling recall…` while it runs, and under every answer a receipt: the model, the node, the tools it used, and how long it took.

---

## Voices: who answers

Every chat has a **voice**, layered like any other setting (the chat's, else its project's, else `lodestar`):

| Voice | Who answers | How |
|---|---|---|
| `lodestar` | a local model | the chat goes to Lodestar, which routes it to a node — as above |
| `claude` | Claude | the `claude` CLI on this machine, with its own login, MCP servers and hooks |

A chat's first Claude turn starts a CLI conversation; every later turn **resumes** it (`--resume`), so the CLI keeps the thread and no history goes on the wire. Switch a chat's voice and back and nothing is lost: the turns the other voice answered are handed to Claude, once, in front of the next prompt — and a chat that started with a local model hands over everything before it.

What runs is the yaml's (`claude:` — the program, its working directory, the tools it may use without asking); a request supplies only the prompt, on stdin. One Claude turn at a time per chat — two would both resume the same conversation. The stream is translated into Lodestar's shape (`chunk`, `tool_status`, `done`, `error`), so the shell reads one stream whoever is answering; the receipt names the Claude model and the voice's `name`.

**Asked, not refused.** `-p` has nobody at a terminal to answer "may I?", so a tool not on `allowed_tools` used to be refused outright. With `claude.ask` (on by default) the CLI is given a permission tool of Symposium's own instead (`--permission-prompt-tool`): `seren_symposium.permit`, a small stdio MCP server the CLI starts for the turn. It asks Symposium over loopback, the ask shows in the chat — the command, the file, the address — and waits on **Allow once**, **Always**, or **Deny**. *Always* offers widths, narrowest first — the folder a file is in or every read, the first word of a command or the whole shell, one MCP tool or every tool on its server — and where to keep it: **this chat**, **this project**, or **every chat**. When the CLI asks about something a rule kept here already covers (it spells some differently), the window answers yes without bothering you; a rule it can't read plainly is never taken to cover anything. Where to ask and the turn's one-time ticket ride in the CLI's environment, never its command line, and an ask without a running turn's ticket is refused.

**Permissions in layers**, each more particular than the one before: the **CLI's own settings** (the base; Symposium only ever adds to them), the **carabiner's** `session:` block (the box's defaults for a session of this harness — see SerenCarabiners), **every chat** (Settings → Claude, over the yaml's `claude.permission_mode` and `allowed_tools`), the **project**, and the **chat** (its ⚙). The mode is the most particular one set — a chat's, else its project's, and so on down; none set and the CLI decides. What may run without asking gathers from the chat upward, until a layer has *also what … allows* unticked: a red-team chat that should ask about everything says so in its own settings, whatever its project or the box allow. The chat sheet shows the result — which mode, from where, and how many rules each layer gave. The window offers every mode the CLI has but `bypassPermissions`, which only the yaml or the carabiner can say, on purpose.

**How it got there.** Folded away in the answer: Claude's thinking, each tool it used with what it was given and what came back, and each time it asked you — live while it works, kept in the answer's receipt after. A local model's `<think>…</think>` folds the same way. What Claude *says* before and between its tools ("let me look in that folder") is the answer too: each piece is shown where it was said, with the steps between pieces folded in their own place, and the kept answer is every piece, not only the last (the CLI's own result is only the text after the last tool). While an answer comes, a line under it says what it is doing and for how long, and the window's title carries a dot.

**Settings → Claude** shows the voice's permission mode, whether it asks, what it may use without asking and the folders it may work in besides its own — and, beside them, the **carabiner** clipped on this box (SerenCarabiners' `claude.yaml`, found through `claude.carabiner`, `KBH_CONFIG`, `<cwd>/kbh` or `~/.seren/kbh`): where its sessions work and which services it routes to. Read for show only; its tokens never leave the file.

**The CLI can't unsay anything**, so a change to messages its conversation has seen — an edit, a rewind, a deleted message, a retry — doesn't pretend otherwise: the next turn starts a *new* CLI conversation, handed the chat as it now stands (up to `claude.handover_max_chars`). A fork, not an undo. Changes past what it saw leave its conversation alone.

Each turn is also told where it's answering (`claude.framing`, after the chat's own system prompt): that replies render as Markdown, where the chat's files are, and how to show an image.

**A room: seats at one table.** A chat can have **seats** (⚙ → seats → *invite*, or *from a terminal…*): each its own Claude conversation, in its own folder, with a name — `wren·kitchen`, `wren·lodestar`. A message goes **round the table in seat order**, and each seat is handed everything said since it last spoke, every line labelled with who said it (`[Chad]`, `[lodestar]`), so the second can disagree with the first instead of both talking past you. `@name` in a message puts that seat **first**, and the rest follow in seat order; `@name!` (or `to` on the wire) asks **only** the seats named. Typing `@` in a room lists who is at the table, so a name is picked rather than spelled (↑ ↓, Enter or Tab; Esc puts the list away). A seat with nothing new ends its turn with `pass` (a few words after it are fine: "pass, nothing new"), which puts nothing in the chat but still counts as having heard the round; it is judged on the last thing the seat said, so a seat that read the code first and then found nothing new has still passed. The others are handed each seat's **last message**, not what it said while it worked ("Now the tests:"); the chat keeps the whole answer for you. Each seat's system prompt says it is one seat of several, by name, to think before deciding whether it has something new, and that only its last message travels (`claude.room_framing`). **Seats call on each other:** a seat whose last message names another with `@name` is answered by it next, so two can argue a point out without you passing every line between them. A seat still due this round only moves up; a turn nobody asked for counts against `claude.room_volleys` (6; 0 turns it off), and at the limit the round stops and says so, for you to referee. A pass calls on nobody, so an argument ends when one side has nothing new. **stop**, on the answer as it comes, ends the round where it is: what had come back is kept. The prompt stack (the layers button) is **per seat** in a room, since each is given its own folder's memory files and hooks and has its own share of the chat still to hear. Each seat has its own colour, given when it sits down; under ⚙ → seats, ↑ ↓ change who answers before whom, a seat's name renames it (what it said goes with it) and its dot gives it another colour. The point is not five opinions from one head, it's five **contexts**: the seat that's been in Lodestar all week knows things this one doesn't. Inviting the first seat into a chat that already has a Claude keeps that Claude at the table as a seat of its own (named from its folder), with its conversation intact. A seat that leaves takes nothing with it; what it said stays, under its name. A room's round is one turn: it streams (`seat` opens each seat's part, `round` ends it), runs on if you close the window, and holds the chat until the last seat is done.

**Picked up, not copied.** A conversation started in a terminal can carry on here: **pick up…** at the foot of the chat list shows the `claude` CLI's own conversations on this box (its transcripts, `~/.claude/projects` or `claude.projects_dir`, read and never written) that aren't a chat yet. Picked, one becomes a chat that *is* that conversation, not a transcript of it: its words come over for you to read, and Claude's next turn `--resume`s it, so nothing is handed over twice. It runs in the folder it began in (the chat's own **works in**, under ⚙), because `--resume` only finds a conversation from there, and that repo's CLAUDE.md and project settings are the ones it had. A conversation touched in the last ten minutes may still be open in its terminal, and two processes on one transcript would each answer without the other's turns, so that one is **forked** (`--fork-session` on its first turn here: a new id, the same past) and the terminal keeps its own; an older one is taken over, and a later `claude --resume` at the terminal sees what was said here. Only what began at a terminal is offered: a headless run — the hippocampus waking a session, a ripple, a chat Symposium itself started — stays where it was started, and asking for one by id is refused. What comes over is the words, not the workings: what you typed (including what you said mid-answer) and what was said back. The tools, the thinking and the hooks stay in the transcript, where the resumed conversation still has them.

---

## Files, images and screen share

**Attaching.** Drop files on the chat, paste an image into the composer, or click the tray above it. They wait in the tray — × takes one out, **clear** empties it — and go up when you send; a message can be files alone. Each chat keeps its files in its own folder beside the session file (`<sessions>/<id>.files/`), and the **Files** panel on the right lists everything the chat has carried, newest first — who posted it, its name, when, and a thumbnail — searchable, as a list or a grid of thumbnails, and never more than half the rail tall. Pictures open large on a click. Deleting the chat deletes its folder; an edit or a deleted message never does, so the file is still there to send again. One file is at most `files.max_mb` (25 MB).

**What each voice gets.** Claude opens files itself: it is given each file's path, the chat's folder is added to what the CLI may read (`--add-dir`), and it reads a meme the way it reads anything. A local model can't open a path, so a text file's contents go into the prompt (up to `files.inline_max_chars`, cut with a note past that), and an image or other binary is named so the model knows something was there. Showing pictures to a model that can see is Lodestar's job, and a later one.

**Images coming back.** An answer shows a picture as Markdown. One from the web — `![caption](https://...)` — is fetched when the answer comes back, kept in the chat, and the answer pointed at the copy; only real image bytes are kept, whatever the server claimed. And Claude can **make** one: with `claude.save_files` (on by default) each turn may write into that chat's folder — and only there, past whatever `permission_mode` and `allowed_tools` already allow — so an SVG it draws, or notes, or code it saves, lands in the chat and shows in the Files panel. Anything an answer links that isn't in the chat stays a link; the window never loads a picture from the internet on its own.

**The picture panel is the chat's.** What you asked for, the prompt drafted from it, the negative, the seed and the count belong to the chat they were written in: open another chat and its own are there (or nothing), come back and yours are where you left them, a restart included. The model, the size and the LoRAs worn become a chat's own the moment you pick one there; a chat that has not picked starts from the ones used last, so a new chat is not a cluster to choose from all over again. A **translate** is answered into the chat it was asked in, even if you have moved on by the time it comes. A plan is not kept: ask again. All of this is the window's to remember (this browser's storage, the last 40 chats), not the session file's.

**What translate is given.** The panel's **translate** has whoever answers the chat write the prompt in the picked model's style. What it writes it *from* is a choice (**translate from**, in the panel; `translate` in a chat's settings, `ui.translate` for every chat, also under Settings → *picture prompts from*):

- **my words only** (`plain`): the request and nothing else, in a blank one-shot session. "A picture of us at the table" has no idea who "us" is.
- **my words and this chat** (`chat`, the default): the chat's latest messages go along as text, who said what (`ui.translate_messages`, 12; `ui.translate_max_chars`, 8000; a long message keeps its end). Any voice, a room included. It sees what was *said*, not the tools or the thinking.
- **the whole conversation** (`fork`): a side copy of the chat's own Claude conversation writes it — `--resume … --fork-session`, so it was there for everything — and is thrown away: `--no-session-persistence` (no transcript), hooks off (no bookmark, no kitchen magnet for a session gone a moment later), nothing allowed, and it is stopped if it reaches for a tool, before the tool runs. The chat never hears of it. It runs as a turn there would (same system prompt, model and folder), so the CLI's cache of the conversation is reused; it still reads all of it, which makes it the slow one. Claude chats only: a room (a conversation per seat), a local model's chat, or a chat with no conversation yet is given `chat` instead, and the panel says so. And where nothing has been said yet, `chat` is just the words.

Files come back to the window from Symposium's own address — the one that can drive the API — so nothing is ever served as a page: pictures as pictures (an SVG's script never runs in an `<img>`), text as plain text, everything else as a download, all with `nosniff` and a sandboxing CSP.

**Screen share** takes stills of a display — the main one unless you pick another — or, on Windows, of one window (drawn by the window itself, so one half-covered still comes out whole), the old SerenChat way, in two modes:

- **polling** — a fresh frame every few seconds, previewed in the panel; the newest one sits in the tray as **screen · …** and rides along with your next message, once (× skips it).
- **push** — **take one** shows you the frame; **send this** sends it, with whatever's in the composer.

Nothing is shared by looking: the preview is only ever on your side until you send. A frame that went is kept in the chat but marked, so it shows as a small chip in its message and in the panel's own **shared in this chat** strip — not in the conversation, and not in Files (a box there brings them in).

Whoever answers sees a turn, not a feed — the frame that came with the message — so stills are everything an answer can use, at a fraction of video's bytes. They're taken by Symposium in Python (`mss`, scaled by Pillow to `screen.max_width`), not by the webview, so there's no permission prompt per frame. Both come with the `[desktop]` extra, or `[screen]` on its own; without them the panel says what to install. `screen.enabled: false` turns it off.

**Ears** are push-to-talk. Hold **🎙 hold to talk** under the composer (or Ctrl+Space anywhere in the window), say it, let go. Two things come back: the words, put in the composer for you to send or fix, and the clip itself, which sits in the tray and rides with the message like any other file — so whoever answers has what you said, not only what whisper made of it, and a clip in a chat plays back there. The **Ears** panel picks the input (`ears.device` names one; the OS default otherwise) and whether the clip goes along or only its words. The microphone is recorded by Symposium in Python (`sounddevice`), like the screen, so there is no browser permission prompt and every input the OS has is on the list — on Windows that includes "Stereo Mix", which is what the desktop is playing. The words come from whisper through Lodestar (`POST /api/v1/hear`), which wakes it on a node that has it and lets go after; Symposium never talks to the node itself. Comes with `[desktop]`, or `[ears]` on its own. `ears.enabled: false` turns it off.

**Say it** is the other direction. Every answer has a 🔊 under it; press it and the answer is said in the assistant's voice. The words (minus any thinking, code fences and markdown marks) go to Lodestar's speaker (`POST /api/v1/speak`), which wakes the voice on the node that has it and lets go after, and the clip comes back to sit on that answer as a file marked `said`, playing at once and again whenever the chat is opened. A long answer is said up to `speech.max_chars`, cut at a sentence, and says so. `speech.voice` names another voice the speaker has; `speech.enabled: false` takes the button away.

---

## Install

```bash
pip install seren-symposium              # headless: serves its UI to a browser
pip install "seren-symposium[desktop]"   # + a native window (pywebview) and screen share
pip install "seren-symposium[screen]"    # + screen share alone (mss, Pillow), for a browser
pip install "seren-symposium[corp]"      # + OS-trust-store TLS for intercepting proxies
```

Requires Python 3.10+.

**The native window is deliberately an extra.** Without it Symposium still runs and still serves its UI — which is exactly what CI and a headless Jetson want. `pywebview` wraps the OS webview rather than shipping Chromium: ~10 MB instead of Electron's 150 MB+, at the cost of inheriting whatever WebView2/WebKit the machine happens to have.

`[updates]` exists as an empty alias. Update checking is core in `seren-meninges` now, so the extra installs nothing — the name is kept because it appears in the Starwright installers, in docs and in people's notes, and an unknown extra only earns a warning that trains operators to ignore warnings.

**On a corporate network** that intercepts TLS (Zscaler, Netskope and friends): install the `corp` extra and set `tls.trust_system_store: true`.

---

## Run

```bash
seren-symposium                  # a window (needs [desktop]) - onto the tray's Symposium if it is running
seren-symposium --tray           # serve from the tray, no window until you open one
seren-symposium --window         # only a window, onto the Symposium serving
seren-symposium --serve          # no window; open the URL yourself
seren-symposium --config ./seren-symposium.yaml
seren-symposium --port 7500
```

**Always on.** The installer starts it at logon in the tray (`--tray`: a Startup-folder shortcut on Windows, an XDG autostart entry on Linux), and the Start menu shortcut opens a window onto it — close the window and Symposium is still there; **Quit** in the tray menu stops it. At logon and as you, not as a system service: the claude voice needs your claude login and MCP servers, and screen share needs your desktop, and a service has neither.

`--serve` is not a fallback, it is the headless path: it's what CI exercises, what a display-less box runs, and what you want when you need browser devtools pointed at the UI. With `[desktop]` absent, a plain `seren-symposium` says so and serves headless rather than dying.

Defaults to **http://127.0.0.1:7426**.

---

## Configure

`seren-symposium.yaml`, all optional — defaults assume the whole stack on localhost. Resolution order, later wins: **defaults → yaml → `SEREN_SYMPOSIUM_*` env**. A missing file is a valid run.

```yaml
lodestar:
  url: "http://127.0.0.1:6361"
  timeout_seconds: 180    # longer than Lodestar's 120, so its 504 reaches you
  # ONE of these. Same precedence as every other service.
  # bearer_token: ""
  # bearer_token_keyring: "seren-lodestar/alice"
  # bearer_token_env: "SEREN_LODESTAR_BEARER_TOKEN"

# The Backend cards — the OTHER services' own /viewer pages, opened in your browser.
# Blank one out to hide its card; a map makes a group of them.
viewers:
  memory:
    memory:      "http://127.0.0.1:7420/viewer"
    loci:        "http://127.0.0.1:7422/viewer"
    callosum:    "http://127.0.0.1:7423/viewer"
    hippocampus: "http://127.0.0.1:7424/viewer"
  tools:   "http://127.0.0.1:7425/viewer"
  cluster: "http://127.0.0.1:6361/viewer"
  probe:   ""

ui:
  host: 127.0.0.1        # loopback ONLY — there is no inbound auth
  port: 7426
  you: "you"             # your name on your own messages (Settings can change it)

files:
  max_mb: 25             # one attachment's ceiling
  inline_max_chars: 20000  # text a local model is given from a message's files
  fetch_images: true     # web images an answer shows are saved into the chat

screen:
  enabled: true
  max_width: 1600        # wider stills are scaled down

folders:
  - name: "seren stack"
    system_prompt: "You are working on the Seren services. Prefer real bytes over recollection."
    temperature: 0.6
    memory_tags: ["seren", "infra"]
  - name: "general"
    temperature: 0.9

sessions:
  dir: ""                # blank: ./sessions next to this file

claude:                  # the claude voice - see Voices
  name: "claude"
  command: ["claude"]
  cwd: ""
  permission_mode: ""    # every chat's; a project or chat can set its own
  allowed_tools: []      # every chat's; a project or chat adds to it
  add_dirs: []           # folders it may work in besides cwd
  ask: true              # ask in the window about a tool not on the list
  carabiner: ""          # its claude.yaml, for Settings; blank looks for it
  projects_dir: ""       # the CLI's conversations, for pick up…; blank = ~/.claude/projects
  # room_framing: "..."  # what each seat of a room is told; {seats} {me} {you}; blank says nothing
  room_volleys: 6        # turns seats may take by calling on each other (@name) per message; 0 = they can't
  save_files: true       # may write into the chat's own folder, and only there
  handover_max_chars: 100000
  # framing: "..."       # what each turn is told about the window; blank says nothing
  mcp_servers: []        # only these MCP servers (names from ~/.claude.json); empty = every one the CLI knows
  mcp_always_load: []    # of those, the ones whose tools load up front instead of through ToolSearch
  builtin_tools: []      # Claude Code's own tools, cut down to these (ToolSearch always kept); empty = all
  skills: true           # its skills and slash commands; false = none
  claudeai_connectors: false   # the account's claude.ai connectors (Gmail, Drive...); off = not loaded
  auto_memory: last_resort     # Claude Code's own MEMORY.md: last_resort | off | on
  # tools_note: "..."    # what each turn is told about the house's own tools ({servers}); blank says nothing
```

**Avatars.** Every voice can wear a picture, and it is open on purpose: an avatar says nothing about who holds what (that is the kitchen's magnets), it is worn because whoever wears it liked it. The one wearing it sets it in an answer, with an image captioned `avatar`: `![avatar](files/possum-spaceship.svg)` for a file it saved into the chat, `![avatar](https://…)` for one on the web, or the file name a tool gave for a picture it just drew. A seat sets its own; a chat's one Claude sets `default`, which everyone wears who has not picked. You can put any picture on anyone under ⚙ → seats (click a seat's face, or *default avatar…*): a file, a link, or any picture already in the chat, which is how one drawn in the picture panel gets worn. They are kept beside the chats (`avatars/`) by **name**, so a seat keeps its face in every room; a seat with none wears its initial in its colour.

**A chat can have faces of its own.** An avatar has two scopes. `![avatar](…)` in an answer is for **that chat** (kept in the chat's settings as `faces`, pointing at one of its own files, so it goes where the chat goes); `![avatar everywhere](…)` is the one worn in **every chat** that has none of its own. The sheet has the same switch (*this chat* / *every chat*), and the Settings tab's *faces* sets the every-chat ones — the default and yours — with no chat open. A chat shows its own face for someone first, then their every-chat one, then (for a Claude) the default.

**A face and a colour for everyone.** Not only seats: the chat's ⚙ lists its **voices** — you, the chat's one Claude, each local model that has answered there, each plugin that has spoken — and each can have a face (click it: the same sheet, any picture) and a bubble colour (click the dot; *plain* takes it off). A face goes by who it is (`you`, `default`, a model's or plugin's name) and is the same in every chat; a colour is that chat's own setting (`colors`, a hue per voice), so one chat can be loud and the next left alone. Only a Claude falls back to the default face: you are never drawn wearing it.

**The box is the chat's, not the window's.** What you are typing — and the files waiting in the tray — stay with the chat they were typed in: open another chat and its own half-written message is there instead, come back and yours is where you left it. The words outlive a restart. The box's top edge is a grip: drag it up when you are long-winded, double-click for the usual height. And an answer coming does not lock it: **send** becomes **send after**, and what you send waits on a slip above the box until the answer is in. How the waiting ones go is a setting (`queue`): **meld** (the default) sends everything waiting as *one* message, in the order you wrote it, so three quick thoughts get one answer that has seen all three and not three answers each blind to the others; **each** sends them one at a time, an answer apiece; **hold** keeps them until you let them go. Every chat's is in Settings (*sent mid-answer*, kept with your chats; `ui.queue` in the yaml), and a chat can say otherwise under its ⚙. The line itself has a **hold** switch, whatever the setting: a slip was written before you read the answer it follows, so hold the line, read, fix or × a slip, then **send now**. Stopping an answer holds the line too (so does an answer that failed): one stop is one stop, and nothing is fired in its place. A message sent from the box while the line is held goes alone; the held ones stay. It does not reach Claude mid-thought; that is still to come. The line under an answer as it comes counts from when the turn began, wherever you were looking meanwhile.

**The bars are yours to size.** The projects on the left and the panels on the right each fold to a strip, and each has a grip on its inner edge: drag it to make the bar wider or narrower (a screen share worth reading, a long project name), double-click for the usual width. The chat between them always keeps room to read in. Widths, like folds, are this browser's to remember.

**The house's own tools are named.** With `claude.mcp_servers` set, each turn is told after the framing where its own tools are and that they come before a Claude Code built-in that does something like them (`claude.tools_note`, `{servers}` the names). Their descriptions sit behind ToolSearch until asked for, and a voice that has to look its own door up every turn starts forgetting it is there.

**What Claude Code brings of its own is turned down.** The CLI arrives with things this house did not ask for. Its built-in tools can be cut to a list (`builtin_tools` → `--tools`; each seat of the first room carried 35 and used a handful) and its skills and slash commands switched off (`skills: false` → `--disable-slash-commands`); both are left as the CLI has them until the yaml says otherwise. The account's claude.ai connectors are not loaded (`claudeai_connectors: false` starts the CLI with `ENABLE_CLAUDEAI_MCP_SERVERS=false`), so no turn pays for a tool list it never uses. Its auto memory (the `MEMORY.md` it keeps per folder) stays but is a **last resort**: each turn is told, after the framing, that the house's memory tools come first (`auto_memory_note`); `auto_memory: off` starts the CLI without it (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`), `on` leaves it as Claude Code has it. And a server named in `mcp_always_load` has its tools in the list from the start (`alwaysLoad`), instead of being looked up through ToolSearch every time; that costs each turn the server's whole tool list, so it is opt-in.

**What you set in the window stays set.** The yaml is the install's — Starwright writes it whole on every reinstall — so what you change in Settings (your name, Claude's, its permissions and folders, confirm-before-delete) and the projects (made, renamed, deleted, each one's voice and defaults) live in `symposium-prefs.json` beside the sessions folder, laid over the yaml at start. Sparse: until you change a thing there, the yaml's value shows through. The projects are taken over the first time one is changed in the window — the yaml's `folders:` seed them, and from then on that file owns the list.

**Backend** is a card per viewer, in that service's own colour, with a widget: a dot, how fast it answered, and a little sparkline of the last few checks (Lodestar's also says whether a model is serving). The check is a ping from the page — it learns *that* something answered and how fast, never what — so Symposium itself still talks to nobody but Lodestar. A card opens that service's own page **in your browser**, where its sign-in sticks — Symposium doesn't try to be a second copy of it. A blank viewer URL **hides** its card rather than drawing a dead one — someone who doesn't run Probe shouldn't get a Probe card that never answers.

A viewer can also be a **group**: a map of names to URLs, drawn as its own row of cards. Memory is one by default, because memory isn't one service — SerenMemory holds the episodes, Loci the facts, the Corpus Callosum searches across both and the Hippocampus consolidates them. Blank members drop out, an empty group drops away, and a plain URL still works as a single button.

The shell wears the same Meninges baseplate as every sibling's `/viewer` — `render_from_dir` over the bricks in `seren_symposium/ui/` (tabs, body, styles, scripts, head, the Lodestar pill) — in Symposium's olive-sage `#a3b18a`. Projects are drawn as a **krater**, the bowl the whole table drinks from, and chats as a **kylix**, the cup passed between two; filled when open, and for the chat you're in.

Token resolution goes through `seren_meninges.resolve_token` (inline / keyring / env), so a token lives in exactly one place per machine.

---

## API

Three of these reach Lodestar; the session and screen routes touch only this machine. Nothing reaches a leaf service. (The one other outbound call is the image fetch above, made when an answer comes back — not a route.)

| Method | Path | What |
|---|---|---|
| GET | `/` | the shell — Chat, Settings, Backend |
| GET | `/api/config` | what the UI needs to draw itself: viewers, folders, version, updates. **No secrets.** |
| POST | `/api/chat` | proxied to Lodestar `/api/v1/chat`, bearer injected |
| POST | `/api/chat/stream` | proxied to Lodestar `/api/v1/chat/stream`, line for line; with a session the turn runs on its own and this only follows it (409 while that chat is still answering) |
| GET | `/api/avatars` | `{avatars: {name: {type, at}}}` — who wears a picture: seats by name, and `default` |
| GET | `/api/avatars/{name}` | that picture |
| POST | `/api/avatars/{name}` | `{session, file}` — a picture already in that chat; or `{session, url}` — one from the web, fetched into that chat first. `name` wears it from now on |
| DELETE | `/api/avatars/{name}` | take it off |
| POST | `/api/sessions/{id}/remove` | take a chat out of Symposium and delete nothing: its file and attachments are set aside (`removed/` beside the chats), and the Claude conversations it carried are left as they are, free to be picked up again |
| POST | `/api/sessions/{id}/stop` | stop the turn running in a chat; what it had already kept stays (a room: every seat that had come back). 404 when nothing is |
| GET | `/api/sessions/{id}/live` | the turn running in a chat right now, replayed from its first event then followed; 404 when nothing is. `GET /api/sessions` marks such chats `answering` |
| GET | `/api/models` | the models on Lodestar's llama node, as names |
| PATCH | `/api/prefs` | what the window sets: `you`, `claude_name`, `confirm_destructive`, `queue`, `translate`, `claude_mode`, `claude_ask`, `claude_tools`, `claude_dirs` |
| POST, PATCH | `/api/projects` | make a project; change one's settings or rename it (`{name, set?, rename?}` — its chats move along) |
| POST | `/api/projects/remove` | `{name}` — the project goes; its chats stay, out of any project |
| GET | `/api/claude` | the claude voice's settings, the modes, the carabiner on this box and its `session:` block (never its tokens); with `session`, or `folder` and `settings`, what a chat runs with and where each part came from |
| POST | `/api/sessions/{id}/seats` | `{name, cwd?, pickup?, fork?, host?}` — a seat at the chat's table; `pickup` makes a terminal's conversation the seat. The first seat into a chat with a Claude seats that Claude too (`host`, else named from its folder) |
| PATCH | `/api/sessions/{id}/seats` | `{order: [names]}` — who sits where: the order the table goes round |
| PATCH | `/api/sessions/{id}/seats/{name}` | `{name?, hue?}` — rename a seat (what it said goes with it) or give it another colour (a hue, 0–359) |
| DELETE | `/api/sessions/{id}/seats/{name}` | the seat leaves; what it said stays |
| GET | `/api/claude/sessions` | the CLI's conversations on this box begun at a terminal and not yet a chat here, newest first: `{id, cwd, title, updated, said, warm}` |
| POST | `/api/claude/sessions/pickup` | `{id, folder?, fork?}` — a new chat that resumes that conversation, in its folder; `fork` defaults to whether it's warm. 403 for a headless run, 409 if it's already a chat |
| GET | `/api/stack` | what a chat's model is given, layer by layer — `session`, or `folder` and a JSON `settings` (a chat not made yet; a project's own with none). A room has one per seat: `seat` (else the first), and the answer names `seat` and `seats` |
| POST | `/api/permit` | the claude voice's permission tool asking — needs the running turn's ticket |
| POST | `/api/permit/answer` | `{id, answer: once \| always \| deny, rule?, scope?: chat \| project \| all}` — an Always names one of the widths offered (else the narrowest) and where it's kept |
| GET, POST | `/api/sessions` | every session, newest first; POST makes one (with `messages`, an import) |
| GET, PATCH, DELETE | `/api/sessions/{id}` | one session whole (the export); rename, move, settings; delete (with its files) |
| POST | `/api/sessions/{id}/files` | attach a file: `{name, data}`, data base64 — JSON like every POST, so no other page's form can |
| GET | `/api/sessions/{id}/files/{name}` | a file back, never as a page (see above) |
| POST | `/api/sessions/{id}/rewind` | `{to}` — keep the first `to` messages |
| POST | `/api/sessions/{id}/fork` | `{at}` — a new chat holding the first `at` messages |
| DELETE | `/api/sessions/{id}/messages/{i}` | take message `i` out |
| GET | `/api/screen/sources` | the displays (`primary` marks the main one) and, on Windows, the windows that can be shared |
| POST | `/api/screen` | `{monitor?}` or `{window?}` — take a still; GET `/api/screen/latest` reads it |
| GET | `/vendor/...` | the Markdown renderer and highlighter we ship |
| GET | `/health` | ours, plus whether Lodestar answers |

A chat body with `"session": id` takes its thread and settings from that session and writes the answer back to it; `"retry": true` asks its last prompt again; `"files"` names attachments already in the chat; `"screen": true` adds the newest screen-share still. Nothing moves under an answer: rewind, fork, delete and dropping a message wait (409) while Claude is answering in that chat. Without `session`, it's the stateless shape: send your own `history`. The stream is Lodestar's NDJSON unchanged — `chunk`, `tool_status`, `done`, `error` — plus one line of Symposium's own at the end, `{"type": "saved", "session": {...}}`, once the session has the turn. A Claude turn adds its steps (`thinking`, `tool_call`, `tool_result`) and its asks (`permission`, `{id, tool, summary, rule, rules, scopes}`: the widths Always may take and where it may be kept).

`/health` reports the two halves separately and on purpose. Symposium's own health is trivially `ok`; the useful half is the cluster head, and it never fails the request to tell you about it. The UI draws it as a dot, not a paragraph.

**"Reachable" is not "usable", and the difference is the whole point.** Lodestar's `/health` is public — it answers `200` for a client whose bearer is wrong, missing, or meant for another machine. Probing only that gives you a green dot on an app where *every message returns 401*, which is the most confusing state this client can be in. So the second probe is `/api/v1/chat/health`, which sits behind the token **and** knows whether a node is actually serving llama:

| `status` | Dot | Means | Go fix |
|---|---|---|---|
| `ok` | green | ready; the detail names the node | — |
| `no_inference` | amber | authenticated, but no node is serving llama | start llama on a node |
| `unauthorized` | red | Lodestar rejected our token | `lodestar.bearer_token` in your yaml |
| `unreachable` | red | nothing answered | is Lodestar running? |

A rejected token is the one error Symposium **annotates** rather than relays. Lodestar answers `{"detail": "unauthorized"}`, which is correct and useless here — it cannot know the token it rejected came from a yaml file on your machine. Naming the file is knowledge only the client has.

A down Lodestar gets you a **502**, never a 500. The app is fine; the head is not, and the UI has to be able to tell those apart.

---

## Where it sits

| Service | Port |
|---|---|
| SerenLodestar (cluster head) | 6361 |
| SerenMemory | 7420 |
| SerenMargin | 7421 |
| SerenLoci | 7422 |
| SerenCorpusCallosum | 7423 |
| SerenHippocampus | 7424 |
| SerenWorkbench | 7425 |
| **SerenSymposium** | **7426** |
| SerenTheatre | 7427 |
| SerenProbe | 7430 |
| SerenObservatory | 7777 |

---

## What talking to Lodestar looks like

Symposium posts to Lodestar's `POST /api/v1/chat`, whose body is flat — it has never heard of a folder:

```json
{ "prompt": "...", "system_prompt": "...", "history": [{"role":"user","content":"..."}],
  "model_override": "...", "temperature": 0.6, "max_tokens": 1024, "repeat_penalty": 1.1 }
```

…and which answers with `{ response, model, node, tool_rounds, usage }`.

The folder preset is flattened into that shape **in Python**, not in the shell's JavaScript, which is what makes the layer precedence — folder default, then session settings, the two system prompts joined — assertable in pytest. The response is passed back to the UI verbatim: Lodestar's field names are the contract, and renaming them in the proxy would only create a second thing to keep in sync.

Conversation history lives in the session file and rides along on each request, trimmed from the oldest end to `lodestar.history_max_chars` (Lodestar trims only tool output, so bounding ordinary conversation is the client's job). Only `role` and `content` go on the wire; timestamps and receipts stay home. Lodestar is stateless per call and keeps none of it.

---

## Not yet wired

Honesty beats a feature list that lies:

- **`memory_tags` goes nowhere.** Folders accept it and `/api/config` serves it, but Lodestar's chat body has no field for it, so nothing is sent. It's a promise waiting on the cluster head.
- **Temperature, max tokens and repeat penalty don't reach Claude.** The CLI has no flags for them.
- **Closing the window mid-answer loses that answer.** The turn is written when its `done` arrives; a stream cut short never gets one — even with the tray still serving.
- **The Backend cards are four fixed names** (memory, tools, cluster, probe), not a list — Hippocampus rides in the Memory group, but there is no Theatre or Observatory card yet.
- **A local model sees no pictures.** It's told an image was attached; showing it one is for Lodestar and a vision model. Audio and video aren't handled by anyone yet.
- **Screen share is stills.** Polling sends the newest frame with your next message, not a stream; there's no global hotkey for push yet, only the button. Sharing one window is Windows-only.
- **macOS doesn't start it at login yet.** Linux gets an XDG autostart entry, Windows a Startup shortcut; on a Mac, open it from the launcher.
- **A room is slow by design and can't retry yet.** Seats answer one after another, so a round of four takes four turns' time. Retrying a round is refused (rewind and send again). Every seat is a Claude; a local model through Lodestar can't take a seat yet, though a voice that isn't one more of the same head is exactly what a room wants.
- **Seats don't speak unprompted.** A round starts only when you send. Within it seats can call on each other (`claude.room_volleys`), but nothing starts a round on its own, and you can't say something into a round while it runs: stop it, then speak.
- **Warm is a guess.** "Touched in the last ten minutes" stands in for "a terminal still has it open"; a terminal left idle longer than that is taken over rather than forked. Say `fork: true` to be sure.
- **Files stay until the chat goes.** Rewinding past or deleting a message leaves its files in the chat's folder (an edit sends them again); only deleting the chat removes them.

The shell, the proxy, folder presets, sessions, streaming, the claude voice and its permission asks, the steps, edit/rewind/fork, attachments both ways, the image fetch, screen share, the prefs file, the token handling, the cross-site checks and the Backend cards are real and tested.

---

## Testing

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

The suite needs no live stack — an unreachable Lodestar is itself one of the things under test. CI additionally installs **every shape an operator can ask for** (bare, `[corp]`, `[desktop]`, `[screen]`, `[updates]`) and constructs the app **from outside the repo**, so the source tree can't mask a missing dependency and a satisfied-but-stale floor can't hide behind a dev install.

> Symposium needs `seren-meninges` 5.x. An older one installed alongside (`pip` will happily keep a 2.x) is the first thing to check when an import fails.

---

## License

AGPL-3.0-or-later. Part of the [Seren](https://github.com/ChadRoesler) project — a fully self-hosted, local-first AI companion stack built to run gracefully on cheap hardware. The floor is a $250 Jetson, not a data center.

Build for the floor, not the ceiling. Rip it and win.
