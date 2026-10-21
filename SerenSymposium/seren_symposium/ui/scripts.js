// Symposium's page logic, dropped into the Meninges baseplate's <script>. The
// baseplate's own api() attaches a saved bearer; this page has none to send,
// so it talks to its shim through call() instead.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const uid = () => Math.random().toString(36).slice(2, 10);
const icon = (name, cls = "i") => `<svg class="${cls}"><use href="#i-${name}"/></svg>`;

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
let CFG = null, tab = "chat", folder = "", SESSIONS = [], current = null, drawing = false, HEALTH = null;
// Files waiting in the tray: {id, name, size, type, file?, url?, stored?} -
// `stored` once it is on the server (an edit puts a message's files back).
let PENDING = [];
// Only this window remembers these - which projects are folded, the tab, the
// screen-share choices. Projects themselves are kept by Symposium (prefs.py).
const local = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } };
const keep = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} };
let FOLDED = new Set(local("symposium.folded", []));
// Chats with an answer coming right now: the server's `answering`, plus the
// one this window just sent. Only THAT chat refuses a new message; every
// other chat is open, and the answer keeps coming whether or not anyone is
// looking at it (the server runs the turn; a window only follows it).
let ANSWERING = new Set();
// The one live stream this window is reading, if any: {sid, ctrl}.
let FOLLOWER = null;
const PLACEHOLDER = $("#input").placeholder;
const chatBusy = () => drawing || (current != null && ANSWERING.has(current.id));
function lockComposer() {
  const locked = chatBusy();
  $("#send").disabled = locked;
  $("#input").placeholder = locked && !drawing ? "answering… read or write in another chat meanwhile" : PLACEHOLDER;
}

const voiceName = v => v === "claude" ? (CFG.claude?.name || "claude") : "local model";
const fileUrl = (sid, name) => `/api/sessions/${encodeURIComponent(sid)}/files/${encodeURIComponent(name)}`;
const isImage = t => /^image\/(png|jpeg|gif|webp|bmp|svg\+xml)$/.test(t || "");
// A screen-share frame: marked by the server; older ones known by their name.
const isScreen = f => f.screen === true || /^screen-\d{8}-\d{6}\.jpg$/.test(f.orig || "");
const size = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const when = at => new Date(at * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

async function reloadConfig() {
  CFG = await (await fetch("/api/config")).json();
  drawVoices();
}

async function boot() {
  CFG = await (await fetch("/api/config")).json();
  await adoptOldProjects();
  folder = (CFG.folders[0] || {}).name || "";
  drawVoices(); await refresh(); drawLog(); drawTray(); screenInit(); earsInit();
  poll(); setInterval(poll, 15000);
  show(local("symposium.tab", "chat"));
}

// ── Tabs ────────────────────────────────────────────────────────────────
// The baseplate styles .tab/.view; which one shows, and what waking a tab
// starts, is ours.
$("#tabbar").addEventListener("click", e => { const t = e.target.closest("[data-tab]"); if (t) show(t.dataset.tab); });

function show(t) {
  if (!["chat", "settings", "backend"].includes(t)) t = "chat";
  tab = t; keep("symposium.tab", t);
  document.querySelectorAll(".tabbar .tab").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === t));
  if (t === "settings") drawSettings();
  if (t === "backend") { drawServices(); pingAll(); }
}

// ── Projects and chats ──────────────────────────────────────────────────

async function refresh() {
  SESSIONS = (await call("GET", "/api/sessions")).sessions;
  for (const s of SESSIONS) s.answering ? ANSWERING.add(s.id) : ANSWERING.delete(s.id);
  drawSide(); lockComposer();
}

// The projects, then any name a chat still uses that no project has (a
// hand-edited file must not make chats vanish), then the loose chats.
function folderNames() {
  const names = CFG.folders.map(f => f.name);
  for (const s of SESSIONS) if (s.folder && !names.includes(s.folder)) names.push(s.folder);
  if (SESSIONS.some(s => !s.folder) || !names.length) names.push("");
  return names;
}

// Projects made before they were kept by Symposium lived only in this
// window's storage; hand them over once.
async function adoptOldProjects() {
  const old = local("symposium.newProjects", null);
  if (!Array.isArray(old)) return;
  for (const name of old) { try { await call("POST", "/api/projects", { name }); } catch {} }
  try { localStorage.removeItem("symposium.newProjects"); } catch {}
  CFG = await (await fetch("/api/config")).json();
}

async function newProject(name) {
  await call("POST", "/api/projects", { name });
  await reloadConfig();
  stopFollowing(); FOLDED.delete(name); folder = name; current = null;
  drawSide(); drawLog(); lockComposer(); if (tab === "settings") drawSettings();
}

const presetFor = name => CFG.folders.find(f => f.name === name) || {};

function drawSide() {
  $("#side").innerHTML = folderNames().map(name => {
    const f = presetFor(name);
    const bits = [f.voice === "claude" ? voiceName("claude") : null, f.model,
                  f.temperature != null ? `t=${f.temperature}` : null].filter(Boolean).join(" · ");
    const chats = SESSIONS.filter(s => s.folder === name);
    const open = !FOLDED.has(name) || (current && current.folder === name);
    const rows = open ? chats.map(s => {
      const here = current && s.id === current.id;
      return `<div class="sess" draggable="true" data-s="${esc(s.id)}" data-folder="${esc(name)}" aria-current="${here}"
                   title="${esc(s.title || "untitled")} · ${s.count} messages">${icon(here ? "kylix-full" : "kylix")}<span>${esc(s.title || "untitled")}</span>`
           + (ANSWERING.has(s.id) ? `<i class="live-dot" title="an answer is coming"></i>` : "")
           + (s.sources?.length ? `<i class="fed-dot" title="a plugin speaks in this chat: ${esc(s.sources.join(", "))}"></i>` : "")
           + `</div>`;
    }) : [];
    return `<div class="folder" data-folder="${esc(name)}"><span class="name" data-fold="${esc(name)}" title="${open ? "fold" : "unfold"}">`
         + `${icon(open ? "krater-full" : "krater")}<span>${esc(name || "chats")}</span></span>`
         + (bits ? `<span class="hint">${esc(bits)}</span>` : "")
         + `<button data-new="${esc(name)}" title="new chat in ${esc(name || "chats")}">+</button></div>`
         + (open ? (rows.join("") || `<div class="sess empty">nothing yet</div>`) : "");
  }).join("") + `<div class="loose-drop" data-folder="">drop here to take it out of its project</div>`
              + `<div class="foot"><button class="ghost" data-project="1">+ project</button>`
              + `<button class="ghost" data-import="1">import…</button></div>`;
}

$("#side").onclick = async e => {
  const row = e.target.closest("[data-s]");
  if (row) return openSession(row.dataset.s);
  const add = e.target.closest("[data-new]");
  if (add) { stopFollowing(); folder = add.dataset.new; current = null; drawSide(); drawLog(); lockComposer(); $("#input").focus(); return; }
  const fold = e.target.closest("[data-fold]");
  if (fold) {
    const name = fold.dataset.fold;
    FOLDED.has(name) ? FOLDED.delete(name) : FOLDED.add(name);
    keep("symposium.folded", [...FOLDED]); return drawSide();
  }
  if (e.target.closest("[data-project]")) {
    const name = (prompt("Name the new project:") || "").trim();
    if (!name) return;
    if (folderNames().includes(name)) { folder = name; current = null; drawSide(); drawLog(); return; }
    try { await newProject(name); $("#input").focus(); } catch (ex) { alert(ex.message); }
    return;
  }
  if (e.target.closest("[data-import]")) $("#importer").click();
};

// Drag a chat onto a project - its name, or any chat in it - to move it there;
// onto the strip at the bottom to take it out of every project.
let dragging = null;
const dropOn = e => dragging && e.target.closest("#side [data-folder]");
function markDrop(name) {
  document.querySelectorAll("#side .folder").forEach(el => el.classList.toggle("drop", el.dataset.folder === name));
  $("#side .loose-drop")?.classList.toggle("drop", name === "");
}
$("#side").addEventListener("dragstart", e => {
  const row = e.target.closest(".sess[data-s]");
  if (!row) return;
  dragging = row.dataset.s;
  e.dataTransfer.setData("text/x-symposium-chat", dragging);
  e.dataTransfer.effectAllowed = "move";
  $("#side").classList.add("dragging");
});
$("#side").addEventListener("dragend", () => { dragging = null; $("#side").classList.remove("dragging"); markDrop(null); });
$("#side").addEventListener("dragover", e => {
  const t = dropOn(e);
  if (!t) return markDrop(null);
  e.preventDefault(); e.dataTransfer.dropEffect = "move"; markDrop(t.dataset.folder);
});
$("#side").addEventListener("drop", async e => {
  const t = dropOn(e), id = dragging;
  if (!t) return;
  e.preventDefault();
  dragging = null; $("#side").classList.remove("dragging"); markDrop(null);
  const s = SESSIONS.find(x => x.id === id), to = t.dataset.folder;
  if (!s || s.folder === to) return;
  try {
    const moved = await call("PATCH", `/api/sessions/${id}`, { folder: to });
    if (current?.id === id) { current = moved; folder = to; }
    FOLDED.delete(to); keep("symposium.folded", [...FOLDED]);
    await refresh(); if ($("#chat-settings").open) drawChatSettings();
  } catch (ex) { alert(ex.message); }
});

// Right-click: rename (in place) and delete on a chat, rename on a project.
function openMenu(x, y, items) {
  const m = $("#menu");
  m.innerHTML = items.map(([label, , cls], i) =>
    `<button type="button" role="menuitem" data-item="${i}"${cls ? ` class="${cls}"` : ""}>${esc(label)}</button>`).join("");
  m.hidden = false;
  m.style.left = `${Math.min(x, innerWidth - m.offsetWidth - 6)}px`;
  m.style.top = `${Math.min(y, innerHeight - m.offsetHeight - 6)}px`;
  m.onclick = e => { const b = e.target.closest("[data-item]"); if (b) { closeMenu(); items[b.dataset.item][1](); } };
}
const closeMenu = () => { $("#menu").hidden = true; };
document.addEventListener("click", e => { if (!e.target.closest("#menu")) closeMenu(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeMenu(); });
window.addEventListener("blur", closeMenu);

$("#side").addEventListener("contextmenu", e => {
  const row = e.target.closest(".sess[data-s]"), proj = e.target.closest(".folder[data-folder]");
  if (!row && !proj) return;
  e.preventDefault();
  if (row) {
    const id = row.dataset.s;
    openMenu(e.clientX, e.clientY, [["Rename", () => editInPlace(row.querySelector("span"), t => renameChat(id, t))],
                                    ["Delete", () => deleteChat(id), "danger"]]);
  } else {
    const name = proj.dataset.folder;
    const items = [["New chat here", () => proj.querySelector("[data-new]").click()]];
    // The loose chats are not a project, so there is nothing to call them.
    if (name) {
      items.unshift(["Rename", () => editInPlace(proj.querySelector(".name > span"), t => renameProject(name, t))]);
      items.push(["Project settings…", () => { OPEN_PROJECT = name; show("settings"); }],
                 ["Delete project", () => deleteProject(name), "danger"]);
    }
    openMenu(e.clientX, e.clientY, items);
  }
});

// Turn a label into a box to type in: Enter or leaving it saves, Escape doesn't.
function editInPlace(span, save) {
  if (!span) return;
  const was = span.textContent;
  const box = Object.assign(document.createElement("input"), { value: was, className: "inplace", maxLength: 120 });
  span.replaceWith(box); box.focus(); box.select();
  let done = false;
  const finish = async keepIt => {
    if (done) return; done = true;
    const v = box.value.trim();
    if (keepIt && v && v !== was) { try { await save(v); } catch (ex) { alert(ex.message); } }
    drawSide();
  };
  box.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  box.addEventListener("blur", () => finish(true));
  box.addEventListener("click", e => e.stopPropagation());
}

async function renameChat(id, title) {
  const s = await call("PATCH", `/api/sessions/${id}`, { title });
  if (current?.id === id) { current = s; drawLog(); }
  await refresh();
}

async function renameProject(from, to) {
  if (folderNames().includes(to) && !confirm(`There is already a project called "${to}". Move ${from}'s chats into it?`)) return;
  await call("PATCH", "/api/projects", { name: from, rename: to });
  if (FOLDED.delete(from)) { FOLDED.add(to); keep("symposium.folded", [...FOLDED]); }
  if (folder === from) folder = to;
  if (OPEN_PROJECT === from) OPEN_PROJECT = to;
  await reloadConfig(); await refresh();
  if (current && current.folder === from) await openSession(current.id);
  if (tab === "settings") drawSettings();
}

async function deleteProject(name) {
  const n = SESSIONS.filter(s => s.folder === name).length;
  if (!sure(`Delete the project "${name}"?` + (n ? ` Its ${n} chat${n === 1 ? "" : "s"} stay, out of any project.` : ""))) return;
  try { await call("POST", "/api/projects/remove", { name }); } catch (ex) { return alert(ex.message); }
  if (folder === name) folder = "";
  await reloadConfig(); await refresh();
  if (current && current.folder === name) await openSession(current.id);
  if (tab === "settings") drawSettings();
}

async function deleteChat(id) {
  const s = SESSIONS.find(x => x.id === id);
  if (!sure(`Delete "${s?.title || "untitled"}" and its files? This can't be undone.`)) return;
  try { await call("DELETE", `/api/sessions/${id}`); } catch (ex) { return alert(ex.message); }
  if (current?.id === id) { current = null; drawLog(); }
  await refresh();
}

async function openSession(id) {
  stopFollowing();
  current = await call("GET", `/api/sessions/${id}`);
  folder = current.folder;
  drawSide(); drawLog(); lockComposer();
  if ($("#chat-settings").open) drawChatSettings();
  // An answer still coming in this chat: pick it up where it is.
  if (current.answering) followLive(id);
}

// ── The conversation ────────────────────────────────────────────────────
// The log follows the newest message - through pictures loading in after it
// is drawn - until you scroll up to read; scrolling back down picks it up again.
let pinned = true;
$("#log").addEventListener("scroll", () => {
  const l = $("#log"); pinned = l.scrollHeight - l.scrollTop - l.clientHeight < 60;
});
const toBottom = (force = false) => { if (force) pinned = true; if (pinned) $("#log").scrollTop = 1e9; };
$("#log").addEventListener("load", e => { if (e.target.tagName === "IMG") toBottom(); }, true);

function drawLog() {
  $("#titlebar").hidden = !current;
  $("#log").innerHTML = "";
  drawFiles();
  if (!current) {
    const v = presetFor(folder).voice === "claude" ? voiceName("claude") : "a local model";
    $("#log").innerHTML = `<div class="blank">New chat in <b>${esc(folder || "chats")}</b>, answered by ${esc(v)}.<br>`
      + `It's kept once the first answer comes back.</div>`;
    return;
  }
  if (document.activeElement !== $("#title")) { $("#title").value = current.title; setRenaming(false); }
  const msgs = current.messages;
  msgs.forEach((m, i) => $("#log").append(bubble(m, i, msgs.length)));
  toBottom(true);
}

// Who said it: you, the chat's Claude by name, or the local model that answered.
function whoName(m) {
  // Fed in by a plugin (the game table): the plugin's name, never yours.
  if (m.role === "user") return m.source ? (m.by || m.source) : (CFG.you || "you");
  const meta = m.meta || {};
  if (CFG.claude?.name && String(meta.node).toLowerCase() === CFG.claude.name.toLowerCase()) return CFG.claude.name;
  if (meta.voice === "picture") return `🖼 ${String(meta.model?.name || meta.model || "a picture").replace(/\.safetensors$/i, "")}`;
  return meta.model ? meta.model.replace(/\.gguf$/i, "") : "local model";
}

function voiceOfChat() {
  const v = current?.settings?.voice || presetFor(current ? current.folder : folder).voice || "lodestar";
  return v === "claude" && CFG.claude?.available ? "claude" : "lodestar";
}

// The receipt under an answer: which model, where, what it reached for, how long.
function receipt(meta) {
  if (!meta) return "";
  return [meta.model, meta.node,
          meta.tools && meta.tools.length ? `tools: ${meta.tools.join(", ")}`
            : (meta.tool_rounds ? `tools: ${meta.tool_rounds}` : null),
          meta.seconds != null ? `${meta.seconds}s` : null].filter(Boolean).join(" · ");
}

// One message. Its tools are the ones the mock drew - delete and retry on an
// answer, edit and delete on yours - plus rewind and fork, because "go back
// to here" and "try another way from here" are what an edit is really for.
function bubble(m, i, count, opts = {}) {
  const d = document.createElement("div");
  // A prompt fed in by a plugin (the game table) is marked by the server and
  // drawn as the plugin's: on the left, dashed, tagged. It never wears your
  // bubble, so what you said and what a plugin said can always be told apart.
  const asked = m.role === "user", fed = asked && !!m.source, mine = asked && !fed;
  d.className = "msg" + (mine ? " me" : "") + (fed ? " fed" : "") + (opts.cls ? " " + opts.cls : "");
  d.dataset.i = i;
  d.innerHTML = `<div class="who">${esc(opts.who || whoName(m))}`
              + (fed ? `<span class="fed-tag" title="fed into the chat by ${esc(m.source)} - not typed by you">plugin</span>` : "")
              + `</div>`;
  const [think, words] = asked ? ["", m.content] : splitThink(m.content);
  const steps = [...(think ? [{ kind: "thinking", text: think }] : []), ...(m.meta?.steps || [])];
  if (steps.length) d.append(stepsEl(steps));
  d.append(renderMd(words, current?.id));
  const atts = attachmentsEl(m.files, current?.id, asked ? "" : m.content);
  if (atts) d.append(atts);
  if (!opts.bare) {
    const meta = document.createElement("div"); meta.className = "meta";
    const line = asked ? "" : receipt(m.meta);
    if (line) meta.append(Object.assign(document.createElement("span"), { className: "receipt", textContent: line }));
    meta.append(actionsEl(m, i, count));
    d.append(meta);
  }
  return d;
}

// ── Under the fold: how an answer got there ─────────────────────────────
// Claude's thinking, each tool it used and what came back, each time it
// asked you - kept with the answer. A local model's <think> block too.

// A <think>…</think> at the start of an answer (an open one while it streams).
function splitThink(text) {
  const m = String(text || "").match(/^\s*<think>([\s\S]*?)(<\/think>|$)/);
  return m ? [m[1].trim(), text.slice(m[0].length)] : ["", text];
}

const ANSWERED = { once: "allowed once", always: "always allowed", deny: "refused" };
function stepHtml(s) {
  if (s.kind === "thinking") return `<div class="step think">${esc(s.text)}</div>`;
  if (s.kind === "asked") {
    return `<div class="step asked">${icon("gear")}<span>asked to use <code>${esc(s.tool)}</code> `
         + `<span class="sum">${esc(s.summary || "")}</span> - <b>${esc(ANSWERED[s.answer] || s.answer)}</b></span></div>`;
  }
  const mark = s.ok === undefined ? "…" : s.ok ? "✓" : "✗";
  return `<div class="step tool${s.ok === false ? " bad" : ""}"><span class="mark">${mark}</span>`
       + `<span><code>${esc(s.tool)}</code> <span class="sum">${esc(s.summary || "")}</span>`
       + (s.result ? `<pre>${esc(s.result)}</pre>` : "") + `</span></div>`;
}

function stepsLine(steps, liveNow) {
  if (liveNow) {
    const last = steps.at(-1) || {};
    return last.kind === "tool" && last.ok === undefined ? `using ${last.tool}…`
         : last.kind === "thinking" ? "thinking…" : "working…";
  }
  const tools = steps.filter(s => s.kind === "tool").length, asked = steps.filter(s => s.kind === "asked").length;
  return [steps.some(s => s.kind === "thinking") && "thought it through",
          tools && `${tools} tool${tools === 1 ? "" : "s"}`, asked && `asked you ${asked}×`].filter(Boolean).join(" · ");
}

function stepsEl(steps, liveNow = false, el = null) {
  const d = el || Object.assign(document.createElement("details"), { className: "steps" });
  const open = d.open;
  d.innerHTML = `<summary>${esc(stepsLine(steps, liveNow))}</summary><div class="step-list">${steps.map(stepHtml).join("")}</div>`;
  d.open = open;
  return d;
}

function actionsEl(m, i, count) {
  const acts = document.createElement("span"); acts.className = "acts";
  const last = i === count - 1, b = [];
  if (m.role === "user" && !m.source) b.push(["edit", "edit: go back to this message and change it"]);
  if (m.role === "assistant" && last) b.push(["retry", "ask again"]);
  if (m.role === "assistant" && !last) b.push(["rewind", "rewind to here: what comes after goes"]);
  if (m.role === "assistant" && CFG.speech?.enabled) b.push(["say", "say it: hear this answer in the assistant's voice"]);
  b.push(["copy", "copy this message"], ["fork", "fork: a new chat from here"], ["trash", "delete this message"]);
  acts.innerHTML = b.map(([n, t]) =>
    `<button type="button" data-act="${n}" data-i="${i}" title="${esc(t)}"${n === "trash" ? ' class="danger"' : ""}>${icon(n)}</button>`).join("");
  return acts;
}

$("#log").addEventListener("click", async e => {
  const img = e.target.closest(".md img, .atts img");
  if (img) return lightbox(img.src);
  const shot = e.target.closest("[data-shot]");
  if (shot) return lightbox(shot.dataset.shot);
  const copy = e.target.closest(".copy");
  if (copy) return copyCode(copy);
  const btn = e.target.closest("[data-act]");
  if (btn?.dataset.act === "copy" && current) {
    const m = current.messages[Number(btn.dataset.i)];
    return copyText(m ? m.content : "", btn, icon("copy"));
  }
  if (!btn || chatBusy() || !current) return;
  try { await act(btn.dataset.act, Number(btn.dataset.i)); } catch (ex) { alert(ex.message); }
});

const sure = text => !CFG.confirm_destructive || confirm(text);

async function act(what, i) {
  const msgs = current.messages, after = msgs.length - i - 1;
  if (what === "retry") return send({ retry: true });
  if (what === "edit") {
    if (after > 0 && !sure(`Edit goes back to this message: the ${after} after it go. Carry on?`)) return;
    const m = msgs[i];
    current = await call("POST", `/api/sessions/${current.id}/rewind`, { to: i });
    PENDING = (m.files || []).map(f => ({ id: uid(), name: f.orig || f.name, size: f.size, type: f.type, stored: f }));
    $("#input").value = m.content;
    await refresh(); drawLog(); drawTray(); $("#input").focus();
  } else if (what === "rewind") {
    if (!sure(`Rewind to here? The ${after} message${after === 1 ? "" : "s"} after it go.`)) return;
    current = await call("POST", `/api/sessions/${current.id}/rewind`, { to: i + 1 });
    await refresh(); drawLog();
  } else if (what === "fork") {
    const fork = await call("POST", `/api/sessions/${current.id}/fork`, { at: i + 1 });
    await refresh(); await openSession(fork.id);
  } else if (what === "trash") {
    if (!sure("Delete this message? This can't be undone.")) return;
    current = await call("DELETE", `/api/sessions/${current.id}/messages/${i}`);
    await refresh(); drawLog();
  } else if (what === "say") {
    await sayAloud(i);
  }
}

// ── Say it: an answer in the assistant's voice ───────────────────────────────────
// The words go to Lodestar's speaker through Symposium; the clip comes back
// as a file on that answer (marked `said`) and plays at once. The button
// shows it is working, since the speaker may be waking.
async function sayAloud(i) {
  const btn = $(`#log [data-act="say"][data-i="${i}"]`);
  if (btn) { btn.disabled = true; btn.classList.add("busy"); btn.title = "saying it…"; }
  try {
    const out = await call("POST", "/api/say", { session: current.id, index: i });
    current = await call("GET", `/api/sessions/${current.id}`);
    drawLog();
    const clip = [...$("#log").querySelectorAll(".msg")][i]?.querySelector(`audio.clip[src$="/${out.file.name}"]`);
    if (clip) { clip.scrollIntoView({ block: "nearest" }); clip.play().catch(() => {}); }
    if (out.cut && btn) btn.title = `said the first part: the answer is longer than ${CFG.speech?.max_chars || 4000} characters`;
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove("busy"); }
  }
}

// ── Markdown ────────────────────────────────────────────────────────────
// marked makes the HTML, DOMPurify takes out anything that could run, and a
// picture is only ever loaded from this chat's own files: one from the web
// stays a link (the server saves the ones an answer shows - attachments.py).

// Should the renderer ever fail to load, messages show as plain text - never
// as HTML nobody sanitised - and the rest of the window still works.
const MARKDOWN = typeof marked !== "undefined" && typeof DOMPurify !== "undefined";
if (MARKDOWN) {
  marked.setOptions({ gfm: true, breaks: true });
  DOMPurify.addHook("afterSanitizeAttributes", node => {
    if (node.tagName === "A") { node.setAttribute("target", "_blank"); node.setAttribute("rel", "noopener noreferrer"); }
  });
}
const OURS = /^files\/([0-9a-f]{8}-[A-Za-z0-9._-]{1,80})$/;

function renderMd(text, sid) {
  const box = document.createElement("div"); box.className = "md";
  if (!MARKDOWN) { box.style.whiteSpace = "pre-wrap"; box.textContent = text || ""; return box; }
  box.innerHTML = DOMPurify.sanitize(marked.parse(text || ""), {
    FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "iframe"], FORBID_ATTR: ["style"] });
  box.querySelectorAll("img").forEach(img => {
    const src = img.getAttribute("src") || "", ours = src.match(OURS);
    if (ours && sid) { img.src = fileUrl(sid, ours[1]); img.loading = "lazy"; return; }
    const label = img.alt || src;
    if (/^https?:\/\//i.test(src)) {
      img.replaceWith(Object.assign(document.createElement("a"), {
        href: src, textContent: `🖼 ${label}`, target: "_blank", rel: "noopener noreferrer" }));
    } else img.replaceWith(document.createTextNode(label ? `[${label}]` : ""));
  });
  box.querySelectorAll("pre > code").forEach(code => {
    const lang = (code.className.match(/language-([\w+#-]+)/) || [])[1];
    if (typeof hljs !== "undefined" && (!lang || hljs.getLanguage(lang))) { try { hljs.highlightElement(code); } catch {} }
    const pre = code.parentElement;
    if (lang) pre.insertAdjacentHTML("afterbegin", `<span class="lang">${esc(lang)}</span>`);
    pre.insertAdjacentHTML("afterbegin", `<button type="button" class="copy" title="copy">${icon("copy")}</button>`);
  });
  return box;
}

const copyCode = btn => copyText(btn.parentElement.querySelector("code").textContent, btn, icon("copy"));

// Into the clipboard, and the button says so for a moment.
async function copyText(text, btn, back) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const t = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(t); t.select(); document.execCommand("copy"); t.remove();
  }
  if (btn) { btn.innerHTML = "✓"; setTimeout(() => { btn.innerHTML = back; }, 1200); }
}

// The whole chat as text: who said it, what they said, what came with it.
$("#copy-all").onclick = () => {
  if (!current) return;
  const text = current.messages.map(m => {
    const files = (m.files || []).map(f => `[${isScreen(f) ? "screen share" : "attached"}: ${f.orig || f.name}]`);
    return [`${whoName(m)}:`, m.content, ...files].filter(Boolean).join("\n");
  }).join("\n\n");
  copyText(text, $("#copy-all"), "Copy all");
};

// What a message carried: pictures as pictures, the rest as chips. An
// answer's picture that its Markdown already shows is not shown twice.
// A screen-share frame is not shown in the conversation - it would bury it -
// only a chip that opens it; they all sit in the screen-share panel.
function attachmentsEl(files, sid, shownIn = "") {
  const list = (files || []).filter(f => !(shownIn && shownIn.includes(`files/${f.name}`)));
  if (!list.length || !sid) return null;
  const box = document.createElement("div"); box.className = "atts";
  box.innerHTML = list.map(f => isScreen(f)
    ? `<button type="button" class="chip shot" data-shot="${esc(fileUrl(sid, f.name))}" title="open the frame">${icon("screen")}`
      + `<span>screen · ${esc(f.source || "shared")}</span></button>`
    : isImage(f.type)
    ? `<img src="${esc(fileUrl(sid, f.name))}" alt="${esc(f.orig || f.name)}" title="${esc(f.orig || f.name)}" loading="lazy">`
    : (f.type || "").startsWith("audio/")
    ? `<audio class="clip" controls preload="none" src="${esc(fileUrl(sid, f.name))}" title="${esc(f.orig || f.name)}"></audio>`
    : `<a class="chip" href="${esc(fileUrl(sid, f.name))}" target="_blank" rel="noopener">${icon("file")}`
      + `<span>${esc(f.orig || f.name)}</span><small>${size(f.size || 0)}</small></a>`).join("");
  return box;
}

// ── Lodestar's state ────────────────────────────────────────────────────
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
    HEALTH = await (await fetch("/health")).json();
    const [cls, label] = STATUS_LABEL[HEALTH.lodestar.status] || ["down", "unknown"];
    $("#dot").className = "dot " + cls;
    $("#stat").textContent = label;
    $("#stat-pill").title = HEALTH.lodestar.detail || "";
    $("#stat-pill").classList.toggle("hot", cls === "up");
  } catch {
    HEALTH = null;
    $("#dot").className = "dot down"; $("#stat").textContent = "offline";
    $("#stat-pill").classList.remove("hot");
  }
  if (tab === "settings") drawAbout();
  if (tab === "backend") drawPings();
  // Chats made or grown behind this window's back (the game table speaking
  // into one) show up without a reload; the open chat is re-read when it grew.
  try {
    const seen = (await call("GET", "/api/sessions")).sessions;
    const key = list => JSON.stringify(list.map(s => [s.id, s.updated, s.folder, s.title, s.answering]));
    if (key(seen) !== key(SESSIONS)) {
      SESSIONS = seen;
      for (const s of SESSIONS) s.answering ? ANSWERING.add(s.id) : ANSWERING.delete(s.id);
      drawSide(); lockComposer();
      const open = current && SESSIONS.find(s => s.id === current.id);
      if (open && open.updated !== current.updated && !ANSWERING.has(current.id) && !FOLLOWER) await openSession(current.id);
    }
  } catch {}
}

// ── The tray: files waiting to go ───────────────────────────────────────

function addFiles(list) {
  const max = (CFG.files?.max_mb || 25) * 1048576;
  for (const file of list) {
    if (file.size > max) { alert(`${file.name} is bigger than ${CFG.files.max_mb} MB.`); continue; }
    // A pasted screenshot arrives as "image.png", every time; give it a date.
    const name = file.name && file.name !== "image.png" ? file.name
      : `pasted-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.${(file.type.split("/")[1] || "png")}`;
    PENDING.push({ id: uid(), name, size: file.size, type: file.type, file,
                   url: isImage(file.type) ? URL.createObjectURL(file) : null });
  }
  drawTray();
}

function dropPending(id) {
  const p = PENDING.find(x => x.id === id);
  if (p?.url) URL.revokeObjectURL(p.url);
  PENDING = PENDING.filter(x => x.id !== id);
  drawTray();
}

function drawTray() {
  // The screen frame that goes with the next message, shown before it goes;
  // × skips this one (the next frame rides again).
  const shot = screenRides()
    ? `<span class="pending screen-chip" title="the newest screen frame goes with this message">`
      + `<img src="/api/screen/latest?t=${SCREEN.at}" alt="" data-shot="/api/screen/latest?t=${SCREEN.at}">`
      + `<span>screen · ${esc(SCREEN.label)}</span><button type="button" data-skip-screen="1" title="not this one">×</button></span>`
    : "";
  $("#tray-items").innerHTML = shot + PENDING.map(p => {
    const thumb = p.url ? `<img src="${esc(p.url)}" alt="">`
      : p.stored && isImage(p.type) && current ? `<img src="${esc(fileUrl(current.id, p.stored.name))}" alt="">`
      : icon("file");
    return `<span class="pending" title="${esc(p.name)} · ${size(p.size || 0)}">${thumb}<span>${esc(p.name)}</span>`
         + `<button type="button" data-drop="${p.id}" title="remove">×</button></span>`;
  }).join("");
  $("#tray-hint").hidden = PENDING.length > 0 || !!shot;
}

$("#tray").addEventListener("click", e => {
  const x = e.target.closest("[data-drop]");
  if (x) { e.stopPropagation(); return dropPending(x.dataset.drop); }
  if (e.target.closest("[data-skip-screen]")) { SCREEN.sentAt = SCREEN.at; drawTray(); drawCaption(); return; }
  const shot = e.target.closest("[data-shot]");
  if (shot) return lightbox(shot.dataset.shot);
  if (!e.target.closest(".pending")) $("#picker").click();
});
$("#tray").addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#picker").click(); } });
$("#picker").onchange = () => { addFiles($("#picker").files); $("#picker").value = ""; };
$("#input").addEventListener("paste", e => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); addFiles(files); }
});

// Drag and drop anywhere on the room.
let dragDepth = 0;
const hasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
$("#room").addEventListener("dragenter", e => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; $("#room").classList.add("dragging"); });
$("#room").addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
$("#room").addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#room").classList.remove("dragging"); } });
$("#room").addEventListener("drop", e => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; $("#room").classList.remove("dragging");
  addFiles(e.dataTransfer.files);
});

const b64 = file => new Promise((ok, no) => {
  const r = new FileReader();
  r.onload = () => ok(String(r.result).split(",", 2)[1] || "");
  r.onerror = () => no(r.error);
  r.readAsDataURL(file);
});

// The tray goes up first; the message then names what is already there.
async function uploadPending(sid) {
  const out = [];
  for (const p of PENDING) {
    if (!p.stored) p.stored = await call("POST", `/api/sessions/${sid}/files`, { name: p.name, data: await b64(p.file) });
    out.push(p.stored);
  }
  return out;
}

$("#clear").onclick = () => {
  $("#input").value = "";
  PENDING.forEach(p => p.url && URL.revokeObjectURL(p.url)); PENDING = [];
  drawTray(); $("#input").focus();
};

// ── Sending: streamed, shown as it comes ────────────────────────────────

$("#input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
});

$("#composer").addEventListener("submit", e => {
  e.preventDefault();
  const text = $("#input").value.trim();
  if (text || PENDING.length) send({ prompt: text });
});

async function send(ask) {
  if (chatBusy()) return;
  const given = { prompt: ask.prompt, pending: PENDING };
  // Only the newest answer can be retried, and nothing moves while one is coming.
  $("#log").querySelectorAll(".acts").forEach(b => b.remove());
  let sid = null;
  try {
    // A session exists before its first message so the answer and the files
    // have somewhere to land; one whose first turn fails is left empty.
    if (!current) { current = await call("POST", "/api/sessions", { folder }); await refresh(); }
    sid = current.id;
    ANSWERING.add(sid); lockComposer(); drawSide();
    if (ask.retry) {
      // The answer being retried comes off the screen; it stays on disk until
      // its replacement is in.
      drawLog();
      if (current.messages.at(-1)?.role === "assistant") $("#log").lastChild?.remove();
      $("#log").querySelectorAll(".acts").forEach(b => b.remove());
    } else {
      ask.files = await uploadPending(current.id);
      if (!ask.screen && screenRides()) ask.screen = true;
      $("#log").querySelector(".blank")?.remove();
      $("#log").append(bubble({ role: "user", content: ask.prompt || "", files: ask.files }, -1, 0, { bare: true }));
      toBottom(true);
      $("#input").value = ""; PENDING = []; drawTray();
    }
    // true: kept. false: it failed, so what you sent goes back in the
    // composer. null: you opened another chat meanwhile - the answer is still
    // coming over there, so nothing is given back and nothing is drawn here.
    const kept = await stream({ session: sid, ...ask });
    if (kept) {
      given.pending.forEach(p => p.url && URL.revokeObjectURL(p.url));
      if (ask.screen) { SCREEN.sentAt = SCREEN.at; drawTray(); drawCaption(); }
    } else if (kept === false) giveBack(ask, given);
  } catch (ex) {
    if (sid) ANSWERING.delete(sid);
    if (!sid || current?.id === sid) {
      $("#log").append(bubble({ role: "assistant", content: [ex.message, ex.body?.detail, ex.body?.hint].filter(Boolean).join(" - ") },
                              -1, 0, { cls: "err", bare: true, who: "symposium" }));
      giveBack(ask, given);
    }
  } finally {
    lockComposer(); drawSide();
    if (!sid || current?.id === sid) $("#input").focus();
  }
}

// A turn that didn't come back isn't kept, so what you sent goes back in the
// composer - words and files - rather than making you do it again.
function giveBack(ask, given) {
  if (ask.retry) return;
  if (given.prompt && !$("#input").value) $("#input").value = given.prompt;
  if (!PENDING.length) { PENDING = given.pending; drawTray(); }
}

function stopFollowing() {
  if (FOLLOWER) { FOLLOWER.ctrl.abort(); FOLLOWER = null; }
}

// Send, and follow the answer. true: kept; false: it failed; null: this window
// stopped watching (another chat was opened) - the server keeps the turn going.
async function stream(body) {
  const ctrl = new AbortController();
  const r = await fetch("/api/chat/stream", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body), signal: ctrl.signal });
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    throw Object.assign(new Error(j.error || `HTTP ${r.status}`), { body: j });
  }
  return follow(body.session, r, ctrl);
}

// An answer already coming in this chat - opened, or come back to, mid-answer:
// the server replays it from its first event, then the rest as it happens.
async function followLive(sid) {
  const ctrl = new AbortController();
  let r;
  try { r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/live`, { signal: ctrl.signal }); } catch { return null; }
  if (r.status === 404) { ANSWERING.delete(sid); lockComposer(); drawSide(); return null; }
  if (!r.ok) return null;
  return follow(sid, r, ctrl);
}

// Read a turn's events into a live bubble. The bubble belongs to the chat
// `sid`; if you leave for another chat the follower is aborted (stopFollowing)
// and nothing more is drawn or given back here - the turn itself goes on.
async function follow(sid, r, ctrl) {
  FOLLOWER = { sid, ctrl };
  const live = bubble({ role: "assistant", content: "" }, -1, 0,
                      { cls: "live", bare: true, who: voiceName(voiceOfChat()) });
  $("#log").append(live);
  let md = live.querySelector(".md");
  let raw = "", tool = null, fresh = false, queued = false;
  const steps = [];
  let stepsBox = null;
  const paint = () => {
    queued = false;
    const [think, words] = splitThink(raw);
    const shown = think ? [{ kind: "thinking", text: think }, ...steps] : steps;
    if (shown.length) {
      stepsBox = stepsEl(shown, true, stepsBox);
      if (!stepsBox.isConnected) live.insertBefore(stepsBox, live.querySelector(".who").nextSibling);
    }
    const next = renderMd(words, current?.id); md.replaceWith(next); md = next;
    toBottom();
  };
  const later = () => { if (!queued) { queued = true; requestAnimationFrame(paint); } };
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = "", failed = null, kept = false;
  const handle = ev => {
    if (ev.type === "chunk") {
      // After a tool runs, Lodestar starts the answer over: what came before
      // was the preamble to the call, not part of the reply.
      if (fresh) { raw = ""; fresh = false; }
      raw += ev.content;
      if (!queued) { queued = true; requestAnimationFrame(paint); }
    } else if (ev.type === "tool_status") {
      tool = tool || live.insertBefore(Object.assign(document.createElement("div"), { className: "tool" }), md);
      tool.textContent = `calling ${ev.tool}…`; fresh = true;
    } else if (ev.type === "thinking") {
      const last = steps.at(-1);
      if (last?.kind === "thinking") last.text += ev.content; else steps.push({ kind: "thinking", text: ev.content });
      later();
    } else if (ev.type === "tool_call") {
      steps.push({ kind: "tool", id: ev.id, tool: ev.tool, summary: ev.summary }); later();
    } else if (ev.type === "tool_result") {
      const s = steps.find(x => x.id === ev.id);
      if (s) { s.ok = ev.ok; s.result = ev.preview; later(); }
    } else if (ev.type === "permission") {
      live.insertBefore(permitCard(ev), md); toBottom(true);
    } else if (ev.type === "permission_answered") {
      // Replayed to a window that came back: the ask, and that it was answered.
      const card = live.querySelector(`.permit[data-permit-id="${ev.id}"]`);
      if (card && !card.classList.contains("answered")) {
        card.classList.add("answered", ev.answer);
        card.querySelector(".p-buttons").innerHTML = `<b>${esc(ANSWERED[ev.answer] || ev.answer)}</b>`;
      }
    } else if (ev.type === "done") {
      raw = ev.response ?? raw; paint(); tool?.remove();
    } else if (ev.type === "saved") {
      // The server has it. Redraw from the file - if this chat is still the
      // one on screen; otherwise the sidebar is enough.
      kept = true; ANSWERING.delete(sid);
      return refresh().then(() => { if (current?.id === sid) return openSession(sid); });
    } else if (ev.type === "error") {
      failed = ev;
    }
  };
  const pending = [];
  let left = false;
  try {
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
  } catch (ex) {
    if (ex.name !== "AbortError") throw ex;
    left = true;                                   // stopFollowing: another chat was opened
  }
  if (FOLLOWER?.ctrl === ctrl) FOLLOWER = null;
  await Promise.all(pending);
  if (left) return kept ? true : null;
  if (!kept && !failed) failed = { error: "the answer stopped before it was kept" };
  if (failed) {
    // Shown, never kept: the session file only ever gets turns that came back.
    ANSWERING.delete(sid); lockComposer(); drawSide();
    live.classList.add("err"); live.classList.remove("live"); tool?.remove();
    raw = [raw && `${raw}\n\n`, failed.error, failed.detail, failed.hint].filter(Boolean).join(" ");
    paint();
  }
  return kept;
}

// May I? Claude wants a tool that is not on its list, and waits on you.
function permitCard(ev) {
  const card = document.createElement("div");
  card.className = "permit"; card.dataset.permitId = ev.id;
  card.innerHTML = `<div class="p-ask">${icon("gear")}<span><b>${esc(voiceName("claude"))}</b> wants to use <code>${esc(ev.tool)}</code></span></div>`
    + `<pre class="p-what">${esc(ev.summary)}</pre>`
    + `<div class="p-buttons"><button type="button" class="primary" data-permit="once">Allow once</button>`
    + `<button type="button" class="ghost" data-permit="always" title="from now on, without asking">Always: <code>${esc(ev.rule)}</code></button>`
    + `<button type="button" class="ghost danger" data-permit="deny">Deny</button></div>`;
  return card;
}

$("#log").addEventListener("click", async e => {
  const b = e.target.closest("[data-permit]"), card = b?.closest(".permit");
  if (!card) return;
  card.querySelectorAll("button").forEach(x => { x.disabled = true; });
  try {
    const r = await call("POST", "/api/permit/answer", { id: card.dataset.permitId, answer: b.dataset.permit });
    card.classList.add("answered", b.dataset.permit);
    card.querySelector(".p-buttons").innerHTML = `<b>${esc(ANSWERED[r.answer])}</b>`
      + (r.answer === "always" ? ` <span class="muted">- <code>${esc(r.rule)}</code> is on the list now</span>` : "");
  } catch (ex) {
    card.querySelector(".p-buttons").textContent = `could not answer: ${ex.message}`;
  }
});

// ── The title bar ───────────────────────────────────────────────────────

async function patch(changes) {
  current = await call("PATCH", `/api/sessions/${current.id}`, changes);
  await refresh();
}

function setRenaming(on) {
  $("#title").readOnly = !on;
  $("#rename").textContent = on ? "Save" : "Edit Name";
  if (on) { $("#title").focus(); $("#title").select(); }
}
$("#rename").onclick = async () => {
  if ($("#title").readOnly) return setRenaming(true);
  await patch({ title: $("#title").value.trim() }); setRenaming(false);
};
$("#title").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); $("#rename").click(); }
  if (e.key === "Escape") { $("#title").value = current.title; setRenaming(false); }
});

$("#del").onclick = async () => {
  if (!sure(`Delete "${current.title || "untitled"}" and its files? This can't be undone.`)) return;
  try { await call("DELETE", `/api/sessions/${current.id}`); } catch (ex) { return alert(ex.message); }
  current = null; await refresh(); drawLog();
};

// ── Files panel: everything this chat carried, newest first ─────────────
// Screen-share frames live in their own panel; a box brings them in here too.

const FILES = Object.assign({ view: "list", screens: false }, local("symposium.files", {}));
const keepFiles = () => keep("symposium.files", { view: FILES.view, screens: FILES.screens });

function drawFiles() {
  drawShots();
  const all = [];
  (current?.messages || []).forEach(m => (m.files || []).forEach(f => all.push({ f, m })));
  all.reverse();
  const shown = all.filter(({ f }) => FILES.screens || !isScreen(f));
  const q = $("#files-q").value.trim().toLowerCase();
  const hits = q ? shown.filter(({ f, m }) => `${f.orig || f.name} ${whoName(m)} ${f.source || ""}`.toLowerCase().includes(q)) : shown;
  $("#files-count").textContent = shown.length || "";
  $("#files").className = `files ${FILES.view}`;
  document.querySelectorAll("#files-view [data-view]").forEach(b => b.classList.toggle("on", b.dataset.view === FILES.view));
  $("#files-screens").checked = FILES.screens;
  if (!current || !hits.length) {
    $("#files").innerHTML = `<p class="muted">${!current ? "Open a chat to see its files."
      : q && shown.length ? "Nothing matches." : "Nothing attached in this chat yet."}</p>`;
    return;
  }
  const sep = (CFG.sessions_dir || "").includes("\\") ? "\\" : "/";
  $("#files").innerHTML = hits.map(({ f, m }) => {
    const url = fileUrl(current.id, f.name), path = `${CFG.sessions_dir}${sep}${current.id}.files${sep}${f.name}`;
    const label = isScreen(f) ? `screen · ${f.source || "shared"}` : (f.orig || f.name);
    const thumb = isImage(f.type) ? `<img class="thumb" src="${esc(url)}" alt="" loading="lazy">` : `<span class="thumb">${icon("file")}</span>`;
    if (FILES.view === "grid") {
      return `<a class="tile" href="${esc(url)}" target="_blank" rel="noopener" data-image="${isImage(f.type) ? 1 : ""}"`
           + ` title="${esc(label)} · ${esc(whoName(m))} · ${esc(when(m.at))} · ${size(f.size || 0)}">${thumb}<span>${esc(label)}</span></a>`;
    }
    return `<a class="file" href="${esc(url)}" target="_blank" rel="noopener" data-image="${isImage(f.type) ? 1 : ""}">`
         + `<span><div class="by${m.role === "user" && !m.source ? " me" : ""}">${esc(whoName(m))}:</div>`
         + `<div class="path" title="${esc(path)}">${esc(label)}</div>`
         + `<div class="when">${esc(when(m.at))} · ${size(f.size || 0)}</div></span>${thumb}</a>`;
  }).join("");
}

$("#files").addEventListener("click", e => {
  const a = e.target.closest(".file, .tile");
  if (a && a.dataset.image) { e.preventDefault(); lightbox(a.href); }
});
$("#files-q").addEventListener("input", drawFiles);
$("#files-view").addEventListener("click", e => {
  const b = e.target.closest("[data-view]");
  if (b) { FILES.view = b.dataset.view; keepFiles(); drawFiles(); }
});
$("#files-screens").onchange = () => { FILES.screens = $("#files-screens").checked; keepFiles(); drawFiles(); };

// Every frame this chat was shown, newest first, in the screen-share panel.
function drawShots() {
  const shots = [];
  (current?.messages || []).forEach(m => (m.files || []).forEach(f => { if (isScreen(f)) shots.push({ f, m }); }));
  shots.reverse();
  $("#shared").hidden = !shots.length;
  $("#shared-count").textContent = shots.length || "";
  $("#shots").innerHTML = shots.map(({ f, m }) =>
    `<img src="${esc(fileUrl(current.id, f.name))}" alt="" loading="lazy" data-shot="${esc(fileUrl(current.id, f.name))}"`
    + ` title="${esc(f.source || "screen")} · ${esc(when(m.at))}">`).join("");
}
$("#shots").addEventListener("click", e => { const i = e.target.closest("[data-shot]"); if (i) lightbox(i.dataset.shot); });

function lightbox(src) { $("#lightbox-img").src = src; $("#lightbox").hidden = false; }
$("#lightbox").onclick = () => { $("#lightbox").hidden = true; $("#lightbox-img").removeAttribute("src"); };
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#lightbox").hidden) $("#lightbox").click(); });

// ── Screen share ────────────────────────────────────────────────────────
// A source - a display (the main one unless you pick another) or, on Windows,
// one window - and a preview of it before anything goes anywhere.
// polling: a fresh still every few seconds; the newest rides along with your
//          next message (shown in the tray first; × skips it), once
// push:    take one, look at it, send it - or take another
// The still is taken by Symposium on this machine (screen.py), not the page.

const SCREEN = Object.assign({ mode: "polling", every: 5, attach: true, source: null },
                             local("symposium.screen", {}),
                             { running: false, timer: null, at: 0, sentAt: 0, label: "", sources: null });
// A window's handle means nothing after a restart; its title is how it is found again.
const saveScreen = () => keep("symposium.screen", { mode: SCREEN.mode, every: SCREEN.every,
                                                     attach: SCREEN.attach, source: SCREEN.source });
const screenOk = () => !!(CFG.screen?.enabled && CFG.screen?.available);

function screenInit() {
  const s = CFG.screen || {};
  const box = $("#p-screen .screen");
  box.dataset.mode = SCREEN.mode;
  document.querySelectorAll('input[name="screen-mode"]').forEach(r => { r.checked = r.value === SCREEN.mode; });
  $("#screen-every").value = SCREEN.every;
  $("#screen-attach").checked = SCREEN.attach;
  box.querySelectorAll("input, select, button").forEach(el => { el.disabled = !screenOk(); });
  $("#screen-note").textContent = !s.enabled ? "Screen share is off (screen.enabled in the yaml)."
    : !s.available ? "Needs mss and Pillow on this machine: pip install \"seren-symposium[screen]\"." : "";
  // Displays come with the config, so the main one is chosen before anything is asked.
  SCREEN.sources = { monitors: s.monitors || [], windows: [],
                     primary: (s.monitors || []).find(m => m.primary)?.index || 1 };
  drawSources();
  if (screenOk()) loadSources();
  drawScreenState();
}

async function loadSources() {
  try { SCREEN.sources = await call("GET", "/api/screen/sources"); } catch { return; }
  drawSources();
}

// The saved choice if it still exists, else the main display.
function resolveSource() {
  const { monitors = [], windows = [], primary = 1 } = SCREEN.sources || {};
  const want = SCREEN.source || {};
  if (want.window != null) {
    const w = windows.find(x => x.id === want.window) || windows.find(x => x.title === want.title);
    if (w) return { window: w.id, title: w.title };
  }
  if (want.monitor != null && (want.monitor === 0 ? monitors.length > 1 : monitors.some(m => m.index === want.monitor))) {
    return { monitor: want.monitor };
  }
  return { monitor: primary };
}

function sourceLabel(src) {
  if (src.window != null) return src.title || "a window";
  return src.monitor === 0 ? "every display" : `display ${src.monitor}`;
}

function drawSources() {
  const { monitors = [], windows = [] } = SCREEN.sources || {};
  SCREEN.source = resolveSource();
  SCREEN.label = sourceLabel(SCREEN.source);
  const mons = monitors.length ? monitors : [{ index: 1, width: 0, height: 0, primary: true }];
  const short = s => s.length > 48 ? s.slice(0, 47) + "…" : s;
  $("#screen-source").innerHTML = `<optgroup label="Displays">`
    + mons.map(m => `<option value="m:${m.index}">Display ${m.index}${m.width ? ` · ${m.width}×${m.height}` : ""}${m.primary ? " · main" : ""}</option>`).join("")
    + (mons.length > 1 ? `<option value="m:0">Every display</option>` : "") + `</optgroup>`
    + (windows.length ? `<optgroup label="Windows">`
       + windows.map(w => `<option value="w:${w.id}" title="${esc(w.title)}">${esc(short(w.title))}</option>`).join("") + `</optgroup>` : "");
  $("#screen-source").value = SCREEN.source.window != null ? `w:${SCREEN.source.window}` : `m:${SCREEN.source.monitor}`;
}

function drawScreenState() {
  $("#screen-state").textContent = SCREEN.running ? `● every ${SCREEN.every}s` : "";
  $("#screen-toggle").textContent = SCREEN.running ? "stop" : "start";
  drawCaption();
}

// What the preview is: only a look, or what goes with your next message.
function drawCaption() {
  const fresh = SCREEN.at > SCREEN.sentAt;
  let say = "";
  if (SCREEN.at) {
    if (SCREEN.mode === "push") say = fresh ? `preview of ${SCREEN.label} - not shared yet` : "sent";
    else if (SCREEN.running && SCREEN.attach) say = fresh ? "goes with your next message" : "sent - a new frame comes next";
    else say = `preview of ${SCREEN.label} - not shared`;
  }
  $("#screen-caption").textContent = say;
  $("#screen-send").disabled = !(screenOk() && SCREEN.mode === "push" && SCREEN.at && fresh && !chatBusy());
}

async function snap() {
  const info = await call("POST", "/api/screen", SCREEN.source.window != null
    ? { window: SCREEN.source.window } : { monitor: SCREEN.source.monitor });
  SCREEN.at = info.at;
  $("#screen-preview").innerHTML = `<img src="/api/screen/latest?t=${info.at}" alt="the newest frame">`;
  drawCaption(); drawTray();
  return info;
}

// A preview whenever the panel opens or the source changes - nothing is sent by looking.
async function look() {
  if (!screenOk()) return;
  try { await snap(); $("#screen-note").textContent = ""; }
  catch (ex) { $("#screen-note").textContent = ex.message; }
}

function screenStop() { clearInterval(SCREEN.timer); SCREEN.timer = null; SCREEN.running = false; drawScreenState(); drawTray(); }
async function screenStart() {
  screenStop();
  SCREEN.running = true; drawScreenState();
  const tick = async () => {
    try { await snap(); $("#screen-note").textContent = ""; }
    catch (ex) { $("#screen-note").textContent = ex.message; screenStop(); }
  };
  await tick();
  if (SCREEN.running) SCREEN.timer = setInterval(tick, Math.max(1, SCREEN.every) * 1000);
  drawTray();
}

// Does the newest frame go with this message?
const screenRides = () => SCREEN.mode === "polling" && SCREEN.running && SCREEN.attach && SCREEN.at > SCREEN.sentAt;

$("#p-screen").addEventListener("toggle", () => {
  if (!$("#p-screen").open) return;
  loadSources();
  if (!SCREEN.at && !SCREEN.running) look();
});
$("#screen-sources").onclick = loadSources;
$("#screen-source").onchange = () => {
  const [kind, v] = $("#screen-source").value.split(":");
  const w = (SCREEN.sources?.windows || []).find(x => String(x.id) === v);
  SCREEN.source = kind === "w" && w ? { window: w.id, title: w.title } : { monitor: Number(v) };
  SCREEN.label = sourceLabel(SCREEN.source);
  saveScreen();
  if (SCREEN.running) screenStart(); else look();
};
document.querySelectorAll('input[name="screen-mode"]').forEach(r => r.addEventListener("change", () => {
  SCREEN.mode = r.value; $("#p-screen .screen").dataset.mode = r.value;
  if (r.value === "push") screenStop();
  saveScreen(); drawCaption(); drawTray();
}));
$("#screen-every").onchange = () => {
  SCREEN.every = Math.min(600, Math.max(1, Number($("#screen-every").value) || 5));
  $("#screen-every").value = SCREEN.every; saveScreen(); if (SCREEN.running) screenStart();
};
$("#screen-attach").onchange = () => { SCREEN.attach = $("#screen-attach").checked; saveScreen(); drawCaption(); drawTray(); };
$("#screen-toggle").onclick = () => SCREEN.running ? screenStop() : screenStart();
$("#screen-look").onclick = look;
$("#screen-send").onclick = () => {
  if (chatBusy() || !SCREEN.at || SCREEN.at <= SCREEN.sentAt) return;
  send({ prompt: $("#input").value.trim(), screen: true });
};
$("#screen-preview").addEventListener("click", e => { if (e.target.tagName === "IMG") lightbox(e.target.src); });

// ── Settings ────────────────────────────────────────────────────────────

function drawVoices() {
  const claude = CFG.claude || {};
  $("#s-voice").innerHTML = `<option value="">project's</option>`
    + `<option value="lodestar">local model (lodestar)</option>`
    + `<option value="claude"${claude.available ? "" : " disabled"}>${esc(voiceName("claude"))}`
    + `${claude.available ? "" : " (not on this machine)"}</option>`;
}

let modelsLoaded = false;
async function loadModels() {
  if (modelsLoaded) return;
  try {
    const { models } = await call("GET", "/api/models");
    $("#models").innerHTML = models.map(m => `<option value="${esc(m)}">`).join("");
    modelsLoaded = true;
  } catch { /* no model list is fine; the field still takes a name */ }
}

// One chat's settings: the sheet behind ⚙ in its title bar.
function drawChatSettings() {
  if (!current) return $("#chat-settings").close();
  loadModels();
  const preset = presetFor(current.folder);
  $("#s-title").value = current.title;
  $("#s-move").innerHTML = [...new Set([...folderNames(), ""])].map(n => `<option value="${esc(n)}">${esc(n || "no project")}</option>`).join("");
  $("#s-move").value = current.folder;
  for (const k of SETTINGS) {
    const el = $(`#s-${k}`), own = current.settings[k], inherited = preset[PRESET[k] || k];
    el.value = own ?? "";
    el.placeholder = inherited != null && inherited !== "" ? String(inherited) : "default";
  }
  $("#s-voice").options[0].textContent = `project's (${voiceName(preset.voice || "lodestar")})`;
}
$("#chat-gear").onclick = () => { if (!current) return; drawChatSettings(); $("#chat-settings").showModal(); };
$("#chat-settings").addEventListener("click", e => { if (e.target === $("#chat-settings")) $("#chat-settings").close(); });

$("#s-title").addEventListener("change", () => patch({ title: $("#s-title").value.trim() }).then(drawLog));
$("#s-move").addEventListener("change", async () => {
  await patch({ folder: $("#s-move").value });
  folder = current.folder; drawChatSettings(); drawLog();
});
for (const k of SETTINGS) {
  $(`#s-${k}`).addEventListener("change", e => {
    const raw = String(e.target.value).trim();
    let v = raw === "" ? null : raw;
    if (v !== null && NUMBER.has(k)) {
      v = k === "max_tokens" ? parseInt(v, 10) : parseFloat(v);
      if (Number.isNaN(v)) return drawChatSettings();
    }
    patch({ settings: { [k]: v } }).catch(ex => { alert(ex.message); drawChatSettings(); });
  });
}

// The Settings tab is the whole app's: who's here, the backends, the projects.
let OPEN_PROJECT = null;
function drawSettings() {
  loadModels();
  $("#a-you").value = CFG.you || "";
  $("#a-claude_name").value = CFG.claude?.name || "";
  $("#a-confirm_destructive").checked = !!CFG.confirm_destructive;
  $("#a-prefs-file").textContent = CFG.prefs_file || "";
  drawProjects(); drawAbout(); drawClaude();
}

// Claude: how the voice is asked about tools, what it may use, where it may
// work - and the carabiner clipped on this box, beside it.
let CLAUDE = null;
async function drawClaude() {
  try { CLAUDE = await call("GET", "/api/claude"); } catch { return; }
  const k = CLAUDE.carabiner;
  $("#c-title").textContent = k?.carabiner ? `· carabiner: ${k.carabiner}` : "";
  const rows = [["works in", CLAUDE.cwd || "Symposium's own folder"]];
  if (k?.error) rows.push(["carabiner", `${k.path} - ${k.error}`]);
  else if (k) {
    rows.push(["clipped at", k.path], ["its sessions work in", k.project || "-"]);
    for (const [name, url] of Object.entries(k.servers || {})) rows.push([`routes ${name}`, url]);
    if (k.bookmark) rows.push(["bookmark (Margin)", k.bookmark]);
  } else rows.push(["carabiner", "none clipped on this box"]);
  $("#c-facts").innerHTML = rows.map(([a, b]) => `<dt>${esc(a)}</dt><dd>${esc(b)}</dd>`).join("");
  $("#c-mode").value = CLAUDE.mode || "";
  $("#c-ask").checked = !!CLAUDE.ask;
  const chips = (list, key) => list.map((v, i) =>
    `<span class="pending rule"><code>${esc(v)}</code><button type="button" data-unlist="${key}" data-i="${i}" title="take it off">×</button></span>`).join("")
    || `<span class="muted">none</span>`;
  $("#c-tools").innerHTML = chips(CLAUDE.tools || [], "claude_tools");
  $("#c-dirs").innerHTML = chips(CLAUDE.dirs || [], "claude_dirs");
}

async function setClaude(changes) {
  try { await call("PATCH", "/api/prefs", changes); } catch (ex) { alert(ex.message); }
  await reloadConfig(); drawClaude();
}
$("#c-mode").onchange = () => setClaude({ claude_mode: $("#c-mode").value });
$("#c-ask").onchange = () => setClaude({ claude_ask: $("#c-ask").checked });
$("#set-claude").addEventListener("click", e => {
  const x = e.target.closest("[data-unlist]");
  if (!x) return;
  const key = x.dataset.unlist, list = [...(key === "claude_tools" ? CLAUDE.tools : CLAUDE.dirs)];
  list.splice(Number(x.dataset.i), 1);
  setClaude({ [key]: list });
});
for (const [box, key] of [["#c-tool-add", "claude_tools"], ["#c-dir-add", "claude_dirs"]]) {
  $(box).addEventListener("keydown", e => {
    if (e.key !== "Enter" || !e.target.value.trim()) return;
    e.preventDefault();
    const list = [...(key === "claude_tools" ? CLAUDE.tools : CLAUDE.dirs), e.target.value.trim()];
    e.target.value = "";
    setClaude({ [key]: list });
  });
}

for (const k of ["you", "claude_name", "confirm_destructive"]) {
  $(`#a-${k}`).addEventListener("change", async e => {
    const v = e.target.type === "checkbox" ? e.target.checked : e.target.value.trim();
    try { await call("PATCH", "/api/prefs", { [k]: v }); } catch (ex) { alert(ex.message); }
    await reloadConfig(); drawSettings(); drawSide(); if (current) drawLog();
  });
}

$("#p-add").onclick = async () => {
  const name = (prompt("Name the new project:") || "").trim();
  if (!name) return;
  try { OPEN_PROJECT = name; await newProject(name); } catch (ex) { alert(ex.message); }
};

const PROJECT_KEYS = ["voice", "system_prompt", "model", "temperature", "max_tokens", "repeat_penalty"];
function drawProjects() {
  const names = folderNames().filter(Boolean);
  $("#set-projects").innerHTML = names.map(name => {
    const f = presetFor(name), count = SESSIONS.filter(s => s.folder === name).length;
    const bits = [voiceName(f.voice || "lodestar"), f.model, f.temperature != null && `t=${f.temperature}`,
                  `${count} chat${count === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
    const val = k => esc(f[k] ?? "");
    return `<details class="project" data-p="${esc(name)}"${OPEN_PROJECT === name ? " open" : ""}>`
      + `<summary>${icon("krater-full")}<span class="p-name">${esc(name)}</span><span class="p-sub">${esc(bits)}</span></summary>`
      + `<div class="form">`
      + `<label>name</label><input data-k="name" value="${esc(name)}" maxlength="120">`
      + `<label>voice</label><select data-k="voice"><option value="">local model (lodestar)</option>`
      + `<option value="claude"${f.voice === "claude" ? " selected" : ""}${CFG.claude?.available ? "" : " disabled"}>${esc(voiceName("claude"))}</option></select>`
      + `<label>system prompt</label><textarea data-k="system_prompt">${val("system_prompt")}</textarea>`
      + `<label>model</label><input data-k="model" list="models" value="${val("model")}" placeholder="the default">`
      + `<label>temperature</label><input data-k="temperature" type="number" step="0.05" min="0" max="2" value="${val("temperature")}" placeholder="default">`
      + `<label>max tokens</label><input data-k="max_tokens" type="number" step="64" min="1" value="${val("max_tokens")}" placeholder="default">`
      + `<label>repeat penalty</label><input data-k="repeat_penalty" type="number" step="0.05" min="0" value="${val("repeat_penalty")}" placeholder="default">`
      + `<span></span><div class="row-buttons"><button type="button" class="ghost danger" data-remove="1">delete project</button></div>`
      + `</div></details>`;
  }).join("") || `<p class="muted">No projects yet - chats can live without one.</p>`;
}

$("#set-projects").addEventListener("toggle", e => {
  const d = e.target.closest?.("details.project");
  if (d) OPEN_PROJECT = d.open ? d.dataset.p : (OPEN_PROJECT === d.dataset.p ? null : OPEN_PROJECT);
}, true);
$("#set-projects").addEventListener("change", async e => {
  const el = e.target.closest("[data-k]"), d = e.target.closest("details.project");
  if (!el || !d) return;
  const name = d.dataset.p, k = el.dataset.k, raw = String(el.value).trim();
  try {
    if (k === "name") { if (raw && raw !== name) await renameProject(name, raw); else drawProjects(); return; }
    let v = raw === "" ? null : raw;
    if (v !== null && NUMBER.has(k)) {
      v = k === "max_tokens" ? parseInt(v, 10) : parseFloat(v);
      if (Number.isNaN(v)) return drawProjects();
    }
    await call("PATCH", "/api/projects", { name, set: { [k]: v } });
    await reloadConfig(); drawProjects(); drawSide();
  } catch (ex) { alert(ex.message); drawProjects(); }
});
$("#set-projects").addEventListener("click", e => {
  const d = e.target.closest("details.project");
  if (d && e.target.closest("[data-remove]")) deleteProject(d.dataset.p);
});

$("#export").onclick = () => {
  const blob = new Blob([JSON.stringify(current, null, 1)], { type: "application/json" });
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(blob),
    download: `${(current.title || "session").replace(/[^\w.-]+/g, "_").slice(0, 60)}.json` });
  a.click(); URL.revokeObjectURL(a.href);
};
$("#import").onclick = () => $("#importer").click();
$("#importer").onchange = async () => {
  const file = $("#importer").files[0]; $("#importer").value = "";
  if (!file) return;
  try {
    current = await call("POST", "/api/sessions", JSON.parse(await file.text()));
    folder = current.folder; await refresh(); drawLog(); show("chat");
  } catch (ex) { alert(`Could not import ${file.name}: ${ex.message}`); }
};

function drawAbout() {
  if (!CFG) return;
  const lod = HEALTH?.lodestar, u = CFG.updates || {};
  const rows = [
    ["version", CFG.version + (u.update_available ? `  (${u.latest} is out)` : "")],
    ["lodestar", `${CFG.lodestar} · ${lod ? `${lod.status}${lod.detail ? ` - ${lod.detail}` : ""}` : "checking"}`],
    ["claude voice", CFG.claude?.available ? `${CFG.claude.name}, on this machine` : "not on this machine"],
    ["you are", CFG.you],
    ["chats live in", CFG.sessions_dir],
    ["window settings", CFG.prefs_file],
    ["attachments", `up to ${CFG.files?.max_mb} MB each; web images in answers ${CFG.files?.fetch_images ? "are saved into the chat" : "stay links"}`],
    ["screen share", !CFG.screen?.available ? "needs mss and Pillow"
      : CFG.screen.enabled ? `${(CFG.screen.monitors || []).length} screen(s)` : "off"],
  ];
  $("#set-about").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

// ── Backend: a card per sibling service, with its widget ────────────────
// A glance, not a second copy of each service: the card says whether it
// answers and how fast, and clicking it opens that service's own page in
// your browser (a new-window link leaves the app - pywebview hands it to the
// system browser), where its own sign-in keeps you. The widget is a ping from
// this page (no-cors: it says whether something answered and how fast, never
// what) - Symposium itself still talks to nobody but Lodestar.

const FAMILY = {
  memory:      ["Memory", "#ff6e8a", "episodes, as they happen"],
  loci:        ["Loci", "#5bc8e8", "facts, each with a why"],
  callosum:    ["CorpusCallosum", "#9d7cff", "one search across both"],
  hippocampus: ["Hippocampus", "#c9a0dc", "sleep: drafts and consolidation"],
  tools:       ["Workbench", "#8e9aaf", "the one door to the tools"],
  cluster:     ["Lodestar", "#F5D76E", "routing, nodes, the cluster"],
  probe:       ["Probe", "#8fffb4", "RAG evaluation"],
};
const PINGS = {};   // url -> [ms | null, ...], newest last

function serviceList() {
  const groups = [], loose = [];
  for (const [name, v] of Object.entries(CFG.viewers || {})) {
    if (typeof v === "object") groups.push([name, Object.entries(v)]);
    else loose.push([name, v]);
  }
  return [...groups, ["services", loose]].filter(g => g[1].length);
}

function lodestarHost() { try { return new URL(CFG.lodestar).host; } catch { return "\0"; } }

function drawServices() {
  const chats = SESSIONS.length, projects = folderNames().length;
  $("#services").innerHTML = serviceList().map(([group, items]) =>
    `<div class="group-name">${esc(group)}</div><div class="svc-grid">` + items.map(([name, url]) => {
      const [proper, color, what] = FAMILY[name] || [name, "", ""];
      let host = ""; try { host = new URL(url).host; } catch {}
      return `<a class="svc" href="${esc(url)}" target="_blank" rel="noopener" title="open ${esc(proper)} in your browser"${color ? ` style="--svc:${esc(color)}"` : ""}>`
           + `<span class="s-head"><span class="s-name">Seren<b>${esc(proper)}</b></span><span class="s-host">${esc(host)} ↗</span></span>`
           + `<span class="s-detail">${esc(what)}</span>`
           + `<span class="s-state" data-state="${esc(url)}"><span class="dot"></span><span>checking</span></span>`
           + `<span class="spark" data-spark="${esc(url)}"></span></a>`;
    }).join("") + `</div>`).join("")
    + `<div class="group-name">here</div><div class="svc-grid">`
    + `<div class="svc static"><span class="s-head"><span class="s-name">Claude voice</span></span>`
    + `<span class="s-detail">${CFG.claude?.available ? `${esc(CFG.claude.name)} answers in chats set to it` : "the claude CLI is not on this machine"}</span>`
    + `<span class="s-state"><span class="dot ${CFG.claude?.available ? "up" : "down"}"></span><span>${CFG.claude?.available ? "here" : "missing"}</span></span></div>`
    + `<div class="svc static"><span class="s-head"><span class="s-name">Chats</span></span>`
    + `<span class="s-detail">${chats} chat${chats === 1 ? "" : "s"} in ${projects} project${projects === 1 ? "" : "s"}</span>`
    + `<span class="s-state"><span>${esc(CFG.sessions_dir)}</span></span></div></div>`;
  drawPings();
}

async function ping(url) {
  const ctl = new AbortController(), t0 = performance.now();
  const timer = setTimeout(() => ctl.abort(), 4000);
  try { await fetch(url, { mode: "no-cors", cache: "no-store", signal: ctl.signal }); return Math.round(performance.now() - t0); }
  catch { return null; }
  finally { clearTimeout(timer); }
}

async function pingAll() {
  const urls = serviceList().flatMap(([, items]) => items.map(([, url]) => url));
  await Promise.all(urls.map(async url => { PINGS[url] = [...(PINGS[url] || []), await ping(url)].slice(-16); }));
  drawPings();
}
setInterval(() => { if (tab === "backend") pingAll(); }, 10000);

function drawPings() {
  document.querySelectorAll("[data-state]").forEach(el => {
    const url = el.dataset.state, seen = PINGS[url];
    if (!seen) return;
    const ms = seen.at(-1);
    let label = ms == null ? "not answering" : `up · ${ms} ms`, cls = ms == null ? "down" : "up";
    // Lodestar's widget knows more than a ping: whether a model is serving.
    let why = "";
    if (url.includes(lodestarHost()) && HEALTH?.lodestar && ms != null) {
      const [c, word] = STATUS_LABEL[HEALTH.lodestar.status] || ["warn", HEALTH.lodestar.status];
      cls = c; label += ` · ${word}`; why = HEALTH.lodestar.detail || "";
    }
    el.innerHTML = `<span class="dot ${cls}"></span><span>${esc(label)}</span>`;
    el.title = why;
  });
  document.querySelectorAll("[data-spark]").forEach(el => {
    const seen = PINGS[el.dataset.spark] || [], top = Math.max(50, ...seen.filter(x => x != null));
    el.innerHTML = seen.map(ms => ms == null ? `<i class="miss"></i>`
      : `<i style="height:${Math.max(3, Math.round(18 * ms / top))}px" title="${ms} ms"></i>`).join("");
  });
}


boot();

// ── The picture panel ───────────────────────────────────────────────────
// Words -> translate (a local model writes the prompt in the chosen model's
// style) -> plan (Lodestar says what it would draw, and where) -> tweak any
// of it -> draw. The picture lands in this chat as a turn and a file, at
// full size. Lodestar plans and draws; this panel asks, shows and keeps.
const PIC = Object.assign({ model: "", size: "", loras: {} }, local("symposium.picture", {}));
const savePic = () => keep("symposium.picture", { model: PIC.model, size: PIC.size, loras: PIC.loras });
let PIC_OPTIONS = null, PIC_LORAS = [], PIC_PLAN = null;

const picEl = id => $("#pic-" + id);
const picNote = (text, bad = false) => { picEl("note").textContent = text || ""; picEl("note").classList.toggle("bad", !!bad); };
const picState = text => { picEl("state").textContent = text || ""; };

function picModel() { return PIC_OPTIONS?.models.find(m => m.key === PIC.model || m.name === PIC.model) || null; }

async function picLoadOptions() {
  picNote("looking at the cluster…");
  try {
    PIC_OPTIONS = await call("GET", "/api/pictures/options");
    const sel = picEl("model");
    sel.innerHTML = `<option value="">any model that can draw it</option>` + PIC_OPTIONS.models.map(m =>
      `<option value="${esc(m.name)}" ${m.drawable_on.length ? "" : "disabled"}>${esc(m.name)}`
      + ` · ${esc(m.family || "?")}${m.base ? " / " + esc(m.base) : ""}${m.drawable_on.length ? "" : " (no node draws it)"}</option>`).join("");
    sel.value = PIC.model && PIC_OPTIONS.models.some(m => m.name === PIC.model) ? PIC.model : "";
    PIC.model = sel.value;
    picNote(`${PIC_OPTIONS.models.filter(m => m.drawable_on.length).length} models can be drawn on `
      + `${PIC_OPTIONS.nodes.map(n => n.node).join(", ") || "no node"}`);
    picState(PIC_OPTIONS.nodes.length ? `${PIC_OPTIONS.nodes.length} node${PIC_OPTIONS.nodes.length === 1 ? "" : "s"}` : "no image node");
    await picModelChanged();
  } catch (ex) {
    PIC_OPTIONS = null;
    picNote(ex.body?.error || ex.message, true);
    picState("unavailable");
  }
  picEl("draw").disabled = !PIC_OPTIONS;
}

async function picModelChanged() {
  const m = picModel();
  picEl("guide").textContent = m?.style?.guide ? `${m.style.style}: ${m.style.guide}` : "";
  PIC_LORAS = [];
  picEl("loras").innerHTML = "";
  picEl("loras-hint").textContent = m ? "loading…" : "pick a model first";
  if (!m) return;
  try {
    const got = await call("GET", `/api/pictures/options?model=${encodeURIComponent(m.name)}`);
    PIC_LORAS = got.loras || [];
    picEl("loras-hint").textContent = PIC_LORAS.length ? "click to wear one; the number is its weight" : "none fit this model";
    const worn = PIC.loras[m.name] || {};
    picEl("loras").innerHTML = PIC_LORAS.map(l => {
      const on = worn[l.name] != null;
      return `<span class="lora${on ? " on" : ""}" data-lora="${esc(l.name)}" title="${esc(l.fit || "")}">${esc(l.name.replace(/\.safetensors$/i, ""))}`
        + `${l.fit ? ` <span class="fit">${esc(l.fit)}</span>` : ""}`
        + `<input type="number" step="0.05" min="-2" max="2" value="${on ? worn[l.name] : (l.weight ?? 0.8)}" ${on ? "" : "hidden"}></span>`;
    }).join("");
  } catch (ex) { picEl("loras-hint").textContent = ex.body?.error || ex.message; }
}

function picRequest() {
  // what Lodestar is asked for: the prompt as edited, the model, what is worn
  const req = { prompt: picEl("prompt").value.trim() || picEl("text").value.trim() };
  const m = picModel();
  if (m) req.model = m.name;
  const neg = picEl("negative").value.trim();
  if (neg) req.negative = neg;
  if (PIC.size) { const [w, h] = PIC.size.split("x").map(Number); req.width = w; req.height = h; }
  const seed = picEl("seed").value.trim();
  if (seed) req.seed = Number(seed);
  const count = Number(picEl("count").value || 1);
  if (count > 1) req.count = count;
  const worn = m ? Object.entries(PIC.loras[m.name] || {}) : [];
  if (worn.length) req.loras = worn.map(([name, weight]) => ({ name, weight }));
  return req;
}

async function picTranslate() {
  const text = picEl("text").value.trim();
  if (!text) { picNote("say what you want to see first", true); return; }
  const m = picModel();
  const button = picEl("translate");
  button.disabled = true; picNote("writing the prompt…");
  try {
    // whoever answers this chat writes the draft: Claude in a Claude chat, the chat's own model otherwise
    const got = await call("POST", "/api/pictures/translate", { text, guide: m?.style?.guide || "", session: current?.id, folder });
    picEl("prompt").value = got.prompt || "";
    picEl("negative").value = got.negative || "";
    const by = got.voice === "claude" ? voiceName("claude") : (got.model || "a local model");
    picNote(`drafted by ${by}${got.node && got.voice !== "claude" ? " on " + got.node : ""} - edit it as you like`);
  } catch (ex) { picNote(ex.body?.error || ex.message, true); }
  finally { button.disabled = false; }
}

async function picPlan() {
  const req = picRequest();
  if (!req.prompt) { picNote("a prompt first: translate, or write one", true); return; }
  picNote("planning…");
  try {
    PIC_PLAN = await call("POST", "/api/pictures/plan", req);
    const p = PIC_PLAN;
    picEl("plan-box").hidden = false;                         // stays folded; the summary line says the gist
    picEl("plan-sum").textContent = `${p.model?.name || p.model || "?"} on ${p.node}${p.also?.length ? " (or " + p.also.join(", ") + ")" : ""}`;
    const rows = [["node", p.node], ["model", p.model?.name || p.model], ["family", p.arch],
                  ["size", p.width && p.height ? `${p.width} × ${p.height}` : null],
                  ["loras", (p.loras || []).map(l => `${l.name} ${l.weight ?? ""}`).join(", ") || "none"],
                  ["seed", p.seed], ["steps", p.settings ? `${p.settings.steps} · cfg ${p.settings.cfg} · ${p.settings.sampler}` : null],
                  ["prompt", p.prompt], ["negative", p.negative], ["notes", (p.notes || []).join("; ") || null]];
    picEl("plan-facts").innerHTML = rows.filter(([, v]) => v != null && v !== "").map(([k, v]) => `<div class="row"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join("");
    picNote(`plan: ${p.node} will draw it with ${p.model?.name || p.model}`);
  } catch (ex) { PIC_PLAN = null; picEl("plan-box").hidden = true; picNote(ex.body?.error || ex.message, true); }
}

async function picDraw() {
  if (chatBusy()) return;
  const req = picRequest();
  if (!req.prompt) { picNote("a prompt first: translate, or write one", true); return; }
  const asked = picEl("text").value.trim() || req.prompt;
  drawing = true; lockComposer(); picEl("draw").disabled = true;
  $("#chat .pic").classList.add("busy"); picNote("drawing… this can take a minute or two");
  try {
    if (!current) { current = await call("POST", "/api/sessions", { folder }); await refresh(); }
    const got = await call("POST", "/api/pictures/draw", { session: current.id, asked, why: asked, request: req });
    await openSession(current.id);
    picNote(`drawn on ${got.node} with ${got.model?.name || got.model || "?"} in ${got.seconds ?? "?"}s · seed ${got.seed}`);
    if (got.seed != null) picEl("seed").placeholder = `last: ${got.seed}`;
  } catch (ex) {
    picNote([ex.body?.error, ex.body?.detail].filter(Boolean).join(" - ") || ex.message, true);
  } finally {
    drawing = false; lockComposer(); picEl("draw").disabled = !PIC_OPTIONS;
    $("#chat .pic").classList.remove("busy");
  }
}

$("#picture-btn").onclick = () => {
  const p = $("#p-picture");
  p.open = true;
  if (!PIC_OPTIONS) picLoadOptions();
  picEl("text").focus();
  p.scrollIntoView({ block: "nearest" });
};
$("#p-picture").addEventListener("toggle", () => { if ($("#p-picture").open && !PIC_OPTIONS) picLoadOptions(); });
picEl("reload").onclick = picLoadOptions;
picEl("model").onchange = async () => { PIC.model = picEl("model").value; savePic(); await picModelChanged(); };
picEl("size").onchange = () => { PIC.size = picEl("size").value; savePic(); };
picEl("size").value = PIC.size || "";
picEl("translate").onclick = picTranslate;
picEl("plan").onclick = picPlan;
picEl("draw").onclick = picDraw;
picEl("text").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); picTranslate(); } });
picEl("loras").addEventListener("click", e => {
  const chip = e.target.closest(".lora");
  if (!chip || e.target.tagName === "INPUT") return;
  const m = picModel(); if (!m) return;
  const worn = PIC.loras[m.name] = PIC.loras[m.name] || {};
  const input = chip.querySelector("input");
  if (chip.classList.toggle("on")) { worn[chip.dataset.lora] = Number(input.value); input.hidden = false; }
  else { delete worn[chip.dataset.lora]; input.hidden = true; }
  savePic();
});
picEl("loras").addEventListener("change", e => {
  if (e.target.tagName !== "INPUT") return;
  const chip = e.target.closest(".lora"), m = picModel();
  if (chip && m && chip.classList.contains("on")) { (PIC.loras[m.name] = PIC.loras[m.name] || {})[chip.dataset.lora] = Number(e.target.value); savePic(); }
});

// ── Ears: push-to-talk ──────────────────────────────────────────────────
// The microphone is recorded in Python (sounddevice), like the screen; on
// release the clip is kept in the chat and goes to whisper through Lodestar.
// The words land in the composer for you to send or fix; the clip sits in
// the tray and rides with the message, so what was said is not only what
// whisper made of it.
const EARS = Object.assign({ attach: true, device: null },
                           local("symposium.ears", {}),
                           { live: false, by: "", since: 0, timer: null, last: "" });
const saveEars = () => keep("symposium.ears", { attach: EARS.attach, device: EARS.device });
const earsOk = () => !!(CFG.ears?.enabled && CFG.ears?.available);

function earsInit() {
  const e = CFG.ears || {};
  $("#ears-attach").checked = EARS.attach;
  $("#ears-note").textContent = !e.enabled ? "Ears are off (ears.enabled in the yaml)."
    : !e.available ? "Needs sounddevice on this machine: pip install \"seren-symposium[ears]\"." : "";
  drawEarsDevices(e.devices || []);
  $("#ears-btn").disabled = !earsOk();
  $("#p-ears").querySelectorAll("select, input, button").forEach(el => { el.disabled = !earsOk(); });
  drawEarsState();
}

// The saved choice by name (an index moves when something is plugged in),
// else what the yaml names, else the default input.
function drawEarsDevices(list) {
  const sel = $("#ears-device");
  sel.innerHTML = list.map(d => `<option value="${d.index}">${esc(d.name)}${d.default ? " · default" : ""}</option>`).join("")
    || `<option value="">no input found</option>`;
  const want = EARS.device && list.find(d => d.name === EARS.device);
  const yaml = CFG.ears?.device && list.find(d => d.name.toLowerCase().includes(CFG.ears.device.toLowerCase()));
  const pick = want || yaml || list.find(d => d.default) || list[0];
  if (pick) sel.value = String(pick.index);
}
const earsDevice = () => { const v = $("#ears-device").value; return v === "" ? undefined : Number(v); };

function drawEarsState() {
  const b = $("#ears-btn");
  b.classList.toggle("live", EARS.live);
  b.textContent = EARS.live ? `● listening ${Math.round((Date.now() - EARS.since) / 1000)}s` : "🎙 hold to talk";
  $("#ears-state").textContent = EARS.live ? "● listening" : "";
  $("#ears-caption").textContent = EARS.live ? "release to hear it - Esc drops it" : EARS.last;
}

async function earsStart(by) {
  if (EARS.live || !earsOk() || chatBusy()) return;
  try {
    // A chat exists before the clip so it has somewhere to be kept.
    if (!current) { current = await call("POST", "/api/sessions", { folder }); await refresh(); }
    await call("POST", "/api/ears/start", { device: earsDevice() });
    EARS.live = true; EARS.by = by; EARS.since = Date.now();
    EARS.timer = setInterval(drawEarsState, 500);
    $("#ears-note").textContent = "";
    drawEarsState();
  } catch (ex) { $("#ears-note").textContent = ex.message; }
}

async function earsStop(drop = false) {
  if (!EARS.live) return;
  clearInterval(EARS.timer); EARS.timer = null; EARS.live = false; drawEarsState();
  if (drop) {
    try { await call("POST", "/api/ears/cancel"); } catch {}
    EARS.last = "dropped"; drawEarsState(); return;
  }
  $("#ears-caption").textContent = "hearing…";
  try {
    const out = await call("POST", "/api/ears/stop", { session: current?.id, keep: EARS.attach });
    if (out.text) {
      const box = $("#input"), sep = box.value && !/\s$/.test(box.value) ? " " : "";
      box.value += sep + out.text; box.focus();
    }
    if (out.file && current) {
      PENDING.push({ id: uid(), name: out.file.orig, size: out.file.size, type: out.file.type, stored: out.file });
      drawTray();
    }
    const short = s => s.length > 90 ? s.slice(0, 89) + "…" : s;
    EARS.last = out.error ? `${out.file ? "kept the clip, but" : "but"}: ${out.error}`
      : out.text ? `heard: ${short(out.text)}` : `heard nothing (${out.seconds}s)`;
  } catch (ex) { EARS.last = ex.message; }
  drawEarsState();
}

$("#ears-btn").addEventListener("pointerdown", e => { e.preventDefault(); e.currentTarget.setPointerCapture(e.pointerId); earsStart("mouse"); });
$("#ears-btn").addEventListener("pointerup", () => { if (EARS.by === "mouse") earsStop(); });
$("#ears-btn").addEventListener("pointercancel", () => { if (EARS.by === "mouse") earsStop(true); });
// Ctrl+Space, held, anywhere in the window: the same from the keyboard.
document.addEventListener("keydown", e => {
  if (e.code === "Space" && e.ctrlKey && !e.repeat) { e.preventDefault(); earsStart("key"); }
  else if (e.key === "Escape" && EARS.live) earsStop(true);
});
document.addEventListener("keyup", e => { if (e.code === "Space" && EARS.live && EARS.by === "key") earsStop(); });
$("#ears-attach").onchange = () => { EARS.attach = $("#ears-attach").checked; saveEars(); };
$("#ears-device").onchange = () => { EARS.device = $("#ears-device").selectedOptions[0]?.textContent.replace(/ · default$/, "") || null; saveEars(); };
$("#ears-devices").onclick = async () => {
  try { drawEarsDevices((await call("GET", "/api/ears")).devices || []); } catch (ex) { $("#ears-note").textContent = ex.message; }
};
