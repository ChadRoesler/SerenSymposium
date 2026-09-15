# Security Policy

## Supported versions

| Component | Supported |
|-----------|-----------|
| Latest release tag | ✅ |
| Older tags | ❌ |

Security fixes are applied to the current release only. Pin to the latest tag.

---

## Threat model

SerenSymposium is a **self-hosted desktop client**, and its security story is different from the rest of the family in one important way: **it is the only member that authenticates nobody.**

Every other Seren service binds a port to be connected *to*, and guards it with a bearer. Symposium binds a port to serve *itself* to a webview on the same machine, and connects outward to exactly one place — SerenLodestar. It **holds** a bearer it presents; it never **checks** one.

That makes the loopback bind load-bearing rather than a default worth tuning.

| Surface | Default | Notes |
|---------|---------|-------|
| UI server | `127.0.0.1:7426` | **Loopback only, and there is no inbound auth.** Anything that can reach this port is already executing on the machine. |
| `ui.host` | `127.0.0.1` | Changing this does not "expose the UI" — it creates an unauthenticated service that will proxy chat, with your bearer attached, for anyone who can route to it. Symposium prints a warning at startup if you set it. |
| Bearer token | Not set | The token Symposium **presents to Lodestar**. Resolved via `seren_meninges.resolve_token` — inline, OS keyring, or env var. Prefer keyring or env over inline. |
| `seren-symposium.yaml` | `./seren-symposium.yaml` | May contain an inline bearer. Do not commit it. Prefer `bearer_token_keyring` so no secret is on disk in plaintext. |
| Token exposure to the UI | Never | The bearer stays in Python. `/api/config` is asserted by test to contain no secret, and this is the entire reason the loopback shim exists rather than letting JavaScript call Lodestar directly. |
| Viewer tabs | Blank by default where unknown | Tabs load **other services' pages in an iframe**. A viewer URL is a URL you are choosing to render in your own window — point them only at services you run. |
| Conversation history | In memory only | Never written to disk. It is sent to Lodestar on each request and lost when the app closes. |

---

## Deployment recommendations

- **Normal use**: the defaults. Loopback bind, no inbound auth, bearer in the OS keyring.
- **Remote Lodestar**: set `lodestar.url` to the remote host and put TLS in front of it. Symposium deliberately honours `HTTP_PROXY`/`HTTPS_PROXY` for non-loopback Lodestar URLs and *ignores* them for loopback, so a corporate proxy cannot swallow a call to your own machine.
- **Behind a TLS-intercepting proxy** (Zscaler, Netskope): install the `corp` extra and set `tls.trust_system_store: true` rather than disabling verification.
- **Do not** put Symposium on a shared host and reach it over the network. It is a desktop client; if you need a multi-user surface, the thing you want is a service that authenticates, and that is not this.

---

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Open a [GitHub Security Advisory](https://github.com/ChadRoesler/SerenSymposium/security/advisories/new) (private disclosure). Include:

- A description of the issue and its impact
- Steps to reproduce
- Any relevant config or environment details

You will get a response within **7 days**. If a fix is needed, a patched release will be tagged and the advisory will be published after users have had time to update.
