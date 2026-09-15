# SerenSymposium

**The front door to the Seren constellation.** One window: chat in the first tab, and every sibling service's own operator page in the rest.

Symposium is not "a chat app," and it is emphatically not a ninth service. It is the **desktop shell** the other eight already earned — Memory, Workbench, Lodestar and Probe each serve a perfectly good `/viewer` on the shared Meninges baseplate, and reimplementing any of them inside a client would be building a second copy of something that already works. So Symposium loads them. What it adds is the one surface nobody else owns: a place to actually *talk* to the stack.

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

**Why a local server instead of just JavaScript → Lodestar.** Two reasons, both practical rather than architectural:

1. The bearer stays in Python. It is never serialised into a webview's JavaScript, and a test asserts the token never appears in `/api/config`.
2. Lodestar never needs CORS opened for a desktop app.

The cost is one loopback port. That's the whole trade.

**There is no inbound auth, and that is a design constraint, not an omission.** Symposium binds `127.0.0.1` only — anything that can reach it is already on the machine. Widen `ui.host` past loopback and you have not "exposed the UI," you have built a different, unauthenticated service. Don't. It warns you at startup if you try.

---

## Folders are context boundaries, not filing

The easiest thing to mistake in the config. Folders exist to stop **context drift**, and grouping is the side effect rather than the point.

A session opened in `seren stack` already knows what it is about — it inherits a system prompt, a model, a temperature, a set of memory tags. One opened in `general` inherits none of it and doesn't drag the last three hours of infrastructure talk along with it.

Four layers, split by *how often you touch a thing* rather than by what it is:

| Layer | Holds | Where you change it |
|---|---|---|
| per-install | lodestar url, ui port, confirm_destructive | `seren-symposium.yaml` |
| per-folder | system prompt, model, temperature, memory tags | folder preset |
| per-session | overrides of the above | header chip |
| per-message | the message | the composer |

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
  timeout_seconds: 120
  # ONE of these. Same precedence as every other service.
  # bearer_token: ""
  # bearer_token_keyring: "seren-lodestar/chad"
  # bearer_token_env: "SEREN_LODESTAR_BEARER_TOKEN"

# The read-only tabs — the OTHER services' own /viewer pages.
# Blank one out to hide that tab.
viewers:
  memory:  "http://127.0.0.1:7420/viewer"
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
```

A blank viewer URL **hides** that tab rather than rendering a dead one — someone who doesn't run Probe shouldn't get a Probe tab that spins.

Token resolution goes through `seren_meninges.resolve_token` (inline / keyring / env), so a token lives in exactly one place per machine.

---

## API

Four routes. That's the point.

| Method | Path | What |
|---|---|---|
| GET | `/` | the shell — chat plus tabs pointing at sibling viewers |
| GET | `/api/config` | what the UI needs to draw itself: viewers, folders, version, updates. **No secrets.** |
| POST | `/api/chat` | proxied to Lodestar `/api/v1/chat`, bearer injected |
| GET | `/health` | ours, plus whether Lodestar answers |

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
| SerenWorkbench | 7425 |
| **SerenSymposium** | **7426** |
| SerenProbe | 7430 |
| SerenObservatory | 7777 |

---

## What talking to Lodestar looks like

Symposium posts to Lodestar's `POST /api/v1/chat`, whose body is flat — it has never heard of a folder:

```json
{ "prompt": "...", "system_prompt": "...", "history": [{"role":"user","content":"..."}],
  "model_override": "...", "temperature": 0.6 }
```

…and which answers with `{ response, model, node, tool_rounds, usage }`.

The folder preset is flattened into that shape **in Python**, not in the shell's JavaScript, which is what makes the layer precedence — folder default, then per-session override — assertable in pytest. The response is passed back to the UI verbatim: Lodestar's field names are the contract, and renaming them in the proxy would only create a second thing to keep in sync.

Conversation history lives in the shell, one thread per folder, and rides along on each request. Lodestar is stateless per call and keeps none of it. A failed turn is **not** recorded, so an error never poisons the context that every later message replays.

---

## Not yet wired

Honesty beats a feature list that lies:

- **Each viewer tab asks for its own token.** The tabs load other services' `/viewer` pages, and the Meninges viewer baseplate keeps its bearer in `localStorage` scoped to the **serving** origin — `127.0.0.1:6361` for the cluster tab, not Symposium's `127.0.0.1:7426`. Symposium is already holding the Lodestar token and still cannot hand it over: a page on one port cannot write another port's `localStorage`. So you click 🔑 once per service, and it sticks. Fixing this properly means teaching the baseplate to accept a token from its parent frame, which is a change to *Meninges* and lands on all nine services at once. (Services with no bearer set — the trusted-LAN default — just work.)
- **`memory_tags` goes nowhere.** Folders accept it and `/api/config` serves it, but Lodestar's chat body has no field for it, so nothing is sent. It's a promise waiting on the cluster head.
- **No streaming.** Lodestar serves `/api/v1/chat/stream`; Symposium uses the blocking endpoint, so long answers arrive all at once.
- **Sessions aren't persisted.** History is per-folder and in-memory — "new session" clears the active folder, and restarting the app clears everything.
- **No per-session chip yet.** The proxy honours an explicit `system_prompt` / `model_override` / `temperature` on the request and prefers it over the folder default; the UI just has no control that sends one.
- **`confirm_destructive` is plumbed but unused.** It reaches the shell in `/api/config` and nothing consults it.

The shell, the proxy, folder presets, multi-turn history, the token handling and the tab loading are real and tested.

---

## Testing

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

The suite needs no live stack — an unreachable Lodestar is itself one of the things under test. CI additionally installs **every shape an operator can ask for** (bare, `[corp]`, `[desktop]`, `[updates]`) and constructs the app **from outside the repo**, so the source tree can't mask a missing dependency and a satisfied-but-stale floor can't hide behind a dev install.

> If `/api/config` raises `ModuleNotFoundError: seren_meninges.updates` locally, your installed `seren-meninges` is below the `>=2.2.0` floor. Update checking moved into meninges core; an old copy has no `updates` module.

---

## License

GPL-3.0-or-later. Part of the [Seren](https://github.com/ChadRoesler) project — a fully self-hosted, local-first AI companion stack built to run gracefully on cheap hardware. The floor is a $250 Jetson, not a data center.

Build for the floor, not the ceiling. Rip it and win.
