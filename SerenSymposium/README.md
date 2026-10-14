# SerenSymposium

**The front door to the Seren constellation.** One window: chat in the first tab, and every sibling service's own operator page in the rest.

Symposium is not "a chat app," and it is emphatically not another service. It is the **desktop shell** the rest of the family already earned — Memory, Workbench, Lodestar and Probe each serve a perfectly good `/viewer` on the shared Meninges baseplate, and reimplementing any of them inside a client would be building a second copy of something that already works. So Symposium loads them. What it adds is the one surface nobody else owns: a place to actually *talk* to the stack.

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

**The one other thing it runs is local: the claude voice.** A chat can be answered by Claude instead of a local model, and Claude lives in the `claude` CLI on your machine — with its own login, MCP servers and hooks. Symposium runs that program as a subprocess; it still opens no network connection to anything but Lodestar. See [Voices](#voices-who-answers).

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

Only a turn that came back is written. A failed one is shown in red, its prompt goes back in the composer, and it never becomes context that later messages replay. **Retry** asks the last prompt again; the answer it replaces stays on disk until the new one is in.

Answers stream as they're written. When the model reaches for a tool you see `calling recall…` while it runs, and under every answer a receipt: the model, the node, the tools it used, and how long it took.

---

## Voices: who answers

Every chat has a **voice**, layered like any other setting (the chat's, else its project's, else `lodestar`):

| Voice | Who answers | How |
|---|---|---|
| `lodestar` | a local model | the chat goes to Lodestar, which routes it to a node — as above |
| `claude` | Claude | the `claude` CLI on this machine, with its own login, MCP servers and hooks |

A chat's first Claude turn starts a CLI conversation; every later turn **resumes** it (`--resume`), so the CLI keeps the thread and no history goes on the wire. Switch a chat's voice and back and nothing is lost: the turns the other voice answered are handed to Claude, once, in front of the next prompt — and a chat that started with a local model hands over everything before it.

What runs is the yaml's (`claude:` — the program, its working directory, the tools it may use without asking); a request supplies only the prompt, on stdin. `-p` has nobody to answer a permission prompt, so a tool that would ask is refused unless `allowed_tools` lists it. One Claude turn at a time per chat — two would both resume the same conversation. The stream is translated into Lodestar's shape (`chunk`, `tool_status`, `done`, `error`), so the shell reads one stream whoever is answering; the receipt names the Claude model and the voice's `name`.

---

## Install

```bash
pip install seren-symposium              # headless: serves its UI to a browser
pip install "seren-symposium[desktop]"   # + a native window (pywebview)
pip install "seren-symposium[corp]"      # + OS-trust-store TLS for intercepting proxies
```

Requires Python 3.10+.

**The native window is deliberately an extra.** Without it Symposium still runs and still serves its UI — which is exactly what CI and a headless Jetson want. `pywebview` wraps the OS webview rather than shipping Chromium: ~10 MB instead of Electron's 150 MB+, at the cost of inheriting whatever WebView2/WebKit the machine happens to have.

`[updates]` exists as an empty alias. Update checking is core in `seren-meninges` now, so the extra installs nothing — the name is kept because it appears in the Starwright installers, in docs and in people's notes, and an unknown extra only earns a warning that trains operators to ignore warnings.

**On a corporate network** that intercepts TLS (Zscaler, Netskope and friends): install the `corp` extra and set `tls.trust_system_store: true`.

---

## Run

```bash
seren-symposium                  # native window (needs [desktop])
seren-symposium --serve          # no window; open the URL yourself
seren-symposium --config ./seren-symposium.yaml
seren-symposium --port 7500
```

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

# The read-only tabs — the OTHER services' own /viewer pages.
# Blank one out to hide that tab; a map makes a group (one tab, its own row).
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
```

A blank viewer URL **hides** that tab rather than rendering a dead one — someone who doesn't run Probe shouldn't get a Probe tab that spins.

A viewer can also be a **group**: a map of names to URLs, drawn as one tab with a second row naming its members. Memory is one by default, because memory isn't one service — SerenMemory holds the episodes, Loci the facts, the Corpus Callosum searches across both and the Hippocampus consolidates them, and the Memory tab shows all four. Blank members drop out, an empty group drops its tab, and a plain URL still works as a single tab. The member you had open last is remembered.

The shell itself wears the same Meninges baseplate as every sibling's `/viewer` — `render_from_dir` over the bricks in `seren_symposium/ui/` (tabs, body, styles, scripts, the Lodestar pill) — in Symposium's olive-sage `#a3b18a`.

Token resolution goes through `seren_meninges.resolve_token` (inline / keyring / env), so a token lives in exactly one place per machine.

---

## API

Three of these reach Lodestar; the session routes touch only files on this machine. Nothing reaches a leaf service.

| Method | Path | What |
|---|---|---|
| GET | `/` | the shell — chat plus tabs pointing at sibling viewers |
| GET | `/api/config` | what the UI needs to draw itself: viewers, folders, version, updates. **No secrets.** |
| POST | `/api/chat` | proxied to Lodestar `/api/v1/chat`, bearer injected |
| POST | `/api/chat/stream` | proxied to Lodestar `/api/v1/chat/stream`, line for line |
| GET | `/api/models` | the models on Lodestar's llama node, as names |
| GET, POST | `/api/sessions` | every session, newest first; POST makes one (with `messages`, an import) |
| GET, PATCH, DELETE | `/api/sessions/{id}` | one session whole (the export); rename, move, settings; delete |
| GET | `/health` | ours, plus whether Lodestar answers |

A chat body with `"session": id` takes its thread and settings from that session and writes the answer back to it; `"retry": true` asks its last prompt again. Without `session`, it's the stateless shape: send your own `history`. The stream is Lodestar's NDJSON unchanged — `chunk`, `tool_status`, `done`, `error` — plus one line of Symposium's own at the end, `{"type": "saved", "session": {...}}`, once the session has the turn.

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

- **Each viewer tab asks for its own token.** The tabs load other services' `/viewer` pages, and the Meninges viewer baseplate keeps its bearer in `localStorage` scoped to the **serving** origin — `127.0.0.1:6361` for the cluster tab, not Symposium's `127.0.0.1:7426`. Symposium is already holding the Lodestar token and still cannot hand it over: a page on one port cannot write another port's `localStorage`. So you click 🔑 once per service, and it sticks. Fixing this properly means teaching the baseplate to accept a token from its parent frame, which is a change to *Meninges* and lands on all nine services at once. (Services with no bearer set — the trusted-LAN default — just work.)
- **`memory_tags` goes nowhere.** Folders accept it and `/api/config` serves it, but Lodestar's chat body has no field for it, so nothing is sent. It's a promise waiting on the cluster head.
- **Retry in a Claude chat asks again in the same conversation.** The CLI has no undo, so Claude has seen the answer being retried; Symposium's own copy drops it as usual.
- **Projects made in the shell have no preset.** `+ project` makes a name to file chats under; voice, system prompt and the rest per project still come from the yaml. A chat's own settings work anywhere.
- **Temperature, max tokens and repeat penalty don't reach Claude.** The CLI has no flags for them.
- **Closing the window mid-answer loses that answer.** The turn is written when Lodestar's `done` arrives; a stream cut short never gets one.
- **The viewer tabs are four fixed names** (memory, tools, cluster, probe), not a list — Hippocampus rides in the Memory group, but there is no Theatre or Observatory tab yet.
- **A framed viewer brings its own header.** Each sibling's page is the whole baseplate, so a tab shows its banner under Symposium's. An embedded mode in the baseplate would fold it away; that's a Meninges change.

The shell, the proxy, folder presets, sessions, streaming, the claude voice, the token handling, the cross-site checks and the tab loading are real and tested.

---

## Testing

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

The suite needs no live stack — an unreachable Lodestar is itself one of the things under test. CI additionally installs **every shape an operator can ask for** (bare, `[corp]`, `[desktop]`, `[updates]`) and constructs the app **from outside the repo**, so the source tree can't mask a missing dependency and a satisfied-but-stale floor can't hide behind a dev install.

> Symposium needs `seren-meninges` 5.x. An older one installed alongside (`pip` will happily keep a 2.x) is the first thing to check when an import fails.

---

## License

AGPL-3.0-or-later. Part of the [Seren](https://github.com/ChadRoesler) project — a fully self-hosted, local-first AI companion stack built to run gracefully on cheap hardware. The floor is a $250 Jetson, not a data center.

Build for the floor, not the ceiling. Rip it and win.
