# SerenSymposium

**The front door to the Seren constellation.** One window, three tabs: **Chat**, **Settings**, and **Backend** — a card for every sibling service, with a widget saying whether it's there, that opens its own operator page in your browser.

Symposium is not "a chat app," and it is emphatically not another service. It is the **desktop shell** the rest of the family already earned — Memory, Workbench, Lodestar and Probe each serve a perfectly good `/viewer` on the shared Meninges baseplate, and reimplementing any of them inside a client would be building a second copy of something that already works. So Symposium points at them. What it adds is the one surface nobody else owns: a place to actually *talk* to the stack.

```bash
pip install "seren-symposium[desktop]"
seren-symposium
```

---

## The shape of it

**A client, not a service.** Every other member of the family binds a port to be connected *to*. Symposium binds one to serve *itself* and connects outward to exactly one place: [SerenLodestar](https://github.com/ChadRoesler/SerenLodestar), the cluster head. It authenticates nobody — it holds a bearer it *presents*, and never checks one.

**One endpoint, enforced by a test.** Symposium does not know what a tool is, does not route, does not touch MCP, and never reaches a leaf service directly. Lodestar owns all of that. Since that kind of erosion is gradual and easy to miss in review, `test_symposium_exposes_no_route_that_bypasses_lodestar` fails the build rather than trusting the convention.

**Two voices.** A chat is answered by a local model through Lodestar, or — with `voice: claude` — by the `claude` CLI on your machine, with its own login, MCP servers and hooks. Each chat is one Claude conversation, resumed turn after turn; switch voices mid-chat and Claude is handed what it missed.

**A loopback shim, not a web server.** Serving the UI from Python means the bearer never reaches JavaScript and Lodestar never needs CORS opened for a desktop app. The cost is one loopback port — and no inbound auth, which is why it binds `127.0.0.1` only. Widen `ui.host` past loopback and you haven't exposed the UI, you've built a different, unauthenticated service. And because loopback keeps out the LAN but not the other tabs in your browser, every request must come from Symposium's own page: a loopback `Host`, our own `Origin`, and JSON.

**Chats are kept.** Each conversation is a JSON file next to your config — renamed, moved between projects (drag it), exported, imported — with its files in a folder beside it. Answers stream in as Markdown (code blocks highlighted), with how they got there folded above the words — the thinking, each tool and what it returned — and a receipt underneath: model, node, tools, seconds. Any message can be copied, edited, rewound to, forked into a new chat, or deleted.

**Asked, not refused.** `claude -p` has nobody at a terminal to say yes, so a tool not on its list used to just fail. Symposium hands the CLI a permission tool of its own instead: the ask shows in the chat — the command, the file — with **Allow once**, **Always** (no wider than that folder, that command, that site) and **Deny**.

**Files both ways.** Drop or paste them in — memes included — and Claude opens them; a local model gets the text of text files. Answers bring pictures back: one from the web is saved into the chat, and Claude can draw one (an SVG) straight into it. Screen share sends stills of a display or one window — previewed first, kept in a panel of their own rather than the conversation — every few seconds with your next message, or when you press send.

**What you set stays set.** Your name, Claude's, its permissions, the projects — changed in the window, they're kept beside your chats, not in the yaml an installer rewrites.

**Always on.** It starts when you log in and sits in the tray; the window is something you open onto it and close. At logon, as you — not a system service, because the Claude voice needs your login and screen share needs your desktop.

**Projects are context boundaries, not filing.** A chat opened in `seren stack` inherits a system prompt, model and temperature and already knows what it's about; one opened in `general` drags none of it along. Grouping is the side effect, not the point.

---

## Run it

```bash
seren-symposium                  # a window (needs the [desktop] extra) - onto the tray's, if it is running
seren-symposium --tray           # serve from the tray; what starts at logon
seren-symposium --serve          # headless; open the URL yourself
seren-symposium --port 7500
```

Defaults to **http://127.0.0.1:7426**. `--serve` is the headless path, not a fallback — it's what CI exercises and what a display-less Jetson runs. The native window is an extra on purpose: `pywebview` wraps the OS webview instead of shipping Chromium, ~10 MB rather than Electron's 150 MB+.

Configuration is optional and assumes the whole stack on localhost. See [`SerenSymposium/README.md`](SerenSymposium/README.md) for the full config reference, the API table and the port map, or copy [`seren-symposium.yaml.sample`](seren-symposium.yaml.sample) and edit.

---

## Where it sits

| Service | Port |
|---|---|
| SerenLodestar (cluster head) | 6361 |
| SerenMemory | 7420 |
| SerenLoci | 7422 |
| SerenHippocampus | 7424 |
| SerenWorkbench | 7425 |
| **SerenSymposium** | **7426** |
| SerenTheatre | 7427 |
| SerenProbe | 7430 |

Symposium connects to **Lodestar only** (and fetches the web images answers show, which `files.fetch_images: false` turns off). The other rows are there because their `/viewer` pages are what its Backend cards open — in your browser, where each keeps its own sign-in. Blank a viewer URL in the config and that card disappears rather than sitting dead.

---

## Develop

```bash
cd SerenSymposium
pip install -e ".[dev]"
python -m pytest tests/ -v
```

CI runs the rest of the matrix (`.[dev,desktop]`, `.[dev,corp]`, and the bare install with the app constructed from outside the repo), which is where a stale dependency floor shows itself.

The suite needs no live stack — an unreachable Lodestar is itself one of the things under test.

---

## License

AGPL-3.0-or-later. Part of the [Seren](https://github.com/ChadRoesler) project — a fully self-hosted, local-first AI companion stack built to run gracefully on cheap hardware. The floor is a $250 Jetson, not a data center.

Build for the floor, not the ceiling. Rip it and win.
