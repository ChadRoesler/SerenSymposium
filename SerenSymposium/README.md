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
| per-session | overrides of the above | the session's **settings** |
| per-message | the message | the composer |

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

**Asked, not refused.** `-p` has nobody at a terminal to answer "may I?", so a tool not on `allowed_tools` used to be refused outright. With `claude.ask` (on by default) the CLI is given a permission tool of Symposium's own instead (`--permission-prompt-tool`): `seren_symposium.permit`, a small stdio MCP server the CLI starts for the turn. It asks Symposium over loopback, the ask shows in the chat — the command, the file, the address — and waits on **Allow once**, **Always**, or **Deny**. *Always* is no wider than the thing asked about — the folder a file is in, the first word of a command, a web site — and lands on the voice's list for every turn after. Where to ask and the turn's one-time ticket ride in the CLI's environment, never its command line, and an ask without a running turn's ticket is refused.

**How it got there.** Above an answer, folded away: Claude's thinking, each tool it used with what it was given and what came back, and each time it asked you — live while it works, kept in the answer's receipt after. A local model's `<think>…</think>` folds the same way.

**Settings → Claude** shows the voice's permission mode, whether it asks, what it may use without asking and the folders it may work in besides its own — and, beside them, the **carabiner** clipped on this box (SerenCarabiners' `claude.yaml`, found through `claude.carabiner`, `KBH_CONFIG`, `<cwd>/kbh` or `~/.seren/kbh`): where its sessions work and which services it routes to. Read for show only; its tokens never leave the file.

**The CLI can't unsay anything**, so a change to messages its conversation has seen — an edit, a rewind, a deleted message, a retry — doesn't pretend otherwise: the next turn starts a *new* CLI conversation, handed the chat as it now stands (up to `claude.handover_max_chars`). A fork, not an undo. Changes past what it saw leave its conversation alone.

Each turn is also told where it's answering (`claude.framing`, after the chat's own system prompt): that replies render as Markdown, where the chat's files are, and how to show an image.

---

## Files, images and screen share

**Attaching.** Drop files on the chat, paste an image into the composer, or click the tray above it. They wait in the tray — × takes one out, **clear** empties it — and go up when you send; a message can be files alone. Each chat keeps its files in its own folder beside the session file (`<sessions>/<id>.files/`), and the **Files** panel on the right lists everything the chat has carried, newest first — who posted it, its name, when, and a thumbnail — searchable, as a list or a grid of thumbnails, and never more than half the rail tall. Pictures open large on a click. Deleting the chat deletes its folder; an edit or a deleted message never does, so the file is still there to send again. One file is at most `files.max_mb` (25 MB).

**What each voice gets.** Claude opens files itself: it is given each file's path, the chat's folder is added to what the CLI may read (`--add-dir`), and it reads a meme the way it reads anything. A local model can't open a path, so a text file's contents go into the prompt (up to `files.inline_max_chars`, cut with a note past that), and an image or other binary is named so the model knows something was there. Showing pictures to a model that can see is Lodestar's job, and a later one.

**Images coming back.** An answer shows a picture as Markdown. One from the web — `![caption](https://...)` — is fetched when the answer comes back, kept in the chat, and the answer pointed at the copy; only real image bytes are kept, whatever the server claimed. And Claude can **make** one: with `claude.save_files` (on by default) each turn may write into that chat's folder — and only there, past whatever `permission_mode` and `allowed_tools` already allow — so an SVG it draws, or notes, or code it saves, lands in the chat and shows in the Files panel. Anything an answer links that isn't in the chat stays a link; the window never loads a picture from the internet on its own.

Files come back to the window from Symposium's own address — the one that can drive the API — so nothing is ever served as a page: pictures as pictures (an SVG's script never runs in an `<img>`), text as plain text, everything else as a download, all with `nosniff` and a sandboxing CSP.

**Screen share** takes stills of a display — the main one unless you pick another — or, on Windows, of one window (drawn by the window itself, so one half-covered still comes out whole), the old SerenChat way, in two modes:

- **polling** — a fresh frame every few seconds, previewed in the panel; the newest one sits in the tray as **screen · …** and rides along with your next message, once (× skips it).
- **push** — **take one** shows you the frame; **send this** sends it, with whatever's in the composer.

Nothing is shared by looking: the preview is only ever on your side until you send. A frame that went is kept in the chat but marked, so it shows as a small chip in its message and in the panel's own **shared in this chat** strip — not in the conversation, and not in Files (a box there brings them in).

Whoever answers sees a turn, not a feed — the frame that came with the message — so stills are everything an answer can use, at a fraction of video's bytes. They're taken by Symposium in Python (`mss`, scaled by Pillow to `screen.max_width`), not by the webview, so there's no permission prompt per frame. Both come with the `[desktop]` extra, or `[screen]` on its own; without them the panel says what to install. `screen.enabled: false` turns it off.

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
  allowed_tools: []
  add_dirs: []           # folders it may work in besides cwd
  ask: true              # ask in the window about a tool not on the list
  carabiner: ""          # its claude.yaml, for Settings; blank looks for it
  save_files: true       # may write into the chat's own folder, and only there
  handover_max_chars: 100000
  # framing: "..."       # what each turn is told about the window; blank says nothing
```

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
| GET | `/api/sessions/{id}/live` | the turn running in a chat right now, replayed from its first event then followed; 404 when nothing is. `GET /api/sessions` marks such chats `answering` |
| GET | `/api/models` | the models on Lodestar's llama node, as names |
| PATCH | `/api/prefs` | what the window sets: `you`, `claude_name`, `confirm_destructive`, `claude_mode`, `claude_ask`, `claude_tools`, `claude_dirs` |
| POST, PATCH | `/api/projects` | make a project; change one's settings or rename it (`{name, set?, rename?}` — its chats move along) |
| POST | `/api/projects/remove` | `{name}` — the project goes; its chats stay, out of any project |
| GET | `/api/claude` | the claude voice's settings and the carabiner on this box (never its tokens) |
| POST | `/api/permit` | the claude voice's permission tool asking — needs the running turn's ticket |
| POST | `/api/permit/answer` | `{id, answer: once \| always \| deny}` |
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

A chat body with `"session": id` takes its thread and settings from that session and writes the answer back to it; `"retry": true` asks its last prompt again; `"files"` names attachments already in the chat; `"screen": true` adds the newest screen-share still. Nothing moves under an answer: rewind, fork, delete and dropping a message wait (409) while Claude is answering in that chat. Without `session`, it's the stateless shape: send your own `history`. The stream is Lodestar's NDJSON unchanged — `chunk`, `tool_status`, `done`, `error` — plus one line of Symposium's own at the end, `{"type": "saved", "session": {...}}`, once the session has the turn. A Claude turn adds its steps (`thinking`, `tool_call`, `tool_result`) and its asks (`permission`, `{id, tool, summary, rule}`).

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

The folder preset is flattened into that shape **in Python**, not in the shell's JavaScript, which is what makes the layer precedence — folder default, then session settings — assertable in pytest. The response is passed back to the UI verbatim: Lodestar's field names are the contract, and renaming them in the proxy would only create a second thing to keep in sync.

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
