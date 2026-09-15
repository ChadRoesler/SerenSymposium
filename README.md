# SerenSymposium

**The front door to the Seren constellation.** One window: chat in the first tab, and every sibling service's own operator page in the rest.

Symposium is not "a chat app," and it is emphatically not a ninth service. It is the **desktop shell** the other eight already earned — Memory, Workbench, Lodestar and Probe each serve a perfectly good `/viewer` on the shared Meninges baseplate, and reimplementing any of them inside a client would be building a second copy of something that already works. So Symposium loads them. What it adds is the one surface nobody else owns: a place to actually *talk* to the stack.

```bash
pip install "seren-symposium[desktop]"
seren-symposium
```

---

## The shape of it

**A client, not a service.** Every other member of the family binds a port to be connected *to*. Symposium binds one to serve *itself* and connects outward to exactly one place: [SerenLodestar](https://github.com/ChadRoesler/SerenLodestar), the cluster head. It authenticates nobody — it holds a bearer it *presents*, and never checks one.

**One endpoint, enforced by a test.** Symposium does not know what a tool is, does not route, does not touch MCP, and never reaches a leaf service directly. Lodestar owns all of that. Since that kind of erosion is gradual and easy to miss in review, `test_symposium_exposes_no_route_that_bypasses_lodestar` fails the build rather than trusting the convention.

**A loopback shim, not a web server.** Serving the UI from Python means the bearer never reaches JavaScript and Lodestar never needs CORS opened for a desktop app. The cost is one loopback port — and no inbound auth, which is why it binds `127.0.0.1` only. Widen `ui.host` past loopback and you haven't exposed the UI, you've built a different, unauthenticated service.

**Folders are context boundaries, not filing.** A session opened in `seren stack` inherits a system prompt, model and temperature and already knows what it's about; one opened in `general` drags none of it along. Grouping is the side effect, not the point.

---

## Run it

```bash
seren-symposium                  # native window (needs the [desktop] extra)
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
| SerenWorkbench | 7425 |
| **SerenSymposium** | **7426** |
| SerenProbe | 7430 |

Symposium connects to **Lodestar only**. The other rows are there because their `/viewer` pages become its tabs — blank a viewer URL in the config and that tab disappears rather than rendering dead.

---

## Develop

```bash
cd SerenSymposium
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Or mirror the CI matrix in throwaway venvs, which is the only way a stale dependency floor actually shows itself:

```bash
make test          # .[dev]
make test-desktop  # .[dev,desktop]
make test-corp     # .[dev,corp]
make shapes        # bare install, app constructed from OUTSIDE the repo
```

The suite needs no live stack — an unreachable Lodestar is itself one of the things under test.

---

## License

GPL-3.0-or-later. Part of the [Seren](https://github.com/ChadRoesler) project — a fully self-hosted, local-first AI companion stack built to run gracefully on cheap hardware. The floor is a $250 Jetson, not a data center.

Build for the floor, not the ceiling. Rip it and win.
