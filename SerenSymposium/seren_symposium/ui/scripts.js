// Symposium's tab logic, dropped into the Meninges baseplate's <script>. The
// baseplate's own api() attaches a saved bearer; this page has none to send,
// so it talks to its shim through call() instead.
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// Everything goes out as JSON: the shim refuses any POST that isn't, which is
// what stops another web page on this machine from driving it.
async function call(method, path, body) {
  const r = await fetch(path, { method, headers: { "content-type": "application/json" },
                                body: body === undefined ? undefined : JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || `HTTP ${r.status}`), { body: j });
  return j;
}

const SETTINGS = ["voice", "system_prompt", "model_override", "temperature", "max_tokens", "repeat_penalty"];
const NUMBER = new Set(["temperature", "max_tokens", "repeat_penalty"]);
// A chat's settings sit on top of its project's; the project calls one of
// them by a shorter name.
const PRESET = { model_override: "model" };

// Projects are the yaml's folders; a chat is a session in one. The server
// still says folder and session - those are the wire names.
let CFG = null, tab = "chat", folder = "", SESSIONS = [], current = null, busy = false;
// Projects made here that have no chat yet, and which projects are folded.
// Only this browser remembers either; a project is kept by its first chat.
const local = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } };
const keep = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} };
let NEW_PROJECTS = local("symposium.newProjects", []), FOLDED = new Set(local("symposium.folded", []));
// Which member of each group tab (Memory) was open last.
let SUB = local("symposium.subtab", {});

const voiceName = v => v === "claude" ? (CFG.claude?.name || "claude") : "local model";

async function boot() {
  CFG = await (await fetch("/api/config")).json();
  folder = (CFG.folders[0] || {}).name || "";
  drawVoices(); drawTabs(); await refresh(); drawLog(); poll(); setInterval(poll, 15000);
}

function drawVoices() {
  const claude = CFG.claude || {};
  $("#s-voice").innerHTML = `<option value="">project's</option>`
    + `<option value="lodestar">local model (lodestar)</option>`
    + `<option value="claude"${claude.available ? "" : " disabled"}>${esc(voiceName("claude"))}`
    + `${claude.available ? "" : " (not on this machine)"}</option>`;
}

async function refresh() {
  SESSIONS = (await call("GET", "/api/sessions")).sessions;
  drawSide();
}

function drawTabs() {
  const names = ["chat", ...Object.keys(CFG.viewers)];
  $("#tabbar").innerHTML = names.map(n =>
    `<button class="tab${n === tab ? " active" : ""}" data-tab="${esc(n)}">${esc(n)}</button>`).join("");
}

// Tabs and the sidebar are delegated from a container that is never replaced,
// so switching away from chat and back cannot strand a dead listener - or, as
// it used to, tear the chat pane out of the DOM with no way to rebuild it.
$("#tabbar").onclick = e => { const t = e.target.closest("[data-tab]"); if (t) show(t.dataset.tab); };
$("#subtabs").onclick = e => { const b = e.target.closest("[data-sub]"); if (b) { SUB[tab] = b.dataset.sub; keep("symposium.subtab", SUB); show(tab); } };

// Sibling viewers are LOADED, not reimplemented - SerenMemory, Workbench and
// Lodestar already serve these pages on the same Meninges baseplate. A group
// (Memory: memory, loci, callosum, hippocampus) is one tab with a row of its
// own; the frame loads whichever member is picked.
function show(t) {
  tab = t; drawTabs();
  const viewing = t !== "chat";
  $("#chat").classList.toggle("active", !viewing);
  $("#viewer").classList.toggle("active", viewing);
  if (!viewing) { $("#frame").removeAttribute("src"); return; }
  let url = CFG.viewers[t];
  const group = typeof url === "object" ? url : null;
  $("#subtabs").hidden = !group;
  if (group) {
    const names = Object.keys(group);
    const pick = names.includes(SUB[t]) ? SUB[t] : names[0];
    $("#subtabs").innerHTML = names.map(n =>
      `<button class="${n === pick ? "active" : ""}" data-sub="${esc(n)}">${esc(n)}</button>`).join("");
    url = group[pick];
  }
  if ($("#frame").getAttribute("src") !== url) $("#frame").src = url;
}

// Projects from the yaml, then any a chat still names after the yaml
// stopped listing it - a renamed project must not make its chats vanish -
// then ones made here that have no chat yet.
function folderNames() {
  const names = CFG.folders.map(f => f.name);
  for (const s of SESSIONS) if (!names.includes(s.folder)) names.push(s.folder);
  NEW_PROJECTS = NEW_PROJECTS.filter(n => !names.includes(n));
  keep("symposium.newProjects", NEW_PROJECTS);
  names.push(...NEW_PROJECTS);
  return names.length ? names : [""];
}

const presetFor = name => CFG.folders.find(f => f.name === name) || {};

function drawSide() {
  $("#side").innerHTML = folderNames().map(name => {
    const f = presetFor(name);
    const bits = [f.voice === "claude" ? voiceName("claude") : null, f.model,
                  f.temperature != null ? `t=${f.temperature}` : null].filter(Boolean).join(" · ");
    const chats = SESSIONS.filter(s => s.folder === name);
    const open = !FOLDED.has(name) || (current && current.folder === name);
    const rows = open ? chats.map(s =>
      `<div class="sess" data-s="${esc(s.id)}" aria-current="${current && s.id === current.id}"
            title="${esc(s.title || "untitled")} · ${s.count} messages">${esc(s.title || "untitled")}</div>`) : [];
    return `<div class="folder"><span class="name" data-fold="${esc(name)}" title="${open ? "fold" : "unfold"}">`
         + `<span>${open ? "🗁" : "🗀"}</span><span>${esc(name || "chats")}</span></span>`
         + (bits ? `<span class="hint">${esc(bits)}</span>` : "")
         + `<button data-new="${esc(name)}" title="new chat in ${esc(name || "chats")}">+</button></div>`
         + (open ? (rows.join("") || `<div class="sess empty">nothing yet</div>`) : "");
  }).join("") + `<div class="foot"><button class="ghost" data-project="1">+ project</button>`
              + `<button class="ghost" data-import="1">import…</button></div>`;
  $("#move").innerHTML = folderNames().map(n =>
    `<option value="${esc(n)}">${esc(n || "chats")}</option>`).join("");
  if (current) $("#move").value = current.folder;
}

$("#side").onclick = async e => {
  const row = e.target.closest("[data-s]");
  if (row) return openSession(row.dataset.s);
  const add = e.target.closest("[data-new]");
  if (add) { folder = add.dataset.new; current = null; drawSide(); drawLog(); $("#input").focus(); return; }
  const fold = e.target.closest("[data-fold]");
  if (fold) {
    const name = fold.dataset.fold;
    FOLDED.has(name) ? FOLDED.delete(name) : FOLDED.add(name);
    keep("symposium.folded", [...FOLDED]); return drawSide();
  }
  if (e.target.closest("[data-project]")) {
    const name = (prompt("Name the new project:") || "").trim();
    if (!name) return;
    if (!folderNames().includes(name)) NEW_PROJECTS.push(name);
    keep("symposium.newProjects", NEW_PROJECTS);
    FOLDED.delete(name); folder = name; current = null; drawSide(); drawLog(); $("#input").focus();
    return;
  }
  if (e.target.closest("[data-import]")) $("#importer").click();
};

async function openSession(id) {
  current = await call("GET", `/api/sessions/${id}`);
  folder = current.folder;
  drawSide(); drawLog();
}

function drawLog() {
  $("#bar").hidden = !current;
  if (!current) $("#settings").hidden = true;
  $("#log").innerHTML = "";
  if (!current) {
    const v = presetFor(folder).voice === "claude" ? voiceName("claude") : "a local model";
    $("#log").innerHTML = `<div class="blank">New chat in <b>${esc(folder || "chats")}</b>, answered by ${esc(v)}.<br>`
      + `It's kept once the first answer comes back.</div>`;
    return;
  }
  $("#title").value = current.title;
  $("#move").value = current.folder;
  drawSettings();
  const msgs = current.messages;
  msgs.forEach((m, i) => add(m.role, m.content, m.meta, null, i === msgs.length - 1 && m.role === "assistant"));
}

// "Reachable" is not the same as "usable": Lodestar's /health is public, so a
// wrong bearer still answers 200. The label says which of those you have.
const STATUS_LABEL = {
  ok:           ["up",   "lodestar"],
  no_inference: ["warn", "no model"],
  unauthorized: ["down", "bad token"],
  unreachable:  ["down", "lodestar down"],
};

async function poll() {
  try {
    const h = await (await fetch("/health")).json();
    const [cls, label] = STATUS_LABEL[h.lodestar.status] || ["down", "unknown"];
    $("#dot").className = "dot " + cls;
    $("#stat").textContent = label;
    $("#stat-pill").title = h.lodestar.detail || "";
    $("#stat-pill").classList.toggle("hot", cls === "up");
  } catch {
    $("#dot").className = "dot down"; $("#stat").textContent = "offline";
    $("#stat-pill").classList.remove("hot");
  }
}

// The receipt under an answer: which model, where, what it reached for, how long.
function receipt(meta) {
  if (!meta) return "";
  return [meta.model, meta.node,
          meta.tools && meta.tools.length ? `tools: ${meta.tools.join(", ")}`
            : (meta.tool_rounds ? `tools: ${meta.tool_rounds}` : null),
          meta.seconds != null ? `${meta.seconds}s` : null].filter(Boolean).join(" · ");
}

function add(role, text, meta, cls, retry) {
  const d = document.createElement("div");
  d.className = "msg" + (role === "user" ? " me" : "") + (cls ? " " + cls : "");
  const t = document.createElement("div"); t.className = "text"; t.textContent = text;
  d.append(t);
  const line = receipt(meta);
  if (line || retry) {
    const m = document.createElement("div"); m.className = "meta";
    if (line) m.append(Object.assign(document.createElement("span"), { textContent: line }));
    if (retry) {
      const b = Object.assign(document.createElement("button"), { textContent: "retry", type: "button" });
      b.onclick = () => send({ retry: true });
      m.append(b);
    }
    d.append(m);
  }
  $("#log").append(d); $("#log").scrollTop = 1e9;
  return d;
}

// ── Sending: streamed, shown as it comes ──────────────────────────────────

$("#input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
});

$("#composer").addEventListener("submit", e => {
  e.preventDefault();
  const text = $("#input").value.trim();
  if (text) send({ prompt: text });
});

async function send(ask) {
  if (busy) return;
  busy = true; $("#send").disabled = true;
  // Only the newest answer can be retried, and not while another is coming.
  $("#log").querySelectorAll(".meta button").forEach(b => b.remove());
  try {
    // A session exists before its first message so the answer has somewhere
    // to land; one whose first turn fails is left empty, not half-written.
    if (!current) { current = await call("POST", "/api/sessions", { folder }); await refresh(); }
    if (ask.retry) {
      // The answer being retried comes off the screen; it stays on disk until
      // its replacement is in.
      drawLog();
      if (current.messages.at(-1)?.role === "assistant") $("#log").lastChild?.remove();
    } else {
      $("#log").querySelector(".blank")?.remove();
      add("user", ask.prompt);
      $("#input").value = "";
    }
    if (!await stream({ session: current.id, ...ask })) giveBack(ask);
  } catch (ex) {
    add("assistant", [ex.message, ex.body?.detail, ex.body?.hint].filter(Boolean).join(" - "), null, "err");
    giveBack(ask);
  } finally {
    busy = false; $("#send").disabled = false; $("#input").focus();
  }
}

// A turn that didn't come back isn't kept, so its prompt goes back in the
// composer rather than making you type it again.
function giveBack(ask) {
  if (ask.prompt && !$("#input").value) $("#input").value = ask.prompt;
}

// True when the answer came back and was kept.
async function stream(body) {
  const bubble = add("assistant", "", null, "live");
  const text = bubble.querySelector(".text");
  let tool = null, fresh = false;
  const r = await fetch("/api/chat/stream", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) {
    bubble.remove();
    const j = await r.json().catch(() => ({}));
    throw Object.assign(new Error(j.error || `HTTP ${r.status}`), { body: j });
  }
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = "", failed = null, kept = false;
  const handle = ev => {
    if (ev.type === "chunk") {
      // After a tool runs, Lodestar starts the answer over: what came before
      // was the preamble to the call, not part of the reply.
      if (fresh) { text.textContent = ""; fresh = false; }
      text.textContent += ev.content; $("#log").scrollTop = 1e9;
    } else if (ev.type === "tool_status") {
      tool = tool || bubble.insertBefore(Object.assign(document.createElement("div"), { className: "tool" }), text);
      tool.textContent = `calling ${ev.tool}…`; fresh = true;
    } else if (ev.type === "done") {
      text.textContent = ev.response ?? text.textContent;
      tool?.remove();
    } else if (ev.type === "saved") {
      // The server has it; redraw from the file so what you see is what's kept.
      kept = true;
      return refresh().then(() => openSession(ev.session.id));
    } else if (ev.type === "error") {
      failed = ev;
    }
  };
  const pending = [];
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      if (line.trim()) { try { pending.push(handle(JSON.parse(line))); } catch {} }
    }
  }
  await Promise.all(pending);
  if (!kept && !failed) failed = { error: "the answer stopped before it was kept" };
  if (failed) {
    // Shown, never kept: the session file only ever gets turns that came back.
    bubble.classList.add("err"); bubble.classList.remove("live"); tool?.remove();
    text.textContent = [text.textContent && `${text.textContent}\n\n`, failed.error, failed.detail, failed.hint]
      .filter(Boolean).join(" ");
  }
  return kept;
}

// ── The session bar ───────────────────────────────────────────────────────

async function patch(changes) {
  current = await call("PATCH", `/api/sessions/${current.id}`, changes);
  await refresh();
}

$("#title").addEventListener("change", () => patch({ title: $("#title").value.trim() }));
$("#move").addEventListener("change", async () => {
  await patch({ folder: $("#move").value });
  folder = current.folder; drawSettings();
});

$("#del").onclick = async () => {
  if (CFG.confirm_destructive && !confirm(`Delete "${current.title || "untitled"}"? This can't be undone.`)) return;
  await call("DELETE", `/api/sessions/${current.id}`);
  current = null; await refresh(); drawLog();
};

$("#export").onclick = () => {
  const blob = new Blob([JSON.stringify(current, null, 1)], { type: "application/json" });
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(blob),
    download: `${(current.title || "session").replace(/[^\w.-]+/g, "_").slice(0, 60)}.json` });
  a.click(); URL.revokeObjectURL(a.href);
};

$("#importer").onchange = async () => {
  const file = $("#importer").files[0]; $("#importer").value = "";
  if (!file) return;
  try {
    current = await call("POST", "/api/sessions", JSON.parse(await file.text()));
    folder = current.folder; await refresh(); drawLog();
  } catch (ex) { alert(`Could not import ${file.name}: ${ex.message}`); }
};

let modelsLoaded = false;
$("#gear").onclick = async () => {
  $("#settings").hidden = !$("#settings").hidden;
  if ($("#settings").hidden || modelsLoaded) return;
  try {
    const { models } = await call("GET", "/api/models");
    $("#models").innerHTML = models.map(m => `<option value="${esc(m)}">`).join("");
    modelsLoaded = true;
  } catch { /* no model list is fine; the field still takes a name */ }
};

function drawSettings() {
  const preset = presetFor(current.folder);
  for (const k of SETTINGS) {
    const el = $(`#s-${k}`), own = current.settings[k], inherited = preset[PRESET[k] || k];
    el.value = own ?? "";
    el.placeholder = inherited != null && inherited !== "" ? String(inherited) : "default";
  }
  $("#s-voice").options[0].textContent = `project's (${voiceName(preset.voice || "lodestar")})`;
}

for (const k of SETTINGS) {
  $(`#s-${k}`).addEventListener("change", e => {
    const raw = String(e.target.value).trim();
    let v = raw === "" ? null : raw;
    if (v !== null && NUMBER.has(k)) {
      v = k === "max_tokens" ? parseInt(v, 10) : parseFloat(v);
      if (Number.isNaN(v)) return drawSettings();
    }
    patch({ settings: { [k]: v } }).catch(ex => { alert(ex.message); drawSettings(); });
  });
}

boot();
