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

const SETTINGS = ["voice", "system_prompt", "model_override", "temperature", "max_tokens", "repeat_penalty", "cwd", "queue", "interrupt", "pictures", "my_pictures"];
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
// A new chat's own settings and name, set (⚙) before its first message: kept
// here, sent with that message, never an empty chat on the server meanwhile.
let DRAFT = { settings: {}, title: "" };
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
// The one live stream this window is reading, if any: {sid, ctrl, el, asked,
// retry} - the answer's bubble and the question's, so a redraw of the chat
// mid-answer puts both back instead of losing them.
let FOLLOWER = null;
// The question drawn for a turn found running when its chat was opened.
let ASKED_EL = null;

// ── What a chat has running behind it ───────────────────────────────────
// A command a turn started in the background outlives the answer (the
// server's claude.background): the chat is free, the conversation can still
// be asked things, and when the task ends it says so in a turn of its own.
// Chad, 19 Nov 2026: "a task is kicked off, and set to the side ... it runs
// in the background, and then alerts when done. because a frozen seat not
// being able to answer a question would suck". Here: a line of what is
// running and whose it is, with × to end one; and while anything is, the
// chat is looked at again every few seconds, so what it says when it ends
// turns up without being asked for.
let BEHIND_TIMER = null;
function drawBehind() {
  const running = current?.background || [];
  $("#behind").hidden = !running.length;
  $("#behind").innerHTML = running.map(b => {
    const who = b.seat ? seatWho(b.seat) : voiceName("claude");
    // Each task opens on a click: what it ran, how long, its output so far (23 Nov 2026).
    const details = (b.details || []).filter(d => d.id);
    const what = details.length
      ? details.map(d => `<button type="button" class="task-open" data-btask="${esc(d.id)}" title="what it is running, and its output so far">`
          + `${esc(d.description || d.type || "a task")} <i>${esc(howLong(d.since))}</i></button>`).join(" · ")
      : esc((b.tasks || []).join(" · ") || "a task");
    return `<span class="task" title="${esc(who)} left this running; the chat is not held by it, and ${esc(who)} says so when it ends">`
      + `<b>running</b><span>${esc(who)}: ${what}</span>`
      + `<button type="button" data-bstop="${esc(b.seat || "")}" title="end it now - nobody is woken">×</button></span>`;
  }).join("");
  if (running.length && !BEHIND_TIMER) BEHIND_TIMER = setInterval(lookBehind, 4000);
  if (!running.length && BEHIND_TIMER) { clearInterval(BEHIND_TIMER); BEHIND_TIMER = null; }
}
async function lookBehind() {
  if (!current || chatBusy() || FOLLOWER || OPENING) return;       // an answer coming redraws the chat itself
  const sid = current.id;
  let s;
  try { s = await call("GET", `/api/sessions/${encodeURIComponent(sid)}`); } catch { return; }
  if (current?.id !== sid || chatBusy() || FOLLOWER || OPENING) return;
  const was = JSON.stringify(current.background || []), now = JSON.stringify(s.background || []);
  if (s.messages.length !== current.messages.length || s.answering) return openSession(sid);   // it came back, or someone is answering
  current.background = s.background;
  if (was !== now || (s.background || []).length) drawBehind();     // the "running for" ticks on
}
// How long since `t` (seconds), in the fewest words: 40s, 12m, 5h 12m, 2d 3h.
function howLong(t) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - Number(t || 0)));
  if (!t) return "";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
  return d ? `${d}d ${h % 24}h` : h ? `${h}h ${m % 60}m` : `${m}m`;
}

// A task left running, looked into: refreshed every few seconds while open.
let TASK_OPEN = null, TASK_TIMER = null;
async function drawTask() {
  if (!TASK_OPEN || !current) return;
  const [sid, id] = TASK_OPEN;
  let t;
  try { t = await call("GET", `/api/sessions/${encodeURIComponent(sid)}/background/${encodeURIComponent(id)}`); }
  catch (ex) {
    $("#tk-body").innerHTML = `<p class="muted">${esc(ex.message)}</p>`;
    clearInterval(TASK_TIMER); TASK_TIMER = null; return;
  }
  if (!TASK_OPEN || TASK_OPEN[1] !== id) return;
  const who = t.seat ? seatWho(t.seat) : voiceName("claude");
  $("#tk-title").textContent = t.description || t.type || "Running";
  const by = t.started_by, o = t.output;
  const box = $("#tk-body"), out = box.querySelector(".tk-out");
  const stuck = out && out.scrollTop + out.clientHeight >= out.scrollHeight - 8;   // following the end: keep following
  box.innerHTML = `<dl class="tk-facts"><dt>who</dt><dd>${esc(who)}</dd>`
    + `<dt>running for</dt><dd>${esc(howLong(t.since))} <span class="muted">(since ${esc(new Date(t.since * 1000).toLocaleString())})</span></dd>`
    + `<dt>kind</dt><dd><code>${esc(t.type || "?")}</code> · id <code>${esc(t.id)}</code></dd>`
    + (o ? `<dt>last wrote</dt><dd>${esc(howLong(o.written_at))} ago · ${(o.size / 1024).toFixed(1)} KB</dd>` : "")
    + `</dl>`
    + (by ? `<h4>what it ran (${esc(by.tool)})</h4><pre class="sf-pre">${esc(by.input?.command || JSON.stringify(by.input, null, 2))}</pre>` : "")
    + (o ? `<h4>its output so far${o.cut ? " (the end of it)" : ""}</h4><pre class="sf-pre tk-out">${esc(o.tail || "(nothing yet)")}</pre>`
         + `<p class="muted tk-path">${esc(o.path)}</p>`
         : `<p class="muted">No output file for this one - an agent task keeps its words in its own transcript.</p>`);
  const fresh = box.querySelector(".tk-out");
  if (fresh && (stuck || !out)) fresh.scrollTop = fresh.scrollHeight;
}
function openTask(id) {
  if (!current) return;
  TASK_OPEN = [current.id, id];
  $("#tk-body").innerHTML = `<p class="muted">looking…</p>`;
  if (!$("#task-sheet").open) $("#task-sheet").showModal();
  drawTask();
  clearInterval(TASK_TIMER); TASK_TIMER = setInterval(drawTask, 3000);
}
$("#task-sheet").addEventListener("close", () => { TASK_OPEN = null; clearInterval(TASK_TIMER); TASK_TIMER = null; });
$("#tk-refresh").onclick = () => drawTask();

$("#behind").onclick = async e => {
  const open = e.target.closest("[data-btask]");
  if (open) return openTask(open.dataset.btask);
  const x = e.target.closest("[data-bstop]");
  if (!x || !current) return;
  x.disabled = true;
  try {
    const r = await call("POST", `/api/sessions/${encodeURIComponent(current.id)}/background/stop`, { seat: x.dataset.bstop || null });
    current.background = r.background;
  } catch (ex) { alert(ex.message); }
  drawBehind();
};
// One follower per answer. 18 Nov 2026, the first tangle on 5.7.0: a message
// sent while a round was running waited its turn, and went the instant the
// round ended - while the window was still reopening the chat for that round.
// The reopen came back, found an answer coming (the one just sent) and
// followed it too: two live bubbles for one turn, both ticking. So: nothing
// waiting is sent while a chat is being reopened (OPENING), and a chat this
// window is itself sending into, or already following, is never followed
// a second time (SENDING, FOLLOWER).
let OPENING = 0;
const SENDING = new Set();
const PLACEHOLDER = $("#input").placeholder;
const chatBusy = () => drawing || (current != null && ANSWERING.has(current.id));
// How what is sent mid-answer goes once the answer is in: this chat's own
// setting, else every chat's (Settings, "sent mid-answer"), else meld.
const QUEUE_NAMES = { meld: "together, as one message", each: "one at a time, an answer each", hold: "held until I let them go" };
// WHEN what is sent mid-answer goes (Chad, 18 Nov 2026: "It depends ... main
// settings for default, per chat for chat, and then being able to override on
// the message sent"): this chat's own setting, else every chat's, else the
// round - and WHEN_ONCE, the choice made beside the send button for the one
// message being written. A chat with one voice has no seats to wait on.
const INTERRUPT_NAMES = { round: "when the answer, or the whole round, is in", seat: "when the seat speaking is done",
                          now: "at once: the answer is stopped" };
const interruptLabels = () => current?.seats?.length
  ? { round: "after the round", seat: "after this seat", now: "stop and send now" }
  : { round: "after this answer", now: "stop and send now" };
let WHEN_ONCE = null;
// How a message's pictures sit in the log (21 Nov 2026, Chad: "file
// collapse/display for pictures for skimming"): this chat's own, else every
// chat's, else strip.
const PICTURE_NAMES = { strip: "side by side when there are several", full: "each at full size",
                        folded: "folded to one line per message" };
const picturesMode = () => {
  const own = current ? current.settings?.pictures : DRAFT.settings.pictures;
  return PICTURE_NAMES[own] ? own : PICTURE_NAMES[CFG?.pictures] ? CFG.pictures : "strip";
};
// The pictures YOU send, on their own (23 Nov 2026): this chat's, every
// chat's, else as everyone's go.
const myPicturesMode = () => {
  const own = current ? current.settings?.my_pictures : DRAFT.settings.my_pictures;
  return PICTURE_NAMES[own] ? own : PICTURE_NAMES[CFG?.my_pictures] ? CFG.my_pictures : picturesMode();
};
const isYours = m => m.role === "user" && !m.source && !m.seat;
const interruptMode = () => {
  const own = current ? current.settings?.interrupt : DRAFT.settings.interrupt;
  const usual = INTERRUPT_NAMES[own] ? own : INTERRUPT_NAMES[CFG?.interrupt] ? CFG.interrupt : "round";
  const when = WHEN_ONCE || usual;
  return interruptLabels()[when] ? when : "round";
};
// What the picture panel's translate drafts a prompt from: this chat's own
// setting, else every chat's (Settings, "picture prompts from"), else the chat.
const TRANSLATE_NAMES = { plain: "my words only", chat: "my words and this chat", fork: "the whole conversation (a side copy, slower)" };
const TRANSLATE_FROM = { plain: "your words", chat: "your words and this chat", fork: "the whole conversation" };
const translateOwn = () => { const t = current ? current.settings?.translate : DRAFT.settings.translate; return TRANSLATE_NAMES[t] ? t : ""; };
const translateMode = () => translateOwn() || (TRANSLATE_NAMES[CFG?.translate] ? CFG.translate : "chat");
const queueMode = () => {
  const own = current ? current.settings?.queue : DRAFT.settings.queue;
  return QUEUE_NAMES[own] ? own : QUEUE_NAMES[CFG?.queue] ? CFG.queue : "meld";
};
function lockComposer() {
  const locked = chatBusy();
  // An answer coming does not lock the box: what you send now waits its turn
  // (QUEUE) and goes the moment this answer is in. A picture being drawn does.
  $("#send").disabled = drawing;
  $("#send").textContent = locked && !drawing ? "send after" : "send";
  const how = queueMode();
  const when = $("#send-when"), labels = interruptLabels();
  when.hidden = !locked || drawing;
  if (!when.hidden) {
    const now = interruptMode();
    const html = Object.entries(labels).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
    if (when.innerHTML !== html) when.innerHTML = html;
    when.value = now;
  } else WHEN_ONCE = null;                         // a choice for one message does not outlive the answer it was made in
  $("#send").title = !locked || drawing ? ""
    : how === "hold" ? "an answer is coming: this waits above the box until you let it go"
    : how === "each" ? "an answer is coming: this is sent as soon as it is in"
    : "an answer is coming: this goes when it is in, as one message with anything else waiting";
  // The line is redrawn either way (its buttons read differently mid-answer);
  // with no answer coming, whatever is waiting and not held goes.
  setTimeout(() => { drawQueue(); drawBehind(); if (!chatBusy()) drainQueue(); }, 0);
  $("#input").placeholder = locked && !drawing
    ? (how === "hold" ? "answering… what you send now waits until you let it go"
       : how === "each" ? "answering… what you send now goes as soon as this answer is in"
       : "answering… what you send now goes as one message when this answer is in")
    : current?.seats?.length ? `the room: ${current.seats.map(x => x.name).join(", ")} · @ picks who answers first · @name! asks only them` : PLACEHOLDER;
}

const voiceName = v => v === "claude" ? (CFG.claude?.name || "claude") : "local model";
const fileUrl = (sid, name) => `/api/sessions/${encodeURIComponent(sid)}/files/${encodeURIComponent(name)}`;
// The same file handed over to be saved (the server names it as it came).
const saveUrl = (sid, name) => `${fileUrl(sid, name)}?download=1`;
const saveEl = (sid, f) => `<a class="save" href="${esc(saveUrl(sid, f.name))}" download="${esc(f.orig || f.name)}"`
  + ` title="save ${esc(f.orig || f.name)}">${icon("save")}</a>`;
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
  await loadAvatars();
  drawVoices(); await refresh(); drawLog(); takeDraft(); screenInit(); earsInit();
  startWatching();
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

async function newProject(body) {
  await call("POST", "/api/projects", body);
  await reloadConfig();
  FOLDED.delete(body.name); newChatIn(body.name);
  if (tab === "settings") drawSettings();
}

// A new chat in `name`: nothing on the server until its first message, and
// whatever is set for it before then (⚙) waits in DRAFT and goes with it.
function newChatIn(name) {
  stashDraft();
  stopFollowing(); folder = name; current = null; DRAFT = { settings: {}, title: "" };
  takeDraft();
  drawSide(); drawLog(); lockComposer(); drawQueue();
}

// ── What is typed belongs to the chat it was typed in ───────────────────
// The box used to be the window's: half a message written in one chat was
// sitting there in the next (Chad, 17 Nov 2026: "a filled text box in one chat
// shouldnt travel to another"). Now leaving a chat puts its words - and the
// files waiting in its tray - aside under that chat, and coming back brings
// them out again. The words outlive a restart (this browser's storage); files
// not yet sent do not. A chat not made yet goes by its project.
const DRAFTS = new Map(Object.entries(local("symposium.drafts", {})).map(([k, text]) => [k, { text: String(text), pending: [] }]));
const draftKey = () => current ? current.id : `new:${folder}`;
const keepDrafts = () => keep("symposium.drafts",
  Object.fromEntries([...DRAFTS].filter(([, d]) => d.text).slice(-40).map(([k, d]) => [k, d.text.slice(0, 20000)])));
function stashDraft() {
  const text = $("#input").value;
  if (text || PENDING.length) DRAFTS.set(draftKey(), { text, pending: PENDING }); else DRAFTS.delete(draftKey());
  keepDrafts();
  stashPic();                                    // the picture panel is the chat's too
}
function takeDraft() {
  const d = DRAFTS.get(draftKey());
  DRAFTS.delete(draftKey()); keepDrafts();
  $("#input").value = d?.text || "";
  PENDING = d?.pending || [];
  drawTray(); atClose();
  takePic();
}
// Closing the window is leaving every chat at once: what is in the box is kept for the one it is in.
window.addEventListener("pagehide", () => { if ($("#input").value) { DRAFTS.set(draftKey(), { text: $("#input").value, pending: [] }); keepDrafts(); } stashPic(); });

// The session a message lands in: the open chat's, or a new one made now,
// carrying what was set for it while it was only a draft.
async function ensureSession() {
  if (!current) {
    const was = draftKey();
    current = await call("POST", "/api/sessions", { folder, title: DRAFT.title || "", settings: DRAFT.settings });
    DRAFT = { settings: {}, title: "" };
    if (PIC_TRANSLATING?.key === was) PIC_TRANSLATING.key = current.id;   // the prompt being drafted follows the chat it was asked in
    await refresh();
  }
  return current;
}

const presetFor = name => CFG.folders.find(f => f.name === name) || {};

// One chat's row in the bar.
function sessRow(s, name) {
  const here = current && s.id === current.id;
  return `<div class="sess${s.archived ? " archived" : ""}" draggable="true" data-s="${esc(s.id)}" data-folder="${esc(name)}" aria-current="${here}"
               title="${esc(s.title || "untitled")} · ${s.count} messages${s.archived ? " · archived" : ""}">${icon(here ? "kylix-full" : "kylix")}<span>${esc(s.title || "untitled")}</span>`
       + (ANSWERING.has(s.id) ? `<i class="live-dot" title="an answer is coming"></i>` : "")
       + (s.sources?.length ? `<i class="fed-dot" title="a plugin speaks in this chat: ${esc(s.sources.join(", "))}"></i>` : "")
       + `</div>`;
}

// Each project's archived chats, folded into one line under the live ones
// (21 Nov 2026, Chad: "archival, or just hiding from main project area").
// Which piles are open is this window's, for now; the open chat's pile
// opens with it, so an archived chat you are reading is never hidden.
const ARCH_OPEN = new Set();

function drawSide() {
  // into the list, not the bar: the bar's fold button is the page's, not redrawn
  if (SEARCH.q) return drawHits();
  $("#side-list").innerHTML = folderNames().map(name => {
    const f = presetFor(name);
    const bits = [f.voice === "claude" ? voiceName("claude") : null, f.model,
                  f.voice !== "claude" && f.temperature != null ? `t=${f.temperature}` : null].filter(Boolean).join(" · ");
    const chats = SESSIONS.filter(s => s.folder === name);
    const live = chats.filter(s => !s.archived), put = chats.filter(s => s.archived);
    const open = !FOLDED.has(name) || (current && current.folder === name);
    const pileOpen = ARCH_OPEN.has(name) || put.some(s => s.id === current?.id);
    const rows = open ? live.map(s => sessRow(s, name)) : [];
    const pile = open && put.length
      ? `<div class="arch-pile" data-pile="${esc(name)}" aria-expanded="${pileOpen}" title="${pileOpen ? "fold the archived chats" : "show the archived chats"}">`
        + `<span class="seren-chev" aria-hidden="true"></span>archived (${put.length})</div>`
        + (pileOpen ? put.map(s => sessRow(s, name)).join("") : "")
      : "";
    return `<div class="folder" data-folder="${esc(name)}"><span class="name" data-fold="${esc(name)}" title="${open ? "fold" : "unfold"}">`
         + `${icon(open ? "krater-full" : "krater")}<span>${esc(name || "chats")}</span></span>`
         + (bits ? `<span class="hint">${esc(bits)}</span>` : "")
         + `<button data-new="${esc(name)}" title="new chat in ${esc(name || "chats")}">+</button></div>`
         + (open ? (rows.join("") || (put.length ? "" : `<div class="sess empty">nothing yet</div>`)) + pile : "");
  }).join("") + `<div class="loose-drop" data-folder="">drop here to take it out of its project</div>`;
}

// A kebab (⋮) opens its menu under itself, right edges lined up.
function kebab(btn, items) {
  const r = btn.getBoundingClientRect();
  openMenu(r.right, r.bottom + 4, items);
  const m = $("#menu");
  m.style.left = `${Math.max(6, r.right - m.offsetWidth)}px`;
}
// The projects bar's: what used to sit at its foot.
$("#side-more").onclick = e => {
  e.stopPropagation();
  kebab(e.currentTarget, [["New project…", () => openNewProject()],
                          ["Import a chat…", () => $("#importer").click()],
                          ...(CFG?.claude?.available ? [["Pick up from a terminal…", () => openPickup()]] : [])]);
};

async function setArchived(id, on) {
  try {
    const s = await call("PATCH", `/api/sessions/${id}`, { archived: on });
    if (current?.id === id) { current = { ...current, archived: s.archived }; drawTitlebar(); }
  } catch (ex) { return alert(ex.message); }
  await refresh();
}

// ── Search every chat ───────────────────────────────────────────────────
// What is typed in the bar's box, and what came back for it. The server reads
// every chat, archived ones too; a reply that lands after newer words were
// typed is dropped.
// `all`: chat id -> every match in it, for the chats whose "and N more" was
// opened (23 Nov 2026); kept until the words change.
const SEARCH = { q: "", hits: [], asked: 0, timer: 0, pending: false, all: new Map() };
const searchTerms = q => q.toLowerCase().split(/\s+/).filter(Boolean);
function hiliteTerms(text, terms) {
  let out = esc(text);
  for (const w of terms) out = out.replace(new RegExp(escRe(esc(w)), "gi"), m => `<mark>${m}</mark>`);
  return out;
}
function drawHits() {
  const terms = searchTerms(SEARCH.q);
  if (!SEARCH.hits.length) {
    $("#side-list").innerHTML = `<div class="side-none">${SEARCH.pending ? "looking…" : `nothing says “${esc(SEARCH.q)}”`}</div>`;
    return;
  }
  $("#side-list").innerHTML = SEARCH.hits.map(h => {
    const here = current && h.id === current.id;
    const all = SEARCH.all.get(h.id), shown = all || h.matches;
    const snips = shown.map(m => {
      const who = m.role === "user" ? "you" : (m.seat || voiceName("claude"));
      return `<span class="snip" data-hit="${esc(h.id)}" data-at="${m.index}"><b>${esc(who)}:</b> ${hiliteTerms(m.snippet, terms)}</span>`;
    }).join("");
    // "and N more" opens every match in the chat, newest first; again folds it.
    const more = all ? `<button type="button" class="more" data-more="${esc(h.id)}">show only the newest ${h.matches.length}</button>`
      : h.hits > h.matches.length ? `<button type="button" class="more" data-more="${esc(h.id)}">and ${h.hits - h.matches.length} more in this chat</button>` : "";
    return `<div class="hit" data-hit="${esc(h.id)}" aria-current="${here}" title="${esc(h.title || "untitled")}${h.archived ? " · archived" : ""}">`
         + `<div class="hit-head"><span class="hit-title">${h.title_hit ? hiliteTerms(h.title || "untitled", terms) : esc(h.title || "untitled")}</span>`
         + `<span class="hit-where">${esc(h.folder || "chats")}${h.archived ? " · archived" : ""}</span></div>`
         + snips + more + `</div>`;
  }).join("");
}
async function runSearch() {
  const q = SEARCH.q, n = ++SEARCH.asked;
  if (!q) { SEARCH.hits = []; SEARCH.pending = false; return drawSide(); }
  SEARCH.pending = true; if (!SEARCH.hits.length) drawHits();
  let got;
  try { got = await call("GET", `/api/sessions/search?q=${encodeURIComponent(q)}`); }
  catch (ex) {
    if (n === SEARCH.asked) $("#side-list").innerHTML = `<div class="side-none">could not search: ${esc(ex.message)}</div>`;
    return;
  }
  if (n !== SEARCH.asked) return;                  // newer words were typed meanwhile
  SEARCH.pending = false; SEARCH.hits = got.sessions; SEARCH.all.clear(); drawHits();
}
async function openMore(id) {
  if (SEARCH.all.has(id)) { SEARCH.all.delete(id); return drawHits(); }
  const q = SEARCH.q;
  try {
    const got = await call("GET", `/api/sessions/search?q=${encodeURIComponent(q)}&chat=${encodeURIComponent(id)}`);
    if (q !== SEARCH.q || !got.sessions?.[0]) return;
    SEARCH.all.set(id, got.sessions[0].matches); drawHits();
  } catch (ex) { alert(`could not search that chat: ${ex.message}`); }
}
$("#side-q").addEventListener("input", () => {
  SEARCH.q = $("#side-q").value.trim();
  clearTimeout(SEARCH.timer); SEARCH.timer = setTimeout(runSearch, SEARCH.q ? 180 : 0);
});
$("#side-q").addEventListener("keydown", e => {
  if (e.key === "Escape") { e.preventDefault(); $("#side-q").value = ""; SEARCH.q = ""; runSearch(); $("#input").focus(); }
  if (e.key === "Enter") { e.preventDefault(); $("#side-list .snip, #side-list .hit")?.click(); }
});
document.addEventListener("keydown", e => {
  if (e.key.toLowerCase() === "k" && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
    e.preventDefault(); show("chat"); $("#side-q").focus(); $("#side-q").select();
  }
});

// Open a chat at one of its messages, and point at it for a moment.
async function openAt(id, index) {
  await openSession(id);
  const el = $(`#log .msg[data-i="${index}"]`);
  if (!el) return;
  const go = () => { pinned = false; el.scrollIntoView({ block: "center" }); };
  go(); setTimeout(go, 250);                       // again once the pictures above it have taken their room
  el.classList.add("found");
  setTimeout(() => el.classList.add("fading"), 1600);
  setTimeout(() => el.classList.remove("found", "fading"), 3000);
}

$("#side").onclick = async e => {
  const more = e.target.closest("[data-more]");
  if (more) { e.stopPropagation(); return openMore(more.dataset.more); }
  const snip = e.target.closest(".snip[data-hit]");
  if (snip) return openAt(snip.dataset.hit, Number(snip.dataset.at));
  const hit = e.target.closest(".hit[data-hit]");
  if (hit) return openSession(hit.dataset.hit);
  const pile = e.target.closest("[data-pile]");
  if (pile) { const n = pile.dataset.pile; ARCH_OPEN.has(n) ? ARCH_OPEN.delete(n) : ARCH_OPEN.add(n); return drawSide(); }
  const row = e.target.closest("[data-s]");
  if (row) return openSession(row.dataset.s);
  const add = e.target.closest("[data-new]");
  if (add) { newChatIn(add.dataset.new); $("#input").focus(); return; }
  const fold = e.target.closest("[data-fold]");
  if (fold) {
    const name = fold.dataset.fold;
    FOLDED.has(name) ? FOLDED.delete(name) : FOLDED.add(name);
    keep("symposium.folded", [...FOLDED]); return drawSide();
  }
};

// ── Pick up a terminal's conversation ──────────────────────────────────
// The claude CLI's own conversations on this box (GET /api/claude/sessions);
// one picked becomes a chat in the project open now, resuming it where it
// began - forked off the terminal when it may still be open there.
const ago = at => {
  const s = Math.max(0, Date.now() / 1000 - at);
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago`
    : s < 129600 ? `${Math.round(s / 3600)} h ago` : when(at);
};
const lastPart = p => String(p || "").split(/[\\/]/).filter(Boolean).pop() || p || "?";
async function drawPickup() {
  $("#pu-body").innerHTML = `<p class="note">looking…</p>`;
  let got;
  try { got = await call("GET", "/api/claude/sessions"); }
  catch (ex) { $("#pu-body").innerHTML = `<p class="note">Could not look: ${esc(ex.message)}</p>`; return; }
  if (!got.sessions.length) {
    $("#pu-body").innerHTML = `<p class="note">Nothing to pick up in ${esc(got.dir)}.</p>`; return;
  }
  $("#pu-body").innerHTML = `<div class="pu-list">` + got.sessions.map(s =>
    `<button class="pu-row" data-pu="${esc(s.id)}" data-cwd="${esc(s.cwd)}" title="${esc(s.cwd)} · ${esc(s.id)}">`
    + `<span class="pu-title">${esc(s.title || "untitled")}</span>`
    + `<span class="pu-where">${esc(lastPart(s.cwd))}</span>`
    + `<span class="pu-when">${s.said} said · ${esc(ago(s.updated))}</span>`
    + (s.warm ? `<span class="pu-warm" title="touched in the last few minutes: picking it up forks it">warm · will fork</span>` : "")
    + `</button>`).join("") + `</div>`;
}
// Picked for a seat of the open chat rather than as a chat of its own: set by
// "from a terminal…" under the chat's seats.
let PICKUP_SEAT = false;
function openPickup(forSeat = false) {
  PICKUP_SEAT = forSeat;
  $("#pickup-sheet h3").textContent = forSeat ? "Invite from a terminal" : "Pick up from a terminal";
  drawPickup(); $("#pickup-sheet").showModal();
}
$("#pu-refresh").onclick = drawPickup;
// A seat's name from its folder, the kitchen's way: SerenLodestar -> lodestar.
const seatNameFor = cwd => lastPart(cwd).replace(/^seren/i, "").toLowerCase().replace(/[^\w·.-]+/g, "").slice(0, 32);
$("#pu-body").onclick = async e => {
  const row = e.target.closest("[data-pu]");
  if (!row || row.disabled) return;
  row.disabled = true;
  try {
    if (PICKUP_SEAT && current) {
      const name = prompt("Seat name", seatNameFor(row.dataset.cwd) || "guest");
      if (!name) { row.disabled = false; return; }
      current = await call("POST", `/api/sessions/${encodeURIComponent(current.id)}/seats`, { name, pickup: row.dataset.pu });
      $("#pickup-sheet").close(); await refresh(); drawLog(); drawSeats(); return;
    }
    current = await call("POST", "/api/claude/sessions/pickup", { id: row.dataset.pu, folder });
    $("#pickup-sheet").close();
    folder = current.folder; await refresh(); drawLog(); show("chat");
  } catch (ex) { row.disabled = false; alert(`Could not pick it up: ${ex.message}`); }
};

// ── A room's seats (seren_symposium.room), under the chat's ⚙ ───────────
// Each seat its own claude conversation in its own folder; a message goes
// round them in order, each hearing the ones before. @name puts a seat
// first, @name! asks only the ones named. The order here is the order the
// table goes round: ↑ ↓ move a seat, its name renames it, its dot recolours it.
function drawSeats() {
  const box = $("#s-seats");
  if (!current) { box.innerHTML = `<p class="note">Send the first message, then invite seats.</p>`; return; }
  const seats = current.seats || [];
  box.innerHTML = (seats.length
      ? `<div class="seat-list">` + seats.map((x, i) => `<div class="seat-row" style="--seat:${seatColor(x.name)}">`
          + `<button type="button" class="seat-face" data-face="${esc(x.name)}" title="${esc(x.name)}'s avatar: any picture you like">${avatarHtml(x.name)}</button>`
          + `<button type="button" class="seat-dot" data-hue="${esc(x.name)}" title="another colour for ${esc(x.name)}"></button>`
          + `<button type="button" class="seat-name" data-rename="${esc(x.name)}" title="rename ${esc(x.name)}">${esc(seatWho(x.name))}</button>`
          + `<span class="note" title="${esc(x.cwd || "")}">${esc(x.cwd ? lastPart(x.cwd) : "claude.cwd")}</span>`
          + `<button type="button" class="ghost" data-move="${esc(x.name)}" data-by="-1" title="answer earlier"${i ? "" : " disabled"}>↑</button>`
          + `<button type="button" class="ghost" data-move="${esc(x.name)}" data-by="1" title="answer later"${i < seats.length - 1 ? "" : " disabled"}>↓</button>`
          + `<button type="button" class="ghost" data-unseat="${esc(x.name)}" title="let ${esc(x.name)} go - what they said stays">✕</button></div>`).join("")
        + `</div>`
      : `<p class="note">One Claude here. Invite a seat and this becomes a room: each seat its own session in its own folder, `
        + `answering in turn and hearing the others. @name puts one first; @name! asks only them.</p>`)
    + `<div class="seat-add"><input id="seat-name" placeholder="name" maxlength="32" spellcheck="false">`
    + `<input id="seat-cwd" placeholder="folder it works in" spellcheck="false">`
    + `<button type="button" class="ghost" id="seat-invite">invite</button>`
    + `<button type="button" class="ghost" id="seat-pickup">from a terminal…</button>`
    + `<button type="button" class="ghost" data-face="default" title="the picture worn by everyone who has not picked one">default avatar…</button></div>`;
}
$("#s-seats").onclick = async e => {
  if (!current) return;
  const sid = encodeURIComponent(current.id);
  try {
    const gone = e.target.closest("[data-unseat]"), move = e.target.closest("[data-move]");
    const rename = e.target.closest("[data-rename]"), hue = e.target.closest("[data-hue]");
    const face = e.target.closest("[data-face]");
    if (face) return openAvatar(face.dataset.face, "", true, face.dataset.face === "default" ? "all" : "chat");
    if (gone) {
      current = await call("DELETE", `/api/sessions/${sid}/seats/${encodeURIComponent(gone.dataset.unseat)}`);
    } else if (move) {
      const order = current.seats.map(x => x.name), at = order.indexOf(move.dataset.move), to = at + Number(move.dataset.by);
      if (at < 0 || to < 0 || to >= order.length) return;
      [order[at], order[to]] = [order[to], order[at]];
      current = await call("PATCH", `/api/sessions/${sid}/seats`, { order });
    } else if (rename) {
      const was = rename.dataset.rename, name = (prompt("Seat name", was) || "").trim();
      if (!name || name === was) return;
      current = await call("PATCH", `/api/sessions/${sid}/seats/${encodeURIComponent(was)}`, { name });
    } else if (hue) {
      // The next hue round the wheel that no other seat is near.
      const others = current.seats.filter(x => x.name !== hue.dataset.hue).map(x => seatHue(x.name));
      const far = h => others.every(o => Math.min(Math.abs(o - h), 360 - Math.abs(o - h)) >= 25);
      let next = seatHue(hue.dataset.hue);
      for (let n = 0; n < 12; n++) { next = (next + 30) % 360; if (far(next)) break; }
      current = await call("PATCH", `/api/sessions/${sid}/seats/${encodeURIComponent(hue.dataset.hue)}`, { hue: next });
    } else if (e.target.id === "seat-invite") {
      const name = $("#seat-name").value.trim(), cwd = $("#seat-cwd").value.trim();
      if (!name) return $("#seat-name").focus();
      current = await call("POST", `/api/sessions/${sid}/seats`, { name, cwd });
    } else if (e.target.id === "seat-pickup") {
      return openPickup(true);
    } else return;
    await refresh(); drawLog(); drawSeats(); lockComposer();
  } catch (ex) { alert(ex.message); }
};

// ── Voices: a face and a bubble colour for everyone who is not a seat ────
// You, the chat's one Claude, each local model that has answered here, each
// plugin that has spoken. Under the chat's ⚙, beside the seats: a face is
// worn by name in every chat, a colour is this chat's own.
function chatVoices() {
  const out = new Map();
  const add = (key, label, claude = false) => { if (!out.has(key)) out.set(key, { key, label, claude }); };
  add("you", CFG.you || "you");
  if (!current?.seats?.length && voiceOfChat() === "claude") add("default", CFG.claude?.name || "claude", true);
  for (const m of current?.messages || []) {
    if (m.seat) continue;
    const key = voiceKey(m);
    add(key, key === "you" ? (CFG.you || "you") : whoName(m), key === "default");
  }
  return [...out.values()];
}
function drawVoiceRows() {
  const box = $("#s-voices");
  if (!current) { box.innerHTML = `<p class="note">Send the first message, then give everyone a face and a colour.</p>`; return; }
  box.innerHTML = `<div class="seat-list">` + chatVoices().map(v => {
    const hue = voiceHue(v.key);
    return `<div class="voice-row"${hue != null ? ` style="--seat:${hueColor(hue)}"` : ""}>`
      + `<button type="button" class="seat-face" data-vface="${esc(v.key)}" data-label="${esc(v.label)}" data-claude="${v.claude ? 1 : ""}" `
      + `title="${esc(v.label)}'s avatar: any picture you like">${faceHtml(v.key, { claude: v.claude, hue, label: v.label }) || `<i class="avatar blank none">+</i>`}</button>`
      + `<button type="button" class="seat-dot${hue == null ? " none" : ""}" data-vhue="${esc(v.key)}" `
      + `title="${hue == null ? `give ${esc(v.label)}'s bubbles a colour` : `another colour for ${esc(v.label)}'s bubbles`}"></button>`
      + `<b class="voice-name">${esc(v.label)}</b>`
      + (hue != null ? `<button type="button" class="ghost" data-vplain="${esc(v.key)}" title="back to the plain bubble">plain</button>` : `<span></span>`)
      + `</div>`;
  }).join("") + `</div>`
    + `<p class="note">Click a face for any picture, the dot for a bubble colour. A face goes by name, so it is the same in every chat; `
    + `a colour is this chat's. Seats have theirs under seats.</p>`;
}
$("#s-voices").onclick = async e => {
  if (!current) return;
  const face = e.target.closest("[data-vface]"), dot = e.target.closest("[data-vhue]"), plain = e.target.closest("[data-vplain]");
  if (face) return openAvatar(face.dataset.vface, face.dataset.label, !!face.dataset.claude);
  if (!dot && !plain) return;
  const colors = { ...(current.settings?.colors || {}) };
  if (plain) delete colors[plain.dataset.vplain];
  else {
    // The next hue round the wheel that nobody else in this chat is near.
    const key = dot.dataset.vhue;
    const others = [...Object.entries(colors).filter(([k]) => k !== key).map(([, h]) => h),
                    ...(current.seats || []).map(x => seatHue(x.name))];
    const far = h => others.every(o => Math.min(Math.abs(o - h), 360 - Math.abs(o - h)) >= 25);
    let next = Number.isInteger(colors[key]) ? colors[key] : 170;
    for (let n = 0; n < 12; n++) { next = (next + 30) % 360; if (far(next)) break; }
    colors[key] = next;
  }
  try {
    await call("PATCH", `/api/sessions/${encodeURIComponent(current.id)}`, { settings: { colors } });
    current = await call("GET", `/api/sessions/${encodeURIComponent(current.id)}`);
  } catch (ex) { return alert(ex.message); }
  drawLog(); drawVoiceRows();
};

// The avatar sheet: who is wearing what, and three ways to change it - a
// file, a link, or any picture already in this chat (which is how one drawn
// in the picture panel gets worn).
let FACE_FOR = null;
// `label`: who it is, as the chat names them (a seat goes by its seat name);
// `claude`: one who wears the default until it picks (a seat, the chat's Claude).
// `scope`: "chat" - the face is this chat's own, over what is worn everywhere;
// "all" - every chat's. With no chat open there is only every chat.
let FACE_LABEL = "", FACE_CLAUDE = true, FACE_SCOPE = "chat";
function openAvatar(name, label = "", claude = true, scope = "chat") {
  FACE_FOR = name; FACE_LABEL = label; FACE_CLAUDE = claude;
  FACE_SCOPE = current && scope === "chat" ? "chat" : "all";
  $("#av-url").value = ""; drawAvatar(); $("#avatar-sheet").showModal();
}
function drawAvatar() {
  const name = FACE_FOR, here = FACE_SCOPE === "chat";
  const who = name === "default" ? (CFG.claude?.name || "claude") : name === "you" ? (CFG.you || "you") : (FACE_LABEL || seatWho(name));
  $("#av-title").textContent = `${who}'s avatar`;
  $("#av-scope").innerHTML = (current ? `<button type="button" data-scope="chat" class="${here ? "on" : ""}">this chat</button>` : "")
    + `<button type="button" data-scope="all" class="${here ? "" : "on"}">every chat</button>`;
  const chatFace = current?.settings?.faces?.[name];
  const own = here ? !!chatFace : !!AVATARS[name];
  // What is worn when this scope has none of its own, so the sheet never shows a blank where the chat shows a face.
  const src = own ? (here ? fileUrl(current.id, chatFace) : avatarUrl(name))
    : here ? (AVATARS[name] ? avatarUrl(name) : FACE_CLAUDE && AVATARS.default ? avatarUrl("default") : null)
    : name !== "default" && FACE_CLAUDE && AVATARS.default ? avatarUrl("default") : null;
  const why = own ? "" : here ? (src ? "none of its own in this chat: this is the every-chat one" : "none in this chat, and none for every chat")
    : src ? "none of its own: this is the default" : FACE_CLAUDE ? "none: a seat wears its initial" : "none yet";
  $("#av-now").innerHTML = (src ? `<img src="${esc(src)}" alt="">` : "") + (why ? `<span class="note">${esc(why)}</span>` : "");
  const pics = [];
  (current?.messages || []).forEach(m => (m.files || []).forEach(f => { if (isImage(f.type) && !isScreen(f)) pics.push(f); }));
  $("#av-pics").innerHTML = pics.length
    ? pics.reverse().slice(0, 60).map(f => `<button type="button" data-pic="${esc(f.name)}" title="${esc(f.orig || f.name)}">`
        + `<img src="${esc(fileUrl(current.id, f.name))}" loading="lazy" alt=""></button>`).join("")
    : `<p class="note">${current ? "No pictures in this chat yet: draw one with 🖼 picture, or attach one."
        : "Open a chat to pick from its pictures or use a link; a file works from here."}</p>`;
  $("#av-off").hidden = !own;
  $("#av-off").textContent = here ? "take it off in this chat" : "take it off everywhere";
}
function redrawFaces() { drawAvatar(); drawLog(); drawSeats(); drawVoiceRows(); drawFaceDefaults(); }
// `body`: {file} one of this chat's, {url} one from the web, or {data, filename} the picture itself (no chat needed).
async function wearAvatar(body) {
  if (!current && !body.data) return alert("Open a chat to use a link or one of its pictures; a file works from here.");
  try {
    const r = await call("POST", `/api/avatars/${encodeURIComponent(FACE_FOR)}`,
                         body.data ? body : { session: current.id, ...body, ...(FACE_SCOPE === "chat" ? { scope: "chat" } : {}) });
    AVATARS = r.avatars;
    if (r.faces && current) current.settings = { ...current.settings, faces: r.faces };
  } catch (ex) { return alert([ex.message, ex.body?.hint].filter(Boolean).join(" - ")); }
  redrawFaces();
}
$("#av-scope").onclick = e => {
  const pick = e.target.closest("[data-scope]");
  if (pick) { FACE_SCOPE = pick.dataset.scope; drawAvatar(); }
};
$("#av-choose").onclick = () => $("#av-file").click();
$("#av-file").onchange = async e => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  try {
    if (!current) return await wearAvatar({ data: await b64(file), filename: file.name });
    const kept = await call("POST", `/api/sessions/${encodeURIComponent(current.id)}/files`, { name: file.name, data: await b64(file) });
    await wearAvatar({ file: kept.name });
  } catch (ex) { alert(ex.message); }
};
$("#av-use-url").onclick = () => { const url = $("#av-url").value.trim(); if (url) wearAvatar({ url }); };
$("#av-url").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); $("#av-use-url").click(); } });
$("#av-pics").onclick = e => { const pic = e.target.closest("[data-pic]"); if (pic) wearAvatar({ file: pic.dataset.pic }); };
$("#av-off").onclick = async () => {
  try {
    if (FACE_SCOPE === "chat" && current) {
      const faces = { ...(current.settings?.faces || {}) };
      delete faces[FACE_FOR];
      await call("PATCH", `/api/sessions/${encodeURIComponent(current.id)}`, { settings: { faces } });
      current = await call("GET", `/api/sessions/${encodeURIComponent(current.id)}`);
    } else AVATARS = (await call("DELETE", `/api/avatars/${encodeURIComponent(FACE_FOR)}`)).avatars;
  } catch (ex) { return alert(ex.message); }
  redrawFaces();
};

// The faces worn in every chat, on the Settings tab: the default (every
// chat's Claude) and yours. A chat that wants its own sets it under its ⚙.
function drawFaceDefaults() {
  const rows = [["default", CFG.claude?.name || "claude", true], ["you", CFG.you || "you", false]];
  $("#a-faces").innerHTML = `<div class="seat-list">` + rows.map(([key, label, claude]) =>
      `<div class="voice-row"><button type="button" class="seat-face" data-aface="${key}" data-label="${esc(label)}" data-claude="${claude ? 1 : ""}" `
      + `title="${esc(label)}'s avatar in every chat: any picture you like">`
      + (AVATARS[key] ? `<img class="avatar" src="${esc(avatarUrl(key))}" alt="">` : `<i class="avatar blank none">+</i>`) + `</button>`
      + `<span></span><b class="voice-name">${esc(label)}</b><span></span></div>`).join("") + `</div>`
    + `<p class="note">Worn in every chat that has not picked its own. A chat's own faces, and its colours, are under its ⚙.</p>`;
}
$("#a-faces").onclick = e => {
  const face = e.target.closest("[data-aface]");
  if (face) openAvatar(face.dataset.aface, face.dataset.label, !!face.dataset.claude, "all");
};
$("#avatar-sheet").addEventListener("click", e => { if (e.target === $("#avatar-sheet")) $("#avatar-sheet").close(); });

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
    const put = SESSIONS.find(x => x.id === id)?.archived;
    openMenu(e.clientX, e.clientY, [["Rename", () => editInPlace(row.querySelector("span"), t => renameChat(id, t))],
                                    [put ? "Bring back from the archive" : "Archive", () => setArchived(id, !put)],
                                    ["Remove (nothing deleted)", () => removeChat(id)],
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

// Remove is not Delete: the chat leaves Symposium and nothing is thrown
// away. Its file and attachments are set aside (sessions/removed), and a
// Claude conversation it carried - one picked up from a terminal by accident,
// say - is left exactly as it was, to be picked up again or carried on there.
async function removeChat(id) {
  const s = SESSIONS.find(x => x.id === id);
  if (!sure(`Take "${s?.title || "untitled"}" out of Symposium?\n\nNothing is deleted: the chat is set aside, and a Claude `
            + `conversation it carried can be picked up again or carried on in a terminal.`)) return;
  try { await call("POST", `/api/sessions/${id}/remove`, {}); } catch (ex) { return alert(ex.message); }
  if (current?.id === id) newChatIn(current.folder);
  await refresh();
}

async function deleteChat(id) {
  const s = SESSIONS.find(x => x.id === id);
  if (!sure(`Delete "${s?.title || "untitled"}" and its files? This can't be undone.`)) return;
  try { await call("DELETE", `/api/sessions/${id}`); } catch (ex) { return alert(ex.message); }
  if (current?.id === id) newChatIn(current.folder);
  await refresh();
}

async function openSession(id) {
  // Another chat: what was being typed stays behind with the one being left.
  // The same chat drawn again (an answer just landed) keeps its box as it is.
  const moving = current?.id !== id;
  if (moving) stashDraft();
  stopFollowing();
  OPENING++;
  try {
    current = await call("GET", `/api/sessions/${id}`);
    folder = current.folder;
    if (moving) takeDraft();
    drawSide(); drawLog(); lockComposer(); drawQueue();
    if ($("#chat-settings").open) drawChatSettings();
    // An answer still coming in this chat: pick it up where it is - unless
    // this window sent it, or is following it already.
    if (current.answering && !SENDING.has(id) && FOLLOWER?.sid !== id) followLive(id);
  } finally {
    // What was waiting behind the answer goes now, with the chat as it stands.
    if (!--OPENING) setTimeout(drainQueue, 0);
  }
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
  $("#titlebar").hidden = false;
  drawTitlebar();
  $("#log").innerHTML = "";
  drawFiles();
  if (!current) {
    const v = voiceOfChat() === "claude" ? voiceName("claude") : "a local model";
    $("#log").innerHTML = `<div class="blank">New chat in <b>${esc(folder || "chats")}</b>, answered by ${esc(v)}.<br>`
      + `It's kept once the first answer comes back.</div>`;
    return;
  }
  if (document.activeElement !== $("#title")) { $("#title").value = current.title; setRenaming(false); }
  const msgs = current.messages;
  msgs.forEach((m, i) => $("#log").append(bubble(m, i, msgs.length)));
  // A turn still coming in this chat: the question it is answering, then the
  // answer as far as it has got, whether this window sent it or came back to
  // it (22 Oct 2026: opened mid-answer, the question was missing until the
  // answer landed). A retry asks nothing new; the answer it replaces goes.
  const following = FOLLOWER?.sid === current.id ? FOLLOWER : null;
  const pending = following ? null : (current.answering ? current.pending : null);
  ASKED_EL = null;
  if ((following?.retry || pending?.retry) && msgs.at(-1)?.role === "assistant") $("#log").lastChild?.remove();
  if (following) {
    if (following.asked) $("#log").append(following.asked);
    $("#log").append(following.el);
  } else if (pending && !pending.retry) {
    ASKED_EL = bubble({ role: "user", content: pending.prompt || "", files: pending.files || [],
                        source: pending.source, by: pending.by }, -1, 0, { bare: true });
    $("#log").append(ASKED_EL);
  }
  toBottom(true);
}

// A seat of a room, named the way the kitchen names a hand: wren·kitchen.
const seatWho = s => CFG.claude?.name ? `${CFG.claude.name}·${s}` : s;
// Its colour: the hue it was given when it sat down, so who said what reads
// at a glance (17 Nov 2026: four seats, one colour). A seat that has left
// keeps hers (the chat's `left`): one worked out from the name landed where
// it liked, and 19 Nov 2026 starwright, removed, "adopted workbench's
// colours". Only a name the chat never seated gets a worked-out one.
function seatHue(name) {
  const seat = [...(current?.seats || []), ...(current?.left || [])].find(x => x.name === name);
  if (Number.isInteger(seat?.hue)) return seat.hue;
  let h = 0;
  for (const c of String(name)) h = (h * 31 + c.codePointAt(0)) % 360;
  return h;
}
const seatColor = name => `hsl(${seatHue(name)} 68% 56%)`;
// ── Avatars: the picture a voice wears (seren_symposium.avatars) ─────────
// By name: a seat's, and `default` for whoever has not picked. Open on
// purpose (Chad, 17 Nov 2026: "avatars should be more open and free"): the
// one wearing it sets it by showing a picture captioned avatar, and you can
// put any picture on anyone from ⚙ → seats.
let AVATARS = {};
async function loadAvatars() {
  try { AVATARS = (await call("GET", "/api/avatars")).avatars || {}; } catch { AVATARS = {}; }
}
const avatarUrl = name => `/api/avatars/${encodeURIComponent(name)}?v=${AVATARS[name]?.at || 0}`;
// A seat's own, else the default; a seat with neither wears its initial in
// its colour, and a chat's one Claude nothing (`seat` null).
// The picture a voice wears HERE, as an address: this chat's own for it
// first (the chat's `faces` setting - Chad, 17 Nov 2026: "a specific avatar
// for cheese tag, or whatever"), then the one it wears in every chat, then -
// for a Claude - the default. null: none.
function faceSrc(key, claude = false) {
  const here = key && current?.settings?.faces?.[key];
  if (here) return fileUrl(current.id, here);
  if (key && AVATARS[key]) return avatarUrl(key);
  return claude && AVATARS.default ? avatarUrl("default") : null;
}
function avatarHtml(seat) {
  const src = faceSrc(seat || "default", true);
  if (src) return `<img class="avatar" src="${esc(src)}" alt="">`;
  return seat ? `<i class="avatar blank">${esc([...String(seat)][0].toUpperCase())}</i>` : "";
}

// ── Everyone who speaks has a key: what their face and colour go by ──────
// `you` the person, `default` the chat's one Claude, a seat by its name, a
// local model or a plugin by its name made safe for an address. Faces are by
// key everywhere (/api/avatars); a colour is this chat's (settings.colors),
// except a seat's, which is its own. Chad, 17 Nov 2026: "definitely avatars
// for all, color bubbles for all, set up in the per chat settings".
const voiceSlug = s => String(s || "").trim().replace(/\.(gguf|safetensors)$/i, "")
  .replace(/[^\w·.-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "voice";
function voiceKey(m) {
  if (m.role === "user") return m.source ? voiceSlug(m.by || m.source) : "you";
  if (m.seat) return m.seat;
  const name = whoName(m);
  return name === CFG.claude?.name ? "default" : voiceSlug(name);
}
const hueColor = h => `hsl(${h} 68% 56%)`;
function voiceHue(key, isSeat = false) {
  if (isSeat) return seatHue(key);
  const h = current?.settings?.colors?.[key];
  return Number.isInteger(h) ? h : null;
}
// A voice's face: its own picture; a Claude (a seat, the chat's one) falls
// back to the default; anyone with a colour and no picture wears an initial.
function faceHtml(key, { claude = false, hue = null, label = "" } = {}) {
  const src = faceSrc(key, claude);
  if (src) return `<img class="avatar" src="${esc(src)}" alt="">`;
  if (hue == null) return "";
  return `<i class="avatar blank" style="--seat:${hueColor(hue)}">${esc([...String(label || key)][0].toUpperCase())}</i>`;
}

function wearSeat(el, name) {
  el.classList.toggle("seat", !!name);
  if (name) el.style.setProperty("--seat", seatColor(name)); else el.style.removeProperty("--seat");
}

// Who said it: you, the chat's Claude by name, or the local model that answered.
function whoName(m) {
  // Fed in by a plugin (the game table): the plugin's name, never yours.
  if (m.role === "user") return m.source ? (m.by || m.source) : (CFG.you || "you");
  if (m.seat) return seatWho(m.seat);
  const meta = m.meta || {};
  if (CFG.claude?.name && String(meta.node).toLowerCase() === CFG.claude.name.toLowerCase()) return CFG.claude.name;
  if (meta.voice === "picture") return `🖼 ${String(meta.model?.name || meta.model || "a picture").replace(/\.safetensors$/i, "")}`;
  return meta.model ? meta.model.replace(/\.gguf$/i, "") : "local model";
}

function voiceOfChat() {
  const v = (current ? current.settings?.voice : DRAFT.settings.voice)
         || presetFor(current ? current.folder : folder).voice || "lodestar";
  return v === "claude" && CFG.claude?.available ? "claude" : "lodestar";
}

// The receipt under an answer: which model, where, what it reached for, how long.
function receipt(meta) {
  if (!meta) return "";
  return [meta.model, meta.node,
          meta.tools && meta.tools.length ? `tools: ${meta.tools.join(", ")}`
            : (meta.tool_rounds ? `tools: ${meta.tool_rounds}` : null),
          meta.seconds != null ? `${meta.seconds}s` : null,
          meta.stopped ? "stopped by you before it finished" : null,
          // nobody asked: a task it had left running ended, and it came back to say so
          // The CLI's line says "completed (exit code 0)", and that is the LAST command's code: a test run piped
          // through grep, or ended with an echo, is 0 whether it passed or not (20 Nov 2026: two of three were, and
          // one had failed). So the code is left out; whether it worked is in what the seat says.
          meta.woke ? `came back on its own · ${String(meta.woke[0] || "a task ended").replace(/\s*\(exit code -?\d+\)/i, "").slice(0, 120)}`
            : null].filter(Boolean).join(" · ");
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
  const said = opts.who || whoName(m);
  // Whose bubble: its colour and its face. A bubble drawn before its message
  // exists (an answer as it starts) goes by the name it was given.
  const key = opts.cls?.includes("err") ? null
    : opts.who && !asked && !m.seat ? (said === CFG.claude?.name ? "default" : voiceSlug(said)) : voiceKey(m);
  const isSeat = !asked && !!m.seat;
  const hue = key == null ? null : voiceHue(key, isSeat);
  if (hue != null) { d.classList.add("seat"); d.style.setProperty("--seat", hueColor(hue)); }
  const face = key == null ? "" : faceHtml(key, { claude: isSeat || key === "default", hue, label: isSeat ? m.seat : said });
  d.innerHTML = `<div class="who">${face}${esc(said)}`
              + (fed ? `<span class="fed-tag" title="fed into the chat by ${esc(m.source)} - not typed by you">plugin</span>` : "")
              + `</div>`;
  if (!asked && (m.meta?.steps || []).some(s => s.kind === "said")) {
    // Said in pieces (claude): the words in order, the steps folded between.
    d.append(partsEl(m.meta.steps, current?.id));
  } else {
    const [think, words] = asked ? ["", m.content] : splitThink(m.content);
    const steps = [...(think ? [{ kind: "thinking", text: think }] : []), ...(m.meta?.steps || [])];
    if (steps.length) d.append(stepsEl(steps));
    d.append(renderMd(words, current?.id));
  }
  const atts = attachmentsEl(m.files, current?.id, m.content);
  if (atts) d.append(atts);
  layoutPictures(d, isYours(m) ? myPicturesMode() : picturesMode());
  if (!opts.bare) {
    const meta = document.createElement("div"); meta.className = "meta";
    const line = asked ? "" : receipt(m.meta);
    if (line) meta.append(Object.assign(document.createElement("span"), { className: "receipt", textContent: line }));
    meta.append(actionsEl(m, i, count));
    d.append(meta);
  }
  return d;
}

// ── Pictures in a message: full, strip or folded ────────────────────────
// A paragraph that is nothing but pictures (how a burst is written:
// ![a](..) ![b](..) ![c](..), each on its own line) is a run; consecutive ones
// are one run. Three portraits in a row were three 360px walls (21 Nov 2026,
// the over-the-frames burst). Strip puts a run of two or more side by side as
// thumbs - one alone is art and stays full. Folded puts every run, and the
// pictures a message carried, behind one line saying how many and what.
const onlyPictures = p => p.tagName === "P" && p.querySelector("img")
  && [...p.childNodes].every(n => n.nodeName === "IMG" || n.nodeName === "BR" || (n.nodeType === 3 && !n.textContent.trim()));
function pictureRuns(md) {
  const runs = []; let run = [];
  for (const el of [...md.children]) {
    if (onlyPictures(el)) { run.push(el); continue; }
    if (run.length) runs.push(run);
    run = [];
  }
  if (run.length) runs.push(run);
  return runs.map(ps => ({ ps, imgs: ps.flatMap(p => [...p.querySelectorAll("img")]) }));
}
function foldOf(imgs, what = "") {
  const names = imgs.map(i => i.alt || i.title || "").filter(Boolean);
  const box = Object.assign(document.createElement("details"), { className: "pics-fold" });
  box.innerHTML = `<summary>🖼 ${imgs.length} picture${imgs.length === 1 ? "" : "s"}${what}`
                + (names.length ? `<span>${esc(names.join(" · "))}</span>` : "") + `</summary>`;
  const strip = Object.assign(document.createElement("div"), { className: "pics strip" });
  box.append(strip);
  return [box, strip];
}
function layoutPictures(d, mode) {
  if (mode === "full") return;
  for (const md of d.querySelectorAll(".md")) {
    for (const { ps, imgs } of pictureRuns(md)) {
      if (mode === "strip" && imgs.length < 2) continue;
      let holder, put;
      if (mode === "folded") [holder, put] = foldOf(imgs);
      else holder = put = Object.assign(document.createElement("div"), { className: "pics strip" });
      ps[0].before(holder);
      for (const img of imgs) { if (img.alt && !img.title) img.title = img.alt; put.append(img); }
      ps.forEach(p => p.remove());
    }
  }
  // What the message carried: its pictures go in the fold too, its other files stay out.
  const atts = d.querySelector(":scope > .atts");
  const carried = atts ? [...atts.querySelectorAll(":scope > .att.pic")] : [];
  if (!carried.length) return;
  if (mode === "folded") {
    const [box, strip] = foldOf(carried.map(a => a.querySelector("img")), " carried");
    atts.before(box); carried.forEach(a => strip.append(a));
    if (!atts.children.length) atts.remove();
  } else if (carried.length > 1) atts.classList.add("strip");
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
         + `<span class="sum">${esc(s.summary || "")}</span> - <b>${esc(ANSWERED[s.answer] || s.answer)}</b>`
         + (s.answer === "always" && s.scope ? ` (<code>${esc(s.rule || "")}</code> in ${esc(SCOPE_NAMES[s.scope] || s.scope)})` : "")
         + `</span></div>`;
  }
  const mark = s.ok === undefined ? "…" : s.ok ? "✓" : "✗";
  // The corner opens the whole call - every argument, all that came back -
  // from the CLI's transcript; the fold keeps a preview (22 Nov 2026).
  const full = s.id ? `<button type="button" class="step-full" data-tool="${esc(s.id)}" data-cli="${esc(s.sid || "")}" `
    + `title="see all of it">${icon("expand")}</button>` : "";
  return `<div class="step tool${s.ok === false ? " bad" : ""}"><span class="mark">${mark}</span>`
       + `<span class="body"><code>${esc(s.tool)}</code> <span class="sum">${esc(s.summary || "")}</span>`
       + (s.result ? `<div class="res">${full}<pre>${esc(s.result)}</pre></div>` : full) + `</span></div>`;
}

// One tool call in full, in its own sheet.
async function openStep(toolId, cli = "") {
  const sid = current?.id;
  if (!sid) return;
  $("#sf-title").textContent = "Tool call";
  $("#sf-body").innerHTML = `<p class="muted">reading the transcript…</p>`;
  if (!$("#step-sheet").open) $("#step-sheet").showModal();
  try {
    const st = await call("GET", `/api/sessions/${encodeURIComponent(sid)}/steps/${encodeURIComponent(toolId)}`
                                 + (cli ? `?cli=${encodeURIComponent(cli)}` : ""));
    $("#sf-title").textContent = st.tool || "Tool call";
    const given = st.input === null || st.input === undefined ? "" : JSON.stringify(st.input, null, 2);
    const result = st.result === null ? "(nothing came back yet)" : st.result;
    $("#sf-body").innerHTML = `<h4>handed</h4><pre class="sf-pre">${esc(given || "(nothing)")}</pre>`
      + `<h4>${st.ok === false ? "came back as an error" : "came back"}</h4><pre class="sf-pre${st.ok === false ? " bad" : ""}">${esc(result)}</pre>`
      + (st.cut ? `<p class="muted">cut at 2 MB; the rest is in the CLI's transcript.</p>` : "");
    $("#sf-copy").onclick = () => navigator.clipboard?.writeText(result);
  } catch (e) {
    $("#sf-body").innerHTML = `<p class="muted">${esc(e.message || String(e))}</p>`;
  }
}
document.addEventListener("click", e => {
  const b = e.target.closest?.(".step-full");
  if (!b) return;
  e.preventDefault(); e.stopPropagation();
  openStep(b.dataset.tool, b.dataset.cli || "");
});

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

// An answer said in pieces (22 Oct 2026): each piece of its words where it was
// said, and the thinking and tools between pieces folded in their own place,
// instead of every step above and only the last words below. While it is
// coming, the last group of steps says what is happening now. Folds keep
// whether they are open across a redraw.
// Drawn again and again while an answer comes (every frame it grows), so
// only what CHANGED is drawn again: each piece of words and each run of
// steps keeps its element until its own text or steps move. Before (17 Nov
// 2026, Chad: "sections of text just bounce ... sometimes chunks of the main
// message, sometimes bits of the tool calls") every frame threw the whole
// answer away and built it new - every finished paragraph and tool list
// re-laid out many times a second, for as long as the thinking went on.
const stepsMark = (steps, live) => steps.map(s =>
  s.kind === "thinking" ? `t${(s.text || "").length}` : JSON.stringify(s)).join("|") + (live ? "~" : "");
function partsEl(parts, sid, liveNow = false, box = null) {
  box = box || Object.assign(document.createElement("div"), { className: "parts" });
  const old = box._slots || [], slots = [];
  let run = [];
  const flush = last => {
    if (!run.length) return;
    const was = old[slots.length], live = liveNow && last, mark = stepsMark(run, live);
    const same = was?.kind === "steps" && was.mark === mark;
    slots.push({ kind: "steps", mark, el: same ? was.el : stepsEl(run, live, was?.kind === "steps" ? was.el : null) });
    run = [];
  };
  for (const p of parts) {
    if (p.kind !== "said") { run.push(p); continue; }
    flush(false);
    const was = old[slots.length];
    slots.push(was?.kind === "said" && was.text === p.text ? was : { kind: "said", text: p.text, el: renderMd(p.text, sid) });
  }
  flush(true);
  box._slots = slots;
  // Put each element where it belongs, touching only the places that differ.
  slots.forEach((slot, i) => {
    const at = box.children[i];
    if (at !== slot.el) at ? (slot.el.parentNode === box ? box.insertBefore(slot.el, at) : at.replaceWith(slot.el)) : box.append(slot.el);
  });
  while (box.children.length > slots.length) box.lastChild.remove();
  return box;
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
  const img = e.target.closest(".md img, .atts img, .pics-fold img");
  if (img) { LB_LIST = [...$("#log").querySelectorAll(".md img, .atts img, .pics-fold img")].map(i => i.src); return lightbox(img.src); }
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
  // A link to one of this chat's files goes to the file, not to /files/... on
  // the window's own address, which is nothing (20 Nov 2026: the txt Wren
  // linked was a 404). The save button beside it takes it away.
  box.querySelectorAll("a").forEach(a => {
    const ours = (a.getAttribute("href") || "").match(OURS);
    if (!ours || !sid) return;
    a.href = fileUrl(sid, ours[1]);
    a.insertAdjacentHTML("afterend", saveEl(sid, { name: ours[1], orig: ours[1].replace(/^[0-9a-f]{8}-/, "") }));
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
function copyAll() {
  if (!current) return;
  const text = current.messages.map(m => {
    const files = (m.files || []).map(f => `[${isScreen(f) ? "screen share" : "attached"}: ${f.orig || f.name}]`);
    return [`${whoName(m)}:`, m.content, ...files].filter(Boolean).join("\n");
  }).join("\n\n");
  copyText(text, $("#chat-more"), "⋮");
}

// Is this file a picture the words already show - `![…](files/name)`? Only
// that counts as shown: a plain link to it still leaves its chip, or the one
// working handle on it is hidden behind the link (20 Nov 2026).
const escRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const shownAsPicture = (text, name) =>
  !!text && new RegExp(`!\\[[^\\]]*\\]\\(\\s*<?files/${escRe(name)}[\\s>)]`).test(text);

// What a message carried: pictures as pictures, the rest as chips, each with
// a save button. A picture its Markdown already shows is not shown twice.
// A screen-share frame is not shown in the conversation - it would bury it -
// only a chip that opens it; they all sit in the screen-share panel.
function attachmentsEl(files, sid, shownIn = "") {
  const list = (files || []).filter(f => !shownAsPicture(shownIn, f.name));
  if (!list.length || !sid) return null;
  const box = document.createElement("div"); box.className = "atts";
  box.innerHTML = list.map(f => isScreen(f)
    ? `<button type="button" class="chip shot" data-shot="${esc(fileUrl(sid, f.name))}" title="open the frame">${icon("screen")}`
      + `<span>screen · ${esc(f.source || "shared")}</span></button>`
    : isImage(f.type)
    ? `<span class="att pic"><img src="${esc(fileUrl(sid, f.name))}" alt="${esc(f.orig || f.name)}" title="${esc(f.orig || f.name)}" loading="lazy">`
      + `${saveEl(sid, f)}</span>`
    : (f.type || "").startsWith("audio/")
    ? `<span class="att"><audio class="clip" controls preload="none" src="${esc(fileUrl(sid, f.name))}" title="${esc(f.orig || f.name)}"></audio>`
      + `${saveEl(sid, f)}</span>`
    : `<span class="att"><a class="chip" href="${esc(fileUrl(sid, f.name))}" target="_blank" rel="noopener">${icon("file")}`
      + `<span>${esc(f.orig || f.name)}</span><small>${size(f.size || 0)}</small></a>${saveEl(sid, f)}</span>`).join("");
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

// The two things this page keeps an eye on, both through the baseplate's
// watch(): each is redrawn only when its answer changed.
function paintHealth(h) {
  const [cls, label] = STATUS_LABEL[h.lodestar.status] || ["down", "unknown"];
  $("#dot").className = "dot " + cls;
  $("#stat").textContent = label;
  $("#stat-pill").title = h.lodestar.detail || "";
  $("#stat-pill").classList.toggle("hot", cls === "up");
}
function healthDown() {
  HEALTH = null;
  $("#dot").className = "dot down"; $("#stat").textContent = "offline";
  $("#stat-pill").classList.remove("hot");
  healthPanels();
}
// The panels that read HEALTH follow every answer, and a lost one.
function healthPanels() {
  if (tab === "settings") drawAbout();
  if (tab === "backend") drawPings();
}

// Chats made or grown behind this window's back (the game table speaking
// into one) show up without a reload; the open chat is re-read when it grew.
const sessionsKey = list => JSON.stringify(list.map(s => [s.id, s.updated, s.folder, s.title, s.answering]));
async function paintSessions(d) {
  // already held: this window's own refresh() read the same list first
  if (sessionsKey(d.sessions) === sessionsKey(SESSIONS)) return;
  SESSIONS = d.sessions;
  for (const s of SESSIONS) s.answering ? ANSWERING.add(s.id) : ANSWERING.delete(s.id);
  drawSide(); lockComposer();
  const open = current && SESSIONS.find(s => s.id === current.id);
  if (open && open.updated !== current.updated && !ANSWERING.has(current.id) && !FOLLOWER) await openSession(current.id);
}

function startWatching() {
  // /health needs no JSON content-type and no bearer: a plain fetch, as before
  watch("/health", 15000, paintHealth, {
    fetch: async path => (await fetch(path)).json(),
    each: h => { HEALTH = h; healthPanels(); },
    onError: healthDown,
  });
  // a failed read of the chats says nothing; the next round tries again
  watch("/api/sessions", 15000, paintSessions, {
    fetch: path => call("GET", path),
    signature: d => sessionsKey(d.sessions),
  });
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
// A picture pasted or dropped INTO THE BOX goes where the cursor is, as
// `![name](pending:<id>)`, and rides the tray like any file; on sending, once
// it is uploaded, the reference becomes `files/<stored name>` and the message
// shows it right there, between your words - as an answer's pictures already
// are (Chad, 20 Nov 2026: "images intermingled in text ... I would like to be
// able to do too"). Delete the line and it goes as a plain attachment. Other
// files, and anything dropped elsewhere on the room, go to the tray as before.
const PENDING_REF = /\]\(pending:([a-z0-9]{1,12})\)/g;
function addInline(list) {
  const files = [...list], pics = files.filter(f => isImage(f.type)), rest = files.filter(f => !isImage(f.type));
  const input = $("#input"), at = input.selectionStart ?? input.value.length, end = input.selectionEnd ?? at;
  const before = PENDING.length;
  addFiles(pics);
  const refs = PENDING.slice(before).map(p => `![${p.name.replace(/[[\]]/g, "")}](pending:${p.id})`);
  if (refs.length) {
    const lead = at > 0 && !input.value.slice(0, at).endsWith("\n") ? "\n" : "";
    input.setRangeText(`${lead}${refs.join("\n")}\n`, at, end, "end");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }
  if (rest.length) addFiles(rest);
}
// The words as they go: each pending picture named by where the chat keeps it.
const withStored = (text, pending) => (text || "").replace(PENDING_REF, (whole, id) => {
  const p = (pending || []).find(x => x.id === id);
  return p?.stored ? `](files/${p.stored.name})` : whole;
});
$("#input").addEventListener("paste", e => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); addInline(files); }
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
  if (e.target.closest("#input")) addInline(e.dataTransfer.files); else addFiles(e.dataTransfer.files);
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

// ── The box's height: drag its top edge ─────────────────────────────────
// Chad, 17 Nov 2026: "being able to also enlarge the typing area (click drag
// handle) would be good too (sometimes im long winded)". The grip is the
// strip along the top of the box: up for taller, double-click for the usual
// height. This browser's to remember, like the bars' widths.
function setInputHeight(px) {
  if (px == null) { $("#composer").style.removeProperty("--input-h"); keep("symposium.inputH", null); return; }
  const h = Math.round(Math.max(56, Math.min(px, window.innerHeight * 0.6)));
  $("#composer").style.setProperty("--input-h", `${h}px`);
  return h;
}
if (Number.isFinite(local("symposium.inputH", null))) setInputHeight(local("symposium.inputH", null));
$("#input-grip").addEventListener("pointerdown", e => {
  if (e.button !== 0) return;
  e.preventDefault();
  const grip = e.currentTarget, from = $("#input").getBoundingClientRect().height, y0 = e.clientY;
  grip.setPointerCapture(e.pointerId);
  document.body.classList.add("gripping-row");
  let h = from;
  const move = ev => { h = setInputHeight(from + (y0 - ev.clientY)); toBottom(); };
  const done = () => {
    grip.removeEventListener("pointermove", move);
    document.body.classList.remove("gripping-row");
    keep("symposium.inputH", h);
  };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", done, { once: true });
  grip.addEventListener("pointercancel", done, { once: true });
});
$("#input-grip").addEventListener("dblclick", () => setInputHeight(null));

// ── Sending: streamed, shown as it comes ────────────────────────────────

// ── @ in a room: the seats, to pick from ────────────────────────────────
// Typing @ lists who is at the table, so a name is picked, not spelled (Chad,
// 17 Nov 2026: "a little pop up thinggy, that gives the list of whos in the
// room"). What is typed after the @ narrows the list; when it fits no seat
// the whole table is shown, because a typo is when the list is needed most.
// ↑ ↓ move, Enter or Tab picks, Esc puts it away until the next @.
const AT = { open: false, start: 0, pick: 0, rows: [], shut: -1 };
function atToken() {
  const box = $("#input"), upto = box.value.slice(0, box.selectionStart);
  const m = upto.match(/(?:^|[^\w@])@([\w·.-]*)$/);
  return m ? { start: upto.length - m[1].length - 1, typed: m[1] } : null;
}
function atClose() { AT.open = false; AT.pick = 0; $("#at-pop").hidden = true; }
function atDraw() {
  const seats = current?.seats || [], tok = seats.length ? atToken() : null;
  if (!tok) { AT.shut = -1; return atClose(); }
  // Put away with Esc, or a seat's whole name already typed: nothing to pick.
  if (tok.start === AT.shut || seats.some(x => x.name === tok.typed)) return atClose();
  const typed = tok.typed.toLowerCase();
  const starts = seats.filter(x => x.name.toLowerCase().startsWith(typed));
  const within = seats.filter(x => !starts.includes(x) && x.name.toLowerCase().includes(typed));
  const rows = starts.length || within.length ? [...starts, ...within] : seats;
  Object.assign(AT, { open: true, start: tok.start, rows: rows.map(x => x.name), pick: Math.min(AT.pick, rows.length - 1) });
  $("#at-pop").innerHTML = rows.map((x, i) =>
      `<button type="button" role="option" data-at="${esc(x.name)}" aria-selected="${i === AT.pick}" style="--seat:${seatColor(x.name)}">`
      + `<i class="seat-dot"></i><b>${esc(x.name)}</b><span class="note">${esc(x.cwd ? lastPart(x.cwd) : "")}</span></button>`).join("")
    + `<p class="note">↑ ↓ then Enter · they answer first, the rest follow · @name! asks only them</p>`;
  $("#at-pop").hidden = false;
}
function atPick(name) {
  const box = $("#input"), rest = box.value.slice(box.selectionStart).replace(/^ /, "");
  box.value = `${box.value.slice(0, AT.start)}@${name} ${rest}`;
  const caret = AT.start + name.length + 2;
  box.focus(); box.setSelectionRange(caret, caret); atClose();
}
// A key the list takes for itself (true): the composer never sees it, so
// Enter picks a seat instead of sending half a message.
function atKey(e) {
  if (!AT.open) return false;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    AT.pick = (AT.pick + (e.key === "ArrowDown" ? 1 : AT.rows.length - 1)) % AT.rows.length; atDraw();
  } else if ((e.key === "Enter" && !e.shiftKey && !e.isComposing) || e.key === "Tab") atPick(AT.rows[AT.pick]);
  else if (e.key === "Escape") { AT.shut = AT.start; atClose(); }
  else return false;
  e.preventDefault(); e.stopPropagation();
  return true;
}
$("#input").addEventListener("input", atDraw);
$("#input").addEventListener("click", atDraw);
$("#input").addEventListener("blur", atClose);
// mousedown, not click: the composer keeps the caret, so the pick lands where it was.
$("#at-pop").addEventListener("mousedown", e => {
  e.preventDefault();
  const row = e.target.closest("[data-at]");
  if (row) atPick(row.dataset.at);
});

$("#input").addEventListener("keydown", e => {
  if (atKey(e)) return;
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
});

$("#composer").addEventListener("submit", e => {
  e.preventDefault();
  const text = $("#input").value.trim();
  if (!text && !PENDING.length) return;
  if (current && chatBusy() && !drawing) {
    const when = interruptMode();
    WHEN_ONCE = null;                              // the choice was for this message
    queueMessage(text, when);
  } else send({ prompt: text });
});
$("#send-when").onchange = e => { WHEN_ONCE = e.target.value; $("#input").focus(); };

// ── Said while an answer is coming: it waits its turn ───────────────────
// Chad, 17 Nov 2026: "being able to send live would be good as well". A
// message sent mid-answer is not lost and not refused: it is kept on a slip
// above the box, by chat, and sent once the answer is in. It does not reach
// Claude MID-thought; that needs the CLI's streaming input, which is its own
// piece of work. × takes a slip back into the box.
//
// HOW they go is the chat's `queue` setting, over every chat's (queueMode).
// The first build sent them one at a time, and three quick thoughts became
// three replies, each blind to the other two - Chad, 17 Nov 2026: "taking
// each message at once leaves a weird lag ... like a stutter"; "meld was the
// word I was missing":
//   meld  everything waiting goes as ONE message, in the order written
//   each  one at a time, each after the one before it has been answered
//   hold  they wait until you let them go, then go as one
// And the line can be held by hand, whatever the setting (HOLD): slips are
// written before the answer they follow has been read, so they can be stale.
// Stopping an answer holds it, and so does an answer that failed - the first
// build fired the next slip the moment you said stop, so stopping took two.
// A hold lasts until you let the line go, or it empties.
const QUEUE = new Map();                       // chat id -> [{prompt, files}]
const HOLD = new Map();                        // chat id -> true (held by hand, a stop, a failure) | false (let go by hand)
let DRAINING = false;
// Chats whose answer this window stopped FOR a message ("stop and send now"): that stop lets the line go.
const CUTTING = new Set();
const lineHeld = sid => HOLD.has(sid) ? HOLD.get(sid) : queueMode() === "hold";
const holdLine = sid => { if (sid) HOLD.set(sid, true); };
// `when`: round - it waits for the answer, or the whole round; seat - the
// round is asked to end with the seat speaking; now - the answer is stopped.
// Either of the last two is you saying "send this", so the line is let go.
async function queueMessage(text, when = "round") {
  const sid = current.id, waiting = PENDING;
  $("#input").value = ""; PENDING = []; drawTray(); atClose();
  let files = [];
  try {
    // Uploaded now, so the slip holds what the chat already has, not files that may be gone by then.
    for (const p of waiting) files.push(p.stored || (p.stored = await call("POST", `/api/sessions/${sid}/files`, { name: p.name, data: await b64(p.file) })));
  } catch (ex) { alert(ex.message); }
  waiting.forEach(p => p.url && URL.revokeObjectURL(p.url));
  if (!QUEUE.has(sid)) QUEUE.set(sid, []);
  QUEUE.get(sid).push({ prompt: withStored(text, waiting), files });
  const stop = body => call("POST", `/api/sessions/${encodeURIComponent(sid)}/stop`, body);
  if (when === "seat") { HOLD.set(sid, false); stop({ after: "seat" }).catch(() => {}); }
  else if (when === "now") { HOLD.set(sid, false); CUTTING.add(sid); stop({}).catch(() => CUTTING.delete(sid)); }
  drawQueue();
}
function drawQueue() {
  const sid = current?.id, slips = sid ? QUEUE.get(sid) || [] : [];
  const busy = chatBusy();
  // A hold is the line's, not the chat's: once nothing is waiting (and no
  // answer is coming that something could still be queued behind) it is forgotten.
  if (sid && !slips.length && !busy) HOLD.delete(sid);
  $("#queue").hidden = !slips.length;
  if (!slips.length) { $("#queue").innerHTML = ""; return; }
  const held = lineHeld(sid), each = queueMode() === "each";
  const head = held
    ? `<span class="q-head held" title="nothing here is sent until you let it go"><b>held</b>`
      + `<button type="button" class="ghost" data-qgo title="${busy ? "let them go when this answer is in" : "send what is waiting now"}">`
      + `${busy ? "let go" : "send now"}</button></span>`
    : `<span class="q-head" title="${each ? "sent one at a time, each after the one before it is answered"
                                           : "sent as one message when this answer is in"}">`
      + `<b>${each ? "one at a time" : slips.length > 1 ? "as one message" : "after this answer"}</b>`
      + `<button type="button" class="ghost" data-qhold title="keep these here until you let them go">hold</button></span>`;
  $("#queue").innerHTML = head + slips.map((q, i) =>
      `<span class="slip" title="${esc(q.prompt)}"><b>${!i ? "next" : each ? "then" : "+"}</b><span>${esc(q.prompt || `${q.files.length} file(s)`)}</span>`
      + (q.files.length && q.prompt ? `<i>+${q.files.length}</i>` : "")
      + `<button type="button" data-unqueue="${i}" title="take it back into the box">×</button></span>`).join("");
}
$("#queue").onclick = e => {
  if (!current) return;
  const sid = current.id;
  if (e.target.closest("[data-qhold]")) { HOLD.set(sid, true); return drawQueue(); }
  if (e.target.closest("[data-qgo]")) { HOLD.set(sid, false); drawQueue(); return drainQueue(); }
  const x = e.target.closest("[data-unqueue]");
  if (!x) return;
  const [q] = (QUEUE.get(sid) || []).splice(Number(x.dataset.unqueue), 1);
  if (q && !$("#input").value) $("#input").value = q.prompt;
  drawQueue(); $("#input").focus();
};
function drainQueue() {
  if (DRAINING || OPENING || !current || chatBusy()) return;
  const sid = current.id, slips = QUEUE.get(sid);
  if (!slips?.length || lineHeld(sid)) return;
  // Melded: the lot, as one message, a blank line between what were separate
  // sends, every slip's files with it. One at a time: only the first.
  const going = queueMode() === "each" ? [slips.shift()] : slips.splice(0);
  const next = { prompt: going.map(q => q.prompt).filter(Boolean).join("\n\n"), files: going.flatMap(q => q.files) };
  drawQueue();
  DRAINING = true;
  send({ ...next, queued: true }).finally(() => { DRAINING = false; });
}

// `ask.queued`: a slip from the queue. It has its files already (ask.files),
// and what is in the box and the tray right now is something else you are
// writing, so neither is touched.
async function send(ask) {
  if (chatBusy()) return;
  const queued = !!ask.queued;
  delete ask.queued;
  const given = { prompt: ask.prompt, pending: queued ? [] : PENDING };
  // Only the newest answer can be retried, and nothing moves while one is coming.
  $("#log").querySelectorAll(".acts").forEach(b => b.remove());
  let sid = null, mine = null, live = null, sent = false;
  try {
    if (ask.retry) {
      // The answer being retried comes off the screen; it stays on disk until
      // its replacement is in.
      drawLog();
      if (current.messages.at(-1)?.role === "assistant") $("#log").lastChild?.remove();
      $("#log").querySelectorAll(".acts").forEach(b => b.remove());
    } else {
      // Shown at once, before any round trip (22 Oct 2026: "it takes a bit to
      // roll up"): your words, and the files on their way up by name.
      $("#log").querySelector(".blank")?.remove();
      mine = bubble({ role: "user", content: ask.prompt || "", files: queued ? ask.files : [] }, -1, 0, { bare: true });
      if (PENDING.length && !queued) mine.append(goingUp(PENDING));
      $("#log").append(mine);
      if (!queued) $("#input").value = "";
    }
    // ...and the answer starting, with a line saying what it is doing.
    live = liveBubble();
    $("#log").append(live.el); toBottom(true);
    // A session exists before its first message so the answer and the files
    // have somewhere to land; one whose first turn fails is left empty.
    await ensureSession();
    sid = current.id;
    ANSWERING.add(sid); SENDING.add(sid); lockComposer(); drawSide(); drawTitlebar();
    if (!ask.retry && !queued) {
      ask.files = await uploadPending(current.id);
      ask.prompt = withStored(ask.prompt, given.pending);
      if (!ask.screen && screenRides()) ask.screen = true;
      if (ask.files.length) {
        const full = bubble({ role: "user", content: ask.prompt || "", files: ask.files }, -1, 0, { bare: true });
        mine.replaceWith(full); mine = full;
      }
      PENDING = []; drawTray();
    }
    // true: kept. false: it failed, so what you sent goes back in the
    // composer. null: you opened another chat meanwhile - the answer is still
    // coming over there, so nothing is given back and nothing is drawn here.
    sent = true;
    const kept = await stream({ session: sid, ...ask }, live, mine, !!ask.retry);
    live = null;
    if (kept) {
      given.pending.forEach(p => p.url && URL.revokeObjectURL(p.url));
      if (ask.screen) { SCREEN.sentAt = SCREEN.at; drawTray(); drawCaption(); }
    } else if (kept === false) giveBack(ask, given);
  } catch (ex) {
    if (live) { live.stop(); live.el.remove(); }
    // Refused because the chat is answering after all (409): this window did
    // not know - the message was sent from another window, or a seat is
    // still at it. It is not an error and nothing is lost: what you sent
    // waits its turn on a slip, as if you had sent it a moment later, and
    // the answer under way is followed. 20 Nov 2026, the first room with
    // tasks in the background: Chad's message drew as sent, then a red
    // "this chat is still answering the last message", and went back to the box.
    if (ex.status === 409 && sid) {
      mine?.remove();
      ANSWERING.add(sid);
      if (!QUEUE.has(sid)) QUEUE.set(sid, []);
      QUEUE.get(sid).push({ prompt: ask.prompt || "", files: ask.files || [] });
      if (!queued) { given.pending.forEach(p => p.url && URL.revokeObjectURL(p.url)); }
      if (current?.id === sid && !FOLLOWER) followLive(sid);
      return;
    }
    if (!sent && mine) mine.remove();               // nothing went: the composer gets it back
    if (sid) { ANSWERING.delete(sid); holdLine(sid); }   // ...and nothing waiting is fired into whatever broke
    if (!sid || current?.id === sid) {
      $("#log").append(bubble({ role: "assistant", content: [ex.message, ex.body?.detail, ex.body?.hint].filter(Boolean).join(" - ") },
                              -1, 0, { cls: "err", bare: true, who: "symposium" }));
      giveBack(ask, given);
    }
  } finally {
    if (sid) SENDING.delete(sid);
    lockComposer(); drawSide();
    if ((!sid || current?.id === sid) && !document.activeElement?.closest?.("dialog")) $("#input").focus();
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
async function stream(body, live = null, asked = null, retry = false) {
  const ctrl = new AbortController();
  const r = await fetch("/api/chat/stream", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body), signal: ctrl.signal });
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    throw Object.assign(new Error(j.error || `HTTP ${r.status}`), { body: j, status: r.status });
  }
  return follow(body.session, r, ctrl, live, asked, retry);
}

// An answer already coming in this chat - opened, or come back to, mid-answer:
// the server replays it from its first event, then the rest as it happens.
async function followLive(sid) {
  const ctrl = new AbortController();
  let r;
  try { r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/live`, { signal: ctrl.signal }); } catch { return null; }
  if (r.status === 404) { ANSWERING.delete(sid); lockComposer(); drawSide(); return null; }
  if (!r.ok) return null;
  // Followed already by the time this came back (a message sent meanwhile): one bubble, not two.
  if (SENDING.has(sid) || FOLLOWER?.sid === sid) { ctrl.abort(); return null; }
  return follow(sid, r, ctrl, null, ASKED_EL, !!current?.pending?.retry);
}

// Read a turn's events into a live bubble. The bubble belongs to the chat
// `sid`; if you leave for another chat the follower is aborted (stopFollowing)
// and nothing more is drawn or given back here - the turn itself goes on.
async function follow(sid, r, ctrl, pre = null, asked = null, retry = false) {
  const status = pre || liveBubble(current?.id === sid && current.started ? current.started * 1000 : 0);
  const live = status.el;
  FOLLOWER = { sid, ctrl, el: live, asked, retry };
  if (!live.isConnected) $("#log").append(live);
  let md = live.querySelector(".md");
  let raw = "", tool = null, fresh = false, queued = false;
  const steps = [];
  let stepsBox = null;
  // Claude's answer comes in pieces with steps between them, and is drawn in
  // that order rather than started over after every tool (22 Oct 2026: what
  // it said between tools used to vanish). A local model's preamble still goes.
  const interleave = voiceOfChat() === "claude" || !!current?.seats?.length;
  const parts = [];
  let partsBox = null;
  // A room (seren_symposium.room): seat after seat in this one bubble, each
  // kept as it lands; a seat that fails doesn't end the round.
  const roomErrors = [];
  const paint = () => {
    queued = false;
    if (interleave) {
      partsBox = partsEl(parts, current?.id, true, partsBox);
      if (!partsBox.isConnected) md.replaceWith(partsBox);
      return toBottom();
    }
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
    if (ev.type === "seat") {
      // The next seat's turn: the bubble is theirs now, and starts empty.
      parts.length = 0; steps.length = 0; raw = "";
      live.querySelectorAll(".permit").forEach(p => p.remove());
      live.querySelector(".who").innerHTML = avatarHtml(ev.seat) + esc(seatWho(ev.seat));
      wearSeat(live, ev.seat);
      status.set(ev.called_by ? `${ev.seat}'s turn · ${ev.called_by} called on them` : `${ev.seat}'s turn`); paint();
      return;
    }
    if (ev.type === "pass") { status.set(`${ev.seat} passes`); return; }
    // You said stop. What it had said and done is kept, marked stopped; the
    // `saved` that follows redraws the chat with it (seren_symposium.app.keep_stopped).
    // Whatever was waiting behind this answer is held: you stopped to say
    // something, not to have the next slip fired in its place.
    if (ev.type === "stopped") { if (!CUTTING.delete(sid)) holdLine(sid); status.set(ev.kept ? "stopped - keeping what it said" : "stopped"); return; }
    // An answer put a new face on (a picture captioned avatar): wear it everywhere.
    if (ev.type === "avatar") return Promise.all([loadAvatars(), call("GET", `/api/sessions/${encodeURIComponent(sid)}`)]).then(([, s]) => {
      if (current?.id !== sid) return;
      current = s;                                  // a face for this chat only is in the chat's own settings
      drawLog(); drawSeats(); drawVoiceRows();
    });
    if (ev.type === "chunk") {
      status.set("writing");
      if (interleave) {
        const last = parts.at(-1);
        if (last?.kind === "said") last.text += ev.content; else parts.push({ kind: "said", text: ev.content });
        return later();
      }
      // After a tool runs, Lodestar starts the answer over: what came before
      // was the preamble to the call, not part of the reply.
      if (fresh) { raw = ""; fresh = false; }
      raw += ev.content;
      if (!queued) { queued = true; requestAnimationFrame(paint); }
    } else if (ev.type === "tool_status") {
      status.set(`using ${toolName(ev.tool)}`);
      if (interleave) return;
      tool = tool || live.insertBefore(Object.assign(document.createElement("div"), { className: "tool" }), md);
      tool.textContent = `calling ${ev.tool}…`; fresh = true;
    } else if (ev.type === "thinking") {
      status.set("thinking");
      const into = interleave ? parts : steps, last = into.at(-1);
      if (last?.kind === "thinking") last.text += ev.content; else into.push({ kind: "thinking", text: ev.content });
      later();
    } else if (ev.type === "tool_call") {
      (interleave ? parts : steps).push({ kind: "tool", id: ev.id, tool: ev.tool, summary: ev.summary, sid: ev.sid }); later();
    } else if (ev.type === "tool_result") {
      const s = (interleave ? parts : steps).find(x => x.id === ev.id);
      if (s) { s.ok = ev.ok; s.result = ev.preview; later(); }
      status.set("thinking");
    } else if (ev.type === "permission") {
      status.set("waiting on you");
      // Interleaved, the pieces are redrawn in their own box; the ask goes
      // under them, by the status line, where the eye already is.
      live.insertBefore(permitCard(ev), interleave ? status.line : md); toBottom(true);
    } else if (ev.type === "permission_answered") {
      status.set("thinking");
      // Replayed to a window that came back: the ask, and that it was answered.
      const card = live.querySelector(`.permit[data-permit-id="${ev.id}"]`);
      if (card && !card.classList.contains("answered")) {
        card.classList.add("answered", ev.answer);
        card.querySelector(".p-buttons").innerHTML = answeredLine(ev);
        card.querySelector(".p-always")?.remove();
      }
    } else if (ev.type === "done") {
      if (!interleave) raw = ev.response ?? raw;
      paint(); tool?.remove(); status.set("keeping it");
    } else if (ev.type === "saved") {
      kept = true;
      if (ev.seat) {
        // One seat of a room is in; the round goes on. Redraw the messages
        // and keep this bubble for the next seat - never reopen the chat
        // here, which would replay the round from its start (and loop).
        if (FOLLOWER?.ctrl === ctrl) FOLLOWER.asked = null;
        return refresh().then(async () => {
          if (current?.id !== sid) return;
          current = await call("GET", `/api/sessions/${encodeURIComponent(sid)}`);
          drawLog();
        });
      }
      // The server has it. Redraw from the file - if this chat is still the
      // one on screen; otherwise the sidebar is enough.
      ANSWERING.delete(sid);
      return refresh().then(() => { if (current?.id === sid) return openSession(sid); });
    } else if (ev.type === "round") {
      ANSWERING.delete(sid);
      // The seats were still calling on each other at the limit: said, so the
      // quiet is not mistaken for agreement.
      if (ev.capped) roomErrors.push(`The seats were still calling on each other after ${ev.volleys} turns of their own, `
        + `so the round stopped there (claude.room_volleys). Say something to carry it on, or to settle it.`);
      if (!kept) return;
      return refresh().then(async () => {
        if (current?.id !== sid) return;
        await openSession(sid);
        if (roomErrors.length) $("#log").append(bubble({ role: "assistant", content: roomErrors.join("\n\n") },
                                                       -1, 0, { cls: "err", bare: true, who: "symposium" }));
      });
    } else if (ev.type === "error") {
      if (ev.seat) { roomErrors.push(`${seatWho(ev.seat)}: ${[ev.error, ev.detail].filter(Boolean).join(" - ")}`); return; }
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
  status.stop();
  if (left) return kept ? true : null;
  if (!kept && !failed && roomErrors.length) failed = { error: roomErrors.join(" · ") };
  if (!kept && !failed) failed = { error: "the answer stopped before it was kept" };
  if (failed) {
    // Shown, never kept: the session file only ever gets turns that came back.
    holdLine(sid);                                 // what is waiting is not fired into whatever broke
    ANSWERING.delete(sid); lockComposer(); drawSide();
    live.classList.add("err"); live.classList.remove("live"); tool?.remove();
    const why = [failed.error, failed.detail, failed.hint].filter(Boolean).join(" ");
    if (interleave) parts.push({ kind: "said", text: why });
    else raw = [raw && `${raw}\n\n`, why].filter(Boolean).join(" ");
    paint();
  }
  return kept;
}

// The answer as it starts (22 Oct 2026): its bubble at once, and a line under
// it saying what it is doing and for how long, until it is done - "something
// more noticeable when you're responding" than the sidebar's dot. The
// window's title says so too, for when Symposium is behind something else.
const TITLE = document.title;
let LIVE_COUNT = 0;
const toolName = t => String(t || "a tool").split("__").pop();
const goingUp = list => Object.assign(document.createElement("div"), {
  className: "going-up", textContent: `sending ${list.map(p => p.name).join(", ")}` });

// `since`: when the turn began (ms), for a bubble drawn on coming back to a
// chat mid-answer; it counts from there, not from this look at it.
function liveBubble(since = 0) {
  const el = bubble({ role: "assistant", content: "" }, -1, 0,
                    { cls: "live", bare: true, who: voiceName(voiceOfChat()) });
  const line = Object.assign(document.createElement("div"), { className: "live-status" });
  line.innerHTML = `<span class="ls-dots"><i></i><i></i><i></i></span><span class="ls-what">thinking</span><span class="ls-time">0s</span>`
    + `<button type="button" class="ghost ls-stop" title="stop this answer - in a room, the round: what has come back is kept">stop</button>`;
  // Stop: the turn running in the chat this bubble is drawn in. The server
  // ends it and says so in the stream, so nothing is torn down here.
  line.querySelector(".ls-stop").onclick = async e => {
    const sid = FOLLOWER?.el === el ? FOLLOWER.sid : current?.id;
    if (!sid) return;
    e.target.disabled = true;
    holdLine(sid); drawQueue();                    // stop holds the line too: one stop, not one per slip
    try { await call("POST", `/api/sessions/${encodeURIComponent(sid)}/stop`, {}); }
    catch (ex) { e.target.disabled = false; }
  };
  el.append(line);
  const t0 = since || Date.now();
  const tick = () => { line.querySelector(".ls-time").textContent = `${Math.max(0, Math.round((Date.now() - t0) / 1000))}s`; };
  tick();
  const timer = setInterval(tick, 1000);
  LIVE_COUNT++; document.title = `\u25CF ${TITLE}`;
  let stopped = false;
  return {
    el, line,
    set(what) { const w = line.querySelector(".ls-what"); if (w.textContent !== what) w.textContent = what; },
    stop() {
      if (stopped) return;
      stopped = true; clearInterval(timer); line.remove();
      if (--LIVE_COUNT <= 0) { LIVE_COUNT = 0; document.title = TITLE; }
    },
  };
}

// The claude CLI's permission modes as the window offers them (22 Oct 2026:
// the CLI's list; bypassPermissions is the yaml's or the carabiner's to say,
// never one click here). "default" is the older name of manual, shown only
// where it is already set.
const MODE_LABELS = {
  manual: "manual - asks before anything that changes things",
  acceptEdits: "acceptEdits - edits files without asking",
  auto: "auto - a classifier decides what needs asking",
  plan: "plan - looks, never touches",
  dontAsk: "dontAsk - never asks; refuses what is not allowed",
  default: "default - the older name for manual",
};
function modeOptions(blank, value) {
  const keys = ["manual", "acceptEdits", "auto", "plan", "dontAsk", ...(value === "default" ? ["default"] : [])];
  return `<option value="">${esc(blank)}</option>`
    + keys.map(k => `<option value="${k}"${value === k ? " selected" : ""}>${esc(MODE_LABELS[k])}</option>`).join("");
}
const SCOPE_NAMES = { chat: "this chat", project: "this project", all: "every chat" };

// How hard Claude thinks (--effort) and what it shows of it (--thinking-display),
// in the same layers as the permissions: chat, project, every chat, carabiner,
// the CLI (22 Nov 2026). The thinking itself never comes back; a summary does.
const EFFORT_LABELS = { low: "low - quick and cheap", medium: "medium", high: "high - thinks harder",
                        xhigh: "extra high - long coding and agent work", max: "max - whatever it costs" };
const THINKING_LABELS = { summarized: "a summary of it", highlights: "highlights",
                          omitted: "nothing (it still thinks, still billed)" };
function pickOptions(labels, blank, value) {
  return `<option value="">${esc(blank)}</option>`
    + Object.entries(labels).map(([k, l]) => `<option value="${k}"${value === k ? " selected" : ""}>${esc(l)}</option>`).join("");
}

// May I? Claude wants a tool that is not on its list, and waits on you.
function permitCard(ev) {
  const card = document.createElement("div");
  card.className = "permit"; card.dataset.permitId = ev.id;
  card.innerHTML = `<div class="p-ask">${icon("gear")}<span><b>${esc(voiceName("claude"))}</b> wants to use <code>${esc(ev.tool)}</code></span></div>`
    + `<pre class="p-what">${esc(ev.summary)}</pre>`
    + `<div class="p-buttons"><button type="button" class="primary" data-permit="once">Allow once</button>`
    + `<button type="button" class="ghost danger" data-permit="deny">Deny</button></div>`
    + alwaysRow(ev);
  return card;
}

// Always, chosen in two parts (22 Oct 2026): how wide - this folder or every
// read, this MCP tool or its whole server - and where it is kept: this chat,
// this project, or every chat.
function alwaysRow(ev) {
  const rules = ev.rules?.length ? ev.rules : [ev.rule], scopes = ev.scopes || ["all"];
  return `<div class="p-always"><span class="muted">Always allow</span>`
    + (rules.length > 1
      ? `<select class="p-rule" title="how wide">${rules.map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join("")}</select>`
      : `<code class="p-rule-one">${esc(rules[0])}</code>`)
    + `<span class="muted">in</span>`
    + scopes.map(s => `<button type="button" class="ghost" data-permit="always" data-scope="${s}" `
        + `title="from now on, without asking, in ${esc(SCOPE_NAMES[s])}">${esc(SCOPE_NAMES[s])}</button>`).join("")
    + `</div>`;
}
const answeredLine = r => `<b>${esc(ANSWERED[r.answer] || r.answer)}</b>`
  + (r.answer === "always" && r.rule ? ` <span class="muted">- <code>${esc(r.rule)}</code> in ${esc(SCOPE_NAMES[r.scope] || "every chat")}</span>` : "");

$("#log").addEventListener("click", async e => {
  const b = e.target.closest("[data-permit]"), card = b?.closest(".permit");
  if (!card) return;
  card.querySelectorAll("button, select").forEach(x => { x.disabled = true; });
  const body = { id: card.dataset.permitId, answer: b.dataset.permit };
  if (b.dataset.permit === "always") {
    body.scope = b.dataset.scope;
    body.rule = card.querySelector(".p-rule")?.value || card.querySelector(".p-rule-one")?.textContent;
  }
  try {
    const r = await call("POST", "/api/permit/answer", body);
    card.classList.add("answered", b.dataset.permit);
    card.querySelector(".p-buttons").innerHTML = answeredLine(r);
    card.querySelector(".p-always")?.remove();
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

// The bar over a chat, a new one included (22 Oct 2026: "the top bar is
// empty"): its name, and which project, voice and model it answers with, the
// project's settings one click away without leaving the chat.
function drawTitlebar() {
  const fresh = !current;
  for (const id of ["#rename", "#chat-more"]) $(id).hidden = fresh;
  if (fresh) { setRenaming(false); $("#title").value = DRAFT.title || ""; $("#title").placeholder = "new chat"; }
  else $("#title").placeholder = "untitled";
  const where = current ? current.folder : folder, preset = presetFor(where);
  const model = (current ? current.settings?.model_override : DRAFT.settings.model_override) || preset.model || "";
  const chip = $("#chat-project");
  chip.innerHTML = `${icon(where ? "krater-full" : "krater")}<span>${esc(where || "no project")}</span>`
                 + `<small>${esc(voiceName(voiceOfChat()))}${model ? ` · ${esc(String(model).replace(/\.gguf$/i, ""))}` : ""}</small>`;
  chip.title = where ? `${where}: this project's settings` : "not in a project: this chat's own settings";
}
$("#chat-project").onclick = () => {
  const where = current ? current.folder : folder;
  if (where) return openProjectSheet(where);
  openChatSheet("chat");
};

// The title bar's kebab: what a chat can have done to it, the drastic ones last.
$("#chat-more").onclick = e => {
  e.stopPropagation();
  if (!current) return;
  const id = current.id;
  kebab(e.currentTarget, [["Copy all, as text", () => copyAll()],
                          ["Export as JSON", () => exportChat()],
                          [current.archived ? "Bring back from the archive" : "Archive", () => setArchived(id, !current.archived)],
                          ["Remove (nothing deleted)", () => removeChat(id)],
                          ["Delete", () => deleteChat(id), "danger"]]);
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

// A picture an answer brought (one drawn here, above all) opens with what
// made it beside it - prompt, negative, model, LoRAs and weights, steps, cfg,
// sampler, seed, size - from .../files/{name}/settings (24 Oct 2026). One
// drawn before those were kept says so; your own attachments open bare.
let LB_ASKED = 0;
// The chat's pictures, in order, when one was opened from the log: <- and ->
// go through them (folded ones too), so a burst is skimmed in the big view.
let LB_LIST = [];
function lightboxStep(by) {
  const at = LB_LIST.indexOf($("#lightbox-img").src);
  if (at < 0 || LB_LIST.length < 2) return;
  lightbox(LB_LIST[(at + by + LB_LIST.length) % LB_LIST.length]);
}
function lightbox(src) {
  $("#lightbox-img").src = src; $("#lightbox").hidden = false;
  const made = $("#lightbox-made"), ask = ++LB_ASKED;
  made.hidden = true; made.innerHTML = ""; $("#lightbox").classList.remove("with-made");
  const ours = answeredPicture(src);
  if (!ours) return;
  const shown = html => {
    if (ask !== LB_ASKED || $("#lightbox").hidden) return;   // closed, or another picture opened meanwhile
    made.innerHTML = html; made.hidden = false; $("#lightbox").classList.add("with-made");
  };
  call("GET", `${fileUrl(ours.sid, ours.name)}/settings`)
    .then(got => { made.dataset.drawn = JSON.stringify(got.drawn || null); shown(madeHtml(got)); })
    .catch(ex => shown(`<h4>What made it</h4><p class="lb-note bad">${esc(ex.body?.error || ex.message)}</p>`));
}
// A file of the open chat that an answer brought back, not a screen share:
// {sid, name}, or null for anything else (your attachment, a gallery picture).
function answeredPicture(src) {
  let path;
  try { path = new URL(src, location.href).pathname; } catch { return null; }
  const m = path.match(/^\/api\/sessions\/([^/]+)\/files\/([^/]+)$/);
  if (!m || !current || decodeURIComponent(m[1]) !== current.id) return null;
  const name = decodeURIComponent(m[2]);
  const msg = current.messages.find(x => (x.files || []).some(f => f.name === name));
  const f = msg?.files.find(f => f.name === name);
  return msg && msg.role === "assistant" && !isScreen(f) ? { sid: current.id, name } : null;
}
function madeHtml(got) {
  const d = got.drawn, head = `<h4>What made it</h4>`;
  if (!d) return head + `<p class="lb-note">${esc(got.note || "no settings were kept for this picture")}</p>`;
  const show = v => v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
  const loras = (d.loras || []).map(l => `${String(l.name || "?").replace(/\.safetensors$/i, "")}  ×${l.weight ?? "?"}`).join("\n");
  const rows = [["model", [d.model, d.arch, d.base].filter(Boolean).join(" · ")], ["vae", d.vae], ["node", d.node],
                ["size", d.width && d.height ? `${d.width} × ${d.height}` : null],
                ["seed", d.seed != null ? `${d.seed}${d.index ? ` · picture ${d.index} of ${d.count || "?"}` : ""}` : null],
                ...Object.entries(d.settings || {}).map(([k, v]) => [k, show(v)]),
                ["loras", d.loras ? (loras || "none") : null], ["prompt", d.prompt], ["negative", d.negative || (d.prompt ? "none" : null)],
                ["notes", (d.notes || []).join("; ") || null],
                ["drawn", [d.at ? new Date(d.at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : null,
                           d.seconds != null ? `in ${d.seconds}s` : null].filter(Boolean).join(" ") || null]];
  return head + `<div class="pic-facts">`
    + rows.filter(([, v]) => v != null && v !== "").map(([k, v]) => `<div class="row"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join("")
    + `</div>` + (got.note ? `<p class="lb-note">${esc(got.note)}</p>` : "")
    + `<div class="lb-go">${d.prompt ? `<button type="button" class="ghost" data-copy="prompt">copy prompt</button>` : ""}`
    + `<button type="button" class="ghost" data-copy="all" title="every setting, as JSON">copy all</button></div>`;
}
const lightboxClose = () => {
  LB_ASKED++; LB_LIST = [];
  $("#lightbox").hidden = true; $("#lightbox-img").removeAttribute("src");
  $("#lightbox-made").hidden = true; $("#lightbox").classList.remove("with-made");
};
$("#lightbox").onclick = e => {
  const made = e.target.closest("#lightbox-made");
  if (!made) return lightboxClose();                        // the picture or the dark around it: close
  const b = e.target.closest("[data-copy]");
  if (!b) return;                                           // reading, selecting: stay open
  let d = null;
  try { d = JSON.parse(made.dataset.drawn || "null"); } catch {}
  if (d) copyText(b.dataset.copy === "prompt" ? d.prompt || "" : JSON.stringify(d, null, 1), b, b.textContent);
};
document.addEventListener("keydown", e => {
  if ($("#lightbox").hidden) return;
  if (e.key === "Escape") lightboxClose();
  if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); lightboxStep(e.key === "ArrowRight" ? 1 : -1); }
});

// ── The bars: projects on the left, the panels on the right ─────────────
// Either folds to a strip so the chat gets the room (24 Oct 2026). Which are
// folded is this browser's to remember, like the tab - keep() and local()
// already swallow a storage that refuses (a private window, a locked profile).
const BARS = Object.assign({ side: true, rail: true }, local("symposium.bars", {}));
function drawBars() {
  for (const [bar, what] of [["side", "projects"], ["rail", "panels"]]) {
    const open = BARS[bar] !== false;
    $("#chat").classList.toggle(`${bar}-folded`, !open);
    const t = $(`#${bar}-toggle`);
    t.setAttribute("aria-expanded", String(open));
    t.title = open ? `fold the ${what} away` : `bring the ${what} back`;
  }
}
function foldBar(bar, open = BARS[bar] === false) {
  BARS[bar] = open;
  keep("symposium.bars", { side: BARS.side !== false, rail: BARS.rail !== false });
  drawBars();
}
$("#side-toggle").onclick = () => foldBar("side");
$("#rail-toggle").onclick = () => foldBar("rail");
drawBars();

// ...and each bar's width is yours to drag (Chad, 17 Nov 2026: "collapsing is
// great! but to widen out a screen share or other parts would be awesome").
// Each has a grip on its inner edge: drag it, double-click for the usual
// width. The width is this browser's to remember, like the fold; a folded
// bar keeps the width it will open to (--side-open / --rail-open sit under
// the fold's --side-w / --rail-w, so folding still wins while it is folded).
const WIDTHS = local("symposium.widths", {});
const BAR_LIMITS = { side: [170, 560, 0.4], rail: [220, 1100, 0.65] };    // least, most, and most as a share of the window
function setBarWidth(bar, px) {
  if (px == null) { delete WIDTHS[bar]; $("#chat").style.removeProperty(`--${bar}-open`); return; }
  const [least, most, share] = BAR_LIMITS[bar];
  // ...and never so wide that the chat between the bars has no room left to read in.
  const other = $(bar === "side" ? "#rail" : "#side").getBoundingClientRect().width;
  const w = Math.round(Math.max(least, Math.min(px, most, window.innerWidth * share, window.innerWidth - other - 380)));
  WIDTHS[bar] = w;
  $("#chat").style.setProperty(`--${bar}-open`, `${w}px`);
}
for (const bar of ["side", "rail"]) if (Number.isFinite(WIDTHS[bar])) setBarWidth(bar, WIDTHS[bar]);
document.querySelectorAll("#chat .grip").forEach(grip => {
  const bar = grip.dataset.grip;
  grip.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    document.body.classList.add("gripping");
    const box = $("#chat").getBoundingClientRect();
    const move = ev => setBarWidth(bar, bar === "side" ? ev.clientX - box.left : box.right - ev.clientX);
    const done = () => {
      grip.removeEventListener("pointermove", move);
      document.body.classList.remove("gripping");
      keep("symposium.widths", WIDTHS);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", done, { once: true });
    grip.addEventListener("pointercancel", done, { once: true });
  });
  grip.addEventListener("dblclick", () => { setBarWidth(bar, null); keep("symposium.widths", WIDTHS); });
});

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

// Claude's models are the carabiner's list (it asks Anthropic, or knows what
// it shipped with); the yaml's claude.models only until it answers, or when
// none is clipped (22 Nov 2026). Asked once per window; it keeps a while.
let claudeModelsLoaded = false;
async function loadClaudeModels() {
  if (claudeModelsLoaded) return;
  claudeModelsLoaded = true;
  try {
    const { models } = await call("GET", "/api/claude/models");
    if (models?.length) $("#claude-models").innerHTML = models.map(m =>
      `<option value="${esc(m.value)}"${m.label && m.label !== m.value ? ` label="${esc(m.label)}"` : ""}>`).join("");
  } catch { claudeModelsLoaded = false; }
}

function drawVoices() {
  const claude = CFG.claude || {};
  $("#claude-models").innerHTML = (claude.models || []).map(m => `<option value="${esc(m)}">`).join("");
  loadClaudeModels();
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

// One chat's settings: the sheet behind ⚙ in its title bar. A new chat has
// them too (22 Oct 2026): what is set before its first message waits in DRAFT
// and goes with that message. Claude takes no temperature, max tokens or
// repeat penalty, so for a chat it answers they are not shown, and the model
// field suggests Claude's models instead of the local ones.
const LOCAL_ONLY = ["temperature", "max_tokens", "repeat_penalty"];
const CHAT_NOTE = "Blank means the project's value (shown greyed); the system prompt is added after the project's. "
  + "Saved as you leave each field.";
function drawChatSettings() {
  loadModels();
  const own = current ? current.settings : DRAFT.settings, where = current ? current.folder : folder;
  const preset = presetFor(where);
  $("#s-title").value = current ? current.title : DRAFT.title;
  $("#s-move").innerHTML = [...new Set([...folderNames(), ""])].map(n => `<option value="${esc(n)}">${esc(n || "no project")}</option>`).join("");
  $("#s-move").value = where;
  for (const k of SETTINGS) {
    const el = $(`#s-${k}`), inherited = preset[PRESET[k] || k];
    el.value = own[k] ?? "";
    el.placeholder = inherited != null && inherited !== "" ? String(inherited) : "default";
  }
  $("#s-voice").options[0].textContent = `project's (${voiceName(preset.voice || "lodestar")})`;
  const everyChats = QUEUE_NAMES[CFG.queue] ? CFG.queue : "meld";
  $("#s-queue").innerHTML = `<option value="">every chat's (${esc(QUEUE_NAMES[everyChats])})</option>`
    + Object.entries(QUEUE_NAMES).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
  $("#s-queue").value = own.queue ?? "";
  const usualWhen = INTERRUPT_NAMES[CFG.interrupt] ? CFG.interrupt : "round";
  $("#s-interrupt").innerHTML = `<option value="">every chat's (${esc(INTERRUPT_NAMES[usualWhen])})</option>`
    + Object.entries(INTERRUPT_NAMES).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
  $("#s-interrupt").value = own.interrupt ?? "";
  const usualPics = PICTURE_NAMES[CFG.pictures] ? CFG.pictures : "strip";
  $("#s-pictures").innerHTML = `<option value="">every chat's (${esc(PICTURE_NAMES[usualPics])})</option>`
    + Object.entries(PICTURE_NAMES).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
  $("#s-pictures").value = own.pictures ?? "";
  const usualMine = PICTURE_NAMES[CFG.my_pictures] ? `every chat's for yours (${PICTURE_NAMES[CFG.my_pictures]})` : "as the pictures above";
  $("#s-my_pictures").innerHTML = `<option value="">${esc(usualMine)}</option>`
    + Object.entries(PICTURE_NAMES).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
  $("#s-my_pictures").value = own.my_pictures ?? "";
  // The prompts add up (22 Oct 2026): its project's, then this chat's after it.
  const inherit = own.system_inherit !== false, theirs = String(preset.system_prompt || "").trim();
  $("#s-system_inherit").checked = inherit;
  $("#s-system_prompt").placeholder = !theirs ? "none"
    : inherit ? `added after its project's: ${theirs.slice(0, 90)}${theirs.length > 90 ? "…" : ""}`
    : "this one only; its project's prompt is left out";
  const claude = voiceOfChat() === "claude";
  for (const k of LOCAL_ONLY) { $(`#s-${k}`).hidden = claude; $(`label[for="s-${k}"]`).hidden = claude; }
  $("#s-model_override").setAttribute("list", claude ? "claude-models" : "models");
  if (claude && !preset.model) $("#s-model_override").placeholder = "the CLI's default";
  $("#s-cwd").placeholder = CFG.claude?.cwd || "Symposium's own folder (claude.cwd)";
  drawSeats(); drawVoiceRows();
  // Claude's permissions for this chat, over its project's, every chat's and
  // the carabiner's, and what a turn here would run with (22 Oct 2026).
  document.querySelectorAll("#set-chat-form .claude-only").forEach(el => { el.hidden = !claude; });
  if (claude) {
    $("#s-permission_mode").innerHTML = modeOptions("its project's, every chat's or the carabiner's", own.permission_mode || "");
    $("#s-effort").innerHTML = pickOptions(EFFORT_LABELS, "its project's, every chat's or the carabiner's", own.effort || "");
    $("#s-thinking_display").innerHTML = pickOptions(THINKING_LABELS, "its project's, every chat's or the carabiner's", own.thinking_display || "");
    $("#s-allowed_tools").value = (own.allowed_tools || []).join("\n");
    $("#s-tools_inherit").checked = own.tools_inherit !== false;
    drawEffective(where, own);
  }
  $("#s-note").textContent = CHAT_NOTE + (claude
    ? " Claude takes no temperature, max tokens or repeat penalty; its model is an alias (opus, sonnet, haiku) or an exact id."
    : " Temperature, max tokens and repeat penalty are for local models; Claude keeps its own.")
    + (current ? "" : " This chat is kept, with these, once its first answer comes back.");
  // Claude's tab is the claude voice's, the sampling numbers a local model's.
  $('#cs-tabs [data-tab="claude"]').hidden = !claude;
  $('#cs-tabs [data-tab="local"]').hidden = claude;
  if ($(`#cs-tabs [data-tab="${CS_TAB}"]`).hidden) csTab("chat"); else csTab(CS_TAB, false);
}
// Which tab of the chat's sheet is showing. The stack's body is one element
// (#st-body) with its handlers on it: it moves into the Stack tab for a chat,
// and back into its own sheet for a project's stack.
let CS_TAB = "chat";
function csTab(name, fresh = true) {
  CS_TAB = name;
  document.querySelectorAll("#cs-tabs [data-tab]").forEach(b => {
    b.classList.toggle("on", b.dataset.tab === name); b.setAttribute("aria-selected", b.dataset.tab === name);
  });
  document.querySelectorAll("#set-chat-form .cs-pane").forEach(p => { p.hidden = p.dataset.pane !== name; });
  $("#s-note").hidden = name === "stack" || name === "room";
  $("#chat-settings").classList.toggle("wide", name === "stack");
  if (name === "stack" && fresh) {
    STACK_FOR = null; STACK_SEAT = "";
    $("#cs-stack").append($("#st-body"));
    $("#st-body").innerHTML = `<p class="muted">Looking…</p>`;
    drawStack();
  }
}
function openChatSheet(tab = "chat") {
  CS_TAB = tab;
  drawChatSettings();
  if (tab === "stack") csTab("stack");                    // drawn fresh, for the chat open now
  if (!$("#chat-settings").open) $("#chat-settings").showModal();
}
$("#cs-tabs").addEventListener("click", e => { const b = e.target.closest("[data-tab]"); if (b) csTab(b.dataset.tab); });
$("#cs-st-refresh").onclick = () => drawStack();
$("#chat-gear").onclick = () => openChatSheet("chat");
$("#chat-stack").onclick = () => openChatSheet("stack");
$("#chat-settings").addEventListener("click", e => { if (e.target === $("#chat-settings")) $("#chat-settings").close(); });

// One setting of the open chat changed: kept in DRAFT before the chat exists,
// patched onto it after.
function setChatSetting(k, v) {
  if (!current) {
    if (v === null) delete DRAFT.settings[k]; else DRAFT.settings[k] = v;
    drawChatSettings(); lockComposer(); picDrawHow(); return drawLog();
  }
  patch({ settings: { [k]: v } }).then(() => { drawChatSettings(); drawTitlebar(); lockComposer(); picDrawHow(); if (k === "pictures" || k === "my_pictures") drawLog(); })
    .catch(ex => { alert(ex.message); drawChatSettings(); });
}
$("#s-permission_mode").addEventListener("change", e => setChatSetting("permission_mode", e.target.value || null));
$("#s-effort").addEventListener("change", e => setChatSetting("effort", e.target.value || null));
$("#s-thinking_display").addEventListener("change", e => setChatSetting("thinking_display", e.target.value || null));
$("#s-allowed_tools").addEventListener("change", e => {
  const list = e.target.value.split(/\n+/).map(x => x.trim()).filter(Boolean);
  setChatSetting("allowed_tools", list.length ? list : null);
});
$("#s-tools_inherit").addEventListener("change", e => setChatSetting("tools_inherit", e.target.checked ? null : false));
$("#s-system_inherit").addEventListener("change", e => setChatSetting("system_inherit", e.target.checked ? null : false));

// What a turn in this chat would run with, and where each part comes from.
const LAYER_NAMES = { chat: "this chat", project: "its project", symposium: "every chat", carabiner: "the carabiner", cli: "the CLI's own" };
async function drawEffective(where, own) {
  const q = current ? `session=${encodeURIComponent(current.id)}`
    : `folder=${encodeURIComponent(where)}&settings=${encodeURIComponent(JSON.stringify(own))}`;
  try {
    const e = (await call("GET", `/api/claude?${q}`)).effective;
    const n = e.counts || {};
    const parts = [["chat", "here"], ["project", "from its project"], ["symposium", "from every chat"], ["carabiner", "from the carabiner"]]
      .filter(([k]) => n[k]).map(([k, w]) => `${n[k]} ${w}`);
    $("#s-perms").textContent = (e.mode ? `Runs as ${e.mode} (from ${LAYER_NAMES[e.mode_from] || e.mode_from}). ` : "Runs in the CLI's own mode. ")
      + `Allowed without asking: ${parts.join(", ") || "only what the CLI's own settings allow"}`
      + (e.stopped_at ? `, nothing inherited past ${LAYER_NAMES[e.stopped_at]}` : "")
      + ". The CLI's own settings always count too.";
  } catch { $("#s-perms").textContent = ""; }
}

$("#s-title").addEventListener("change", () => {
  const t = $("#s-title").value.trim();
  if (!current) { DRAFT.title = t; return drawTitlebar(); }
  patch({ title: t }).then(drawLog);
});
$("#s-move").addEventListener("change", async () => {
  if (!current) { folder = $("#s-move").value; drawSide(); drawLog(); return drawChatSettings(); }
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
    setChatSetting(k, v);
  });
}

// The Settings tab is the whole app's: who's here, the backends, the projects.
let OPEN_PROJECT = null;
function drawSettings() {
  loadModels();
  $("#a-you").value = CFG.you || "";
  $("#a-claude_name").value = CFG.claude?.name || "";
  $("#a-confirm_destructive").checked = !!CFG.confirm_destructive;
  $("#a-queue").innerHTML = Object.entries(QUEUE_NAMES).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
  $("#a-queue").value = QUEUE_NAMES[CFG.queue] ? CFG.queue : "meld";
  $("#a-interrupt").innerHTML = Object.entries(INTERRUPT_NAMES).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
  $("#a-interrupt").value = INTERRUPT_NAMES[CFG.interrupt] ? CFG.interrupt : "round";
  $("#a-pictures").innerHTML = Object.entries(PICTURE_NAMES).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
  $("#a-pictures").value = PICTURE_NAMES[CFG.pictures] ? CFG.pictures : "strip";
  $("#a-my_pictures").innerHTML = `<option value="">as the pictures above</option>`
    + Object.entries(PICTURE_NAMES).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
  $("#a-my_pictures").value = PICTURE_NAMES[CFG.my_pictures] ? CFG.my_pictures : "";
  $("#a-translate").innerHTML = Object.entries(TRANSLATE_NAMES).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
  $("#a-translate").value = TRANSLATE_NAMES[CFG.translate] ? CFG.translate : "chat";
  $("#a-prefs-file").textContent = CFG.prefs_file || "";
  drawFaceDefaults();
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
    const ks = CLAUDE.carabiner_session || {};
    rows.push(["its permission mode", ks.permission_mode || "none (the CLI's own)"],
              ["it allows", (ks.allowed_tools || []).join(", ") || "nothing more than the CLI"],
              ["its model", ks.model || "none (the CLI's own)"],
              ["how hard it thinks", ks.effort || "none (the CLI's own)"],
              ["what it shows of it", ks.thinking_display || "none (the CLI's own)"]);
  } else rows.push(["carabiner", "none clipped on this box"]);
  $("#c-facts").innerHTML = rows.map(([a, b]) => `<dt>${esc(a)}</dt><dd>${esc(b)}</dd>`).join("");
  $("#c-mode").innerHTML = modeOptions("the carabiner's, else the CLI's own", CLAUDE.mode || "");
  $("#c-ask").checked = !!CLAUDE.ask;
  $("#c-effort").innerHTML = pickOptions(EFFORT_LABELS, "the carabiner's, else the CLI's own", CLAUDE.effort || "");
  $("#c-thinking").innerHTML = pickOptions(THINKING_LABELS, "the carabiner's, else the CLI's own", CLAUDE.thinking_display || "");
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
$("#c-effort").onchange = () => setClaude({ claude_effort: $("#c-effort").value });
$("#c-thinking").onchange = () => setClaude({ claude_thinking: $("#c-thinking").value });
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

for (const k of ["you", "claude_name", "confirm_destructive", "queue", "translate", "interrupt", "pictures", "my_pictures"]) {
  $(`#a-${k}`).addEventListener("change", async e => {
    const v = e.target.type === "checkbox" ? e.target.checked : e.target.value.trim();
    try { await call("PATCH", "/api/prefs", { [k]: v }); } catch (ex) { alert(ex.message); }
    await reloadConfig(); drawSettings(); drawSide(); lockComposer(); picDrawHow(); if (current) drawLog();
  });
}

$("#p-add").onclick = () => openNewProject();

// ── A new project, in one go (22 Oct 2026) ──────────────────────────────
// "create a project, then go to the settings, edit the settings for the
// project, then go in and type to chat blows": the dialog takes the name and
// the settings, filled in from the defaults for new projects, and opens a
// chat in it.
function openNewProject() {
  const d = CFG.project_defaults || {}, claude = CFG.claude || {};
  $("#np-name").value = "";
  $("#np-voice").innerHTML = `<option value="">local model (lodestar)</option>`
    + `<option value="claude"${claude.available ? "" : " disabled"}>${esc(voiceName("claude"))}${claude.available ? "" : " (not on this machine)"}</option>`;
  $("#np-voice").value = d.voice === "claude" && claude.available ? "claude" : "";
  $("#np-model").value = d.model || "";
  $("#np-system_prompt").value = d.system_prompt || "";
  for (const k of LOCAL_ONLY) $(`#np-${k}`).value = d[k] ?? "";
  $("#np-error").hidden = true;
  npVoiceChanged();
  loadModels();
  $("#new-project").showModal(); $("#np-name").focus();
}
function npVoiceChanged() {
  const claude = $("#np-voice").value === "claude";
  document.querySelectorAll("#np-form .np-local").forEach(el => { el.hidden = claude; });
  $("#np-model").setAttribute("list", claude ? "claude-models" : "models");
  $("#np-model").placeholder = claude ? "the CLI's default" : "the default";
}
$("#np-voice").addEventListener("change", npVoiceChanged);
$("#np-form").addEventListener("submit", async e => {
  e.preventDefault();
  const name = $("#np-name").value.trim(), err = $("#np-error");
  const fail = text => { err.textContent = text; err.hidden = false; };
  if (!name) return fail("A project needs a name.");
  if (folderNames().includes(name)) return fail(`There is already a project called ${name}.`);
  const claude = $("#np-voice").value === "claude";
  // Everything the dialog shows is sent, blanks included: what you see is
  // what the project gets (a blank is no opinion, not the default again).
  const body = { name, voice: $("#np-voice").value, model: $("#np-model").value.trim(),
                 system_prompt: $("#np-system_prompt").value.trim() };
  for (const k of LOCAL_ONLY) {
    const raw = $(`#np-${k}`).value.trim();
    body[k] = claude || raw === "" ? null : (k === "max_tokens" ? parseInt(raw, 10) : parseFloat(raw));
  }
  try {
    await newProject(body);
  } catch (ex) { return fail(ex.message); }
  $("#new-project").close();
  show("chat"); $("#input").focus();
});

const PROJECT_KEYS = ["voice", "system_prompt", "model", "temperature", "max_tokens", "repeat_penalty"];
const DEFAULTS_KEY = "\u0000defaults";

// A project's settings as a form - the Settings tab's, the sheet a chat's
// project chip opens, and (without a name) the defaults for new projects.
// The three numbers go to a local model through Lodestar; the claude CLI sets
// its own (21 Oct 2026), and its model field suggests Claude's models.
function projectFields(f, { name = null } = {}) {
  const val = k => esc(f[k] ?? ""), claude = f.voice === "claude";
  return `<div class="form">`
    + (name != null ? `<label>name</label><input data-k="name" value="${esc(name)}" maxlength="120">` : "")
    + `<label>voice</label><select data-k="voice"><option value="">local model (lodestar)</option>`
    + `<option value="claude"${claude ? " selected" : ""}${CFG.claude?.available ? "" : " disabled"}>${esc(voiceName("claude"))}</option></select>`
    + `<label>system prompt</label><textarea data-k="system_prompt">${val("system_prompt")}</textarea>`
    + (name != null ? `<span></span><p class="muted"><a href="#" data-peek="1">the whole prompt stack for chats here</a>; `
        + `a chat's own prompt is added after this one.</p>` : "")
    + `<label>model</label><input data-k="model" list="${claude ? "claude-models" : "models"}" value="${val("model")}" placeholder="${claude ? "the CLI's default" : "the default"}">`
    + (claude
      ? `<span></span><p class="muted">temperature, max tokens and repeat penalty are the local model's; the claude CLI sets its own.</p>`
        // Claude's permissions for chats here, over every chat's and the carabiner's (22 Oct 2026).
        + `<label>permission mode</label><select data-k="permission_mode">${modeOptions("every chat's", f.permission_mode || "")}</select>`
        + `<label>how hard it thinks</label><select data-k="effort">${pickOptions(EFFORT_LABELS, "every chat's", f.effort || "")}</select>`
        + `<label>what it shows of its thinking</label><select data-k="thinking_display">${pickOptions(THINKING_LABELS, "every chat's", f.thinking_display || "")}</select>`
        + `<label>allowed without asking</label><textarea data-k="allowed_tools" class="rules-box" `
        + `placeholder="one per line: Read, Bash(git:*), mcp__wren-workbench__*">${esc((f.allowed_tools || []).join("\n"))}</textarea>`
        + `<span></span><label class="check"><input type="checkbox" data-k="tools_inherit"${f.tools_inherit === false ? "" : " checked"}> `
        + `also what every chat and the carabiner allow</label>`
      : `<label>temperature</label><input data-k="temperature" type="number" step="0.05" min="0" max="2" value="${val("temperature")}" placeholder="default">`
        + `<label>max tokens</label><input data-k="max_tokens" type="number" step="64" min="1" value="${val("max_tokens")}" placeholder="default">`
        + `<label>repeat penalty</label><input data-k="repeat_penalty" type="number" step="0.05" min="0" value="${val("repeat_penalty")}" placeholder="default">`)
    + (name != null ? `<span></span><div class="row-buttons"><button type="button" class="ghost danger" data-remove="1">delete project</button></div>` : "")
    + `</div>`;
}

function drawProjects() {
  const names = folderNames().filter(Boolean), d = CFG.project_defaults || {};
  const dLine = [d.voice ? voiceName(d.voice) : null, d.model, d.system_prompt ? "a system prompt" : null].filter(Boolean).join(" · ");
  const defaults = `<details class="project defaults" data-defaults="1"${OPEN_PROJECT === DEFAULTS_KEY ? " open" : ""}>`
    + `<summary>${icon("gear")}<span class="p-name">defaults for new projects</span><span class="p-sub">${esc(dLine || "none set")}</span></summary>`
    + projectFields(d) + `</details>`;
  $("#set-projects").innerHTML = defaults + (names.map(name => {
    const f = presetFor(name), count = SESSIONS.filter(s => s.folder === name).length;
    const bits = [voiceName(f.voice || "lodestar"), f.model, f.voice !== "claude" && f.temperature != null && `t=${f.temperature}`,
                  `${count} chat${count === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
    return `<details class="project" data-p="${esc(name)}"${OPEN_PROJECT === name ? " open" : ""}>`
      + `<summary>${icon("krater-full")}<span class="p-name">${esc(name)}</span><span class="p-sub">${esc(bits)}</span></summary>`
      + projectFields(f, { name }) + `</details>`;
  }).join("") || `<p class="muted">No projects yet - chats can live without one.</p>`);
}

// A field of a project form changed - a project's (data-p) or the defaults
// (data-defaults) - wherever the form is. `after(renamedTo?)` redraws.
async function projectFieldChanged(e, after) {
  const el = e.target.closest("[data-k]"), holder = e.target.closest("[data-p], [data-defaults]");
  if (!el || !holder) return;
  const k = el.dataset.k, raw = String(el.value).trim();
  let v = raw === "" ? null : raw;
  if (el.type === "checkbox") v = el.checked;
  else if (k === "allowed_tools") v = raw ? raw.split(/\n+/).map(x => x.trim()).filter(Boolean) : [];
  else if (v !== null && NUMBER.has(k)) {
    v = k === "max_tokens" ? parseInt(v, 10) : parseFloat(v);
    if (Number.isNaN(v)) return after();
  }
  try {
    if (holder.dataset.defaults) {
      const d = { ...(CFG.project_defaults || {}) };
      if (v === null || (Array.isArray(v) && !v.length)) delete d[k]; else d[k] = v;
      await call("PATCH", "/api/prefs", { project_defaults: d });
    } else {
      const name = holder.dataset.p;
      if (k === "name") {
        if (raw && raw !== name) { await renameProject(name, raw); return after(raw); }
        return after();
      }
      await call("PATCH", "/api/projects", { name, set: { [k]: v } });
    }
    await reloadConfig();
  } catch (ex) { alert(ex.message); }
  after();
}

function peekSystem(name) { return openStack(name); }

$("#set-projects").addEventListener("toggle", e => {
  const d = e.target.closest?.("details.project");
  if (!d) return;
  const key = d.dataset.defaults ? DEFAULTS_KEY : d.dataset.p;
  OPEN_PROJECT = d.open ? key : (OPEN_PROJECT === key ? null : OPEN_PROJECT);
}, true);
$("#set-projects").addEventListener("change", e => projectFieldChanged(e, () => {
  drawProjects(); drawSide(); drawTitlebar();
}));
$("#set-projects").addEventListener("click", async e => {
  const d = e.target.closest("details.project[data-p]");
  if (d && e.target.closest("[data-peek]")) { e.preventDefault(); return peekSystem(d.dataset.p); }
  if (d && e.target.closest("[data-remove]")) deleteProject(d.dataset.p);
});

// A project's settings from the chat: its chip in the title bar opens this.
function openProjectSheet(name) { drawProjectSheet(name); $("#project-sheet").showModal(); }
function drawProjectSheet(name) {
  $("#ps-body").dataset.p = name;
  $("#ps-title").textContent = `Project: ${name}`;
  $("#ps-body").innerHTML = projectFields(presetFor(name), { name });
}
$("#ps-body").addEventListener("change", e => projectFieldChanged(e, renamed => {
  const name = renamed || $("#ps-body").dataset.p;
  if ($("#project-sheet").open && CFG.folders.some(f => f.name === name)) drawProjectSheet(name);
  drawSide(); drawTitlebar(); if (!current) drawLog();
  if (tab === "settings") drawSettings();
}));
$("#ps-body").addEventListener("click", async e => {
  const name = $("#ps-body").dataset.p;
  if (e.target.closest("[data-peek]")) { e.preventDefault(); return peekSystem(name); }
  if (e.target.closest("[data-remove]")) {
    $("#project-sheet").close();
    await deleteProject(name);
    if (!current) drawLog(); else drawTitlebar();
  }
});

function exportChat() {
  if (!current) return;
  const blob = new Blob([JSON.stringify(current, null, 1)], { type: "application/json" });
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(blob),
    download: `${(current.title || "session").replace(/[^\w.-]+/g, "_").slice(0, 60)}.json` });
  a.click(); URL.revokeObjectURL(a.href);
}
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
// The translate running, if one is: {timer, key} - its clock, and the chat it
// was asked in (a draftKey). One at a time.
let PIC_TRANSLATING = null;

// ── The panel is the chat's ─────────────────────────────────────────────
// It used to be the window's: what you asked for in one chat, the prompt
// drafted from it, the negative, the seed and the plan were all still sitting
// there in the next (Chad, 17 Nov 2026: "should be a per chat panel, not have
// a carry over on text"). Now it leaves and arrives with the box's own draft
// (stashDraft / takeDraft): each chat has its own words, prompt, negative,
// seed and count, and - once it has picked them for itself - its own model,
// size and LoRAs. A chat that has not picked starts from the ones used last,
// so a new chat is not a blank cluster to choose from again. This browser
// remembers them (the last 40 chats); a plan is asked for again.
const PIC_CHATS = new Map(Object.entries(local("symposium.pictures", {})));
let PIC_OWN = false;                             // this chat picked its own model, size or LoRAs
const keepPics = () => keep("symposium.pictures", Object.fromEntries([...PIC_CHATS].slice(-40)));
const picMine = () => { PIC_OWN = true; savePic(); };
function stashPic() {
  const key = draftKey();
  const d = { text: picEl("text").value.slice(0, 8000), prompt: picEl("prompt").value.slice(0, 8000),
              negative: picEl("negative").value.slice(0, 4000), seed: picEl("seed").value, last: picEl("seed").placeholder,
              count: picEl("count").value };
  if (PIC_OWN) Object.assign(d, { model: PIC.model, size: PIC.size, loras: JSON.parse(JSON.stringify(PIC.loras)) });
  PIC_CHATS.delete(key);                         // put back last, so the newest are the ones kept
  if (d.text || d.prompt || d.negative || d.seed || PIC_OWN || Number(d.count || 1) > 1) PIC_CHATS.set(key, d);
  keepPics();
}
// The chooser under the model: this chat's way of translating, or every chat's.
function picDrawHow() {
  const sel = picEl("how"), usual = TRANSLATE_NAMES[CFG?.translate] ? CFG.translate : "chat";
  sel.innerHTML = `<option value="">every chat's (${esc(TRANSLATE_NAMES[usual])})</option>`
    + Object.entries(TRANSLATE_NAMES).map(([k, name]) => `<option value="${k}">${esc(name)}</option>`).join("");
  sel.value = translateOwn();
}
function takePic() {
  picDrawHow();
  const key = draftKey(), d = PIC_CHATS.get(key);
  PIC_CHATS.delete(key); keepPics();             // the panel holds it now, as the box holds a draft
  picEl("text").value = d?.text || ""; picEl("prompt").value = d?.prompt || ""; picEl("negative").value = d?.negative || "";
  picEl("seed").value = d?.seed || ""; picEl("seed").placeholder = d?.last || "random"; picEl("count").value = d?.count || 1;
  PIC_PLAN = null; picEl("plan-box").hidden = true; picEl("plan-box").open = false;
  // A translate still running was asked in one chat: only that chat's panel waits on it.
  const waiting = PIC_TRANSLATING?.key === key;
  picEl("prompt").readOnly = waiting; picEl("prompt").classList.toggle("waiting", waiting);
  if (!waiting) picStatus("");
  const before = PIC.model;
  PIC_OWN = !!d && "model" in d;
  Object.assign(PIC, { model: "", size: "", loras: {} },
                PIC_OWN ? { model: d.model, size: d.size, loras: d.loras || {} } : local("symposium.picture", {}));
  picEl("size").value = PIC.size || "";
  if (!PIC_OPTIONS) return;
  const sel = picEl("model");
  sel.value = PIC.model && PIC_OPTIONS.models.some(m => m.name === PIC.model) ? PIC.model : "";
  PIC.model = sel.value;
  if (PIC.model !== before) picModelChanged(); else picDrawLoras();      // the same model: only what is worn differs
}

const picEl = id => $("#pic-" + id);
const picNote = (text, bad = false) => { picEl("note").textContent = text || ""; picEl("note").classList.toggle("bad", !!bad); };
const picState = text => { picEl("state").textContent = text || ""; };
// The line under translate / plan: what translate is doing, or why it failed.
const picStatus = (text, bad = false, busy = false) => {
  const el = picEl("status");
  el.textContent = text || ""; el.hidden = !text;
  el.classList.toggle("bad", !!bad); el.classList.toggle("busy", !!busy);
};

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
    if (picModel() !== m) return;                  // another chat's model was put on meanwhile: its own answer draws it
    PIC_LORAS = got.loras || [];
    picEl("loras-hint").textContent = PIC_LORAS.length ? "click to wear one; the number is its weight" : "none fit this model";
    picDrawLoras();
  } catch (ex) { picEl("loras-hint").textContent = ex.body?.error || ex.message; }
}

// The LoRAs that fit the model, with the ones this chat wears switched on.
function picDrawLoras() {
  const m = picModel();
  if (!m) return;
  const worn = PIC.loras[m.name] || {};
  picEl("loras").innerHTML = PIC_LORAS.map(l => {
    const on = worn[l.name] != null;
    return `<span class="lora${on ? " on" : ""}" data-lora="${esc(l.name)}" title="${esc(l.fit || "")}">${esc(l.name.replace(/\.safetensors$/i, ""))}`
      + `${l.fit ? ` <span class="fit">${esc(l.fit)}</span>` : ""}`
      + `<input type="number" step="0.05" min="-2" max="2" value="${on ? worn[l.name] : (l.weight ?? 0.8)}" ${on ? "" : "hidden"}></span>`;
  }).join("");
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

// 24 Oct 2026: a translate can take a minute on a local model, and all it
// showed was a greyed button and a note down by draw, out of sight - so it
// looked like nothing happened. Now the button spins and says so, a line
// under it counts the seconds, the prompt box waits (read-only, so nothing
// typed there is lost to the draft), and a failure is said in that line.
async function picTranslate() {
  if (PIC_TRANSLATING) return;                               // Ctrl+Enter while one runs asks nothing new
  const text = picEl("text").value.trim();
  if (!text) { picStatus("say what you want to see first", true); return; }
  const m = picModel();
  const button = picEl("translate"), prompt = picEl("prompt"), started = Date.now();
  const who = voiceOfChat() === "claude" ? voiceName("claude") : "this chat's model";
  // Asked in this chat, and its answer is this chat's: if you are elsewhere
  // when it comes, it is put with that chat's panel, not into the one open.
  const job = PIC_TRANSLATING = { timer: null, key: draftKey() };
  const here = () => draftKey() === job.key;
  const how = translateMode();
  const tick = () => { if (here()) picStatus(`translating with ${who}, from ${TRANSLATE_FROM[how]}… ${Math.round((Date.now() - started) / 1000)}s`, false, true); };
  button.disabled = true; button.setAttribute("aria-busy", "true");
  button.innerHTML = `<span class="spin" aria-hidden="true"></span>translating…`;
  prompt.readOnly = true; prompt.classList.add("waiting");
  tick(); job.timer = setInterval(tick, 1000);
  try {
    // whoever answers this chat writes the draft: Claude in a Claude chat, the chat's own model otherwise
    const got = await call("POST", "/api/pictures/translate", { text, guide: m?.style?.guide || "", session: current?.id, folder, mode: how });
    if (here()) {
      prompt.value = got.prompt || "";
      picEl("negative").value = got.negative || "";
      const by = got.voice === "claude" ? voiceName("claude") : (got.model || "a local model");
      picStatus(`drafted by ${by}${got.node && got.voice !== "claude" ? " on " + got.node : ""}`
        + ` from ${TRANSLATE_FROM[got.mode] || "your words"} in ${Math.round((Date.now() - started) / 1000)}s`
        + (got.instead_of && got.why ? ` (not ${TRANSLATE_FROM[got.instead_of] || got.instead_of}: ${got.why})` : "")
        + ` - edit it as you like`);
    } else {
      PIC_CHATS.set(job.key, { ...(PIC_CHATS.get(job.key) || { text }), prompt: got.prompt || "", negative: got.negative || "" });
      keepPics();
    }
  } catch (ex) {
    const detail = typeof ex.body?.detail === "string" ? ex.body.detail : "";
    if (here()) picStatus(`translate failed: ${[ex.body?.error || ex.message, detail].filter(Boolean).join(" - ")}`, true);
  } finally {
    clearInterval(job.timer); PIC_TRANSLATING = null;
    button.disabled = false; button.removeAttribute("aria-busy"); button.textContent = "translate";
    if (here()) { prompt.readOnly = false; prompt.classList.remove("waiting"); }
  }
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
    const sid = (await ensureSession()).id;
    const got = await call("POST", "/api/pictures/draw", { session: sid, asked, why: asked, request: req });
    if (current?.id === sid) {
      await openSession(sid);
      if (got.seed != null) picEl("seed").placeholder = `last: ${got.seed}`;
    } else if (got.seed != null && PIC_CHATS.has(sid)) { PIC_CHATS.get(sid).last = `last: ${got.seed}`; keepPics(); }
    picNote(`drawn on ${got.node} with ${got.model?.name || got.model || "?"} in ${got.seconds ?? "?"}s · seed ${got.seed}`);
  } catch (ex) {
    picNote([ex.body?.error, ex.body?.detail].filter(Boolean).join(" - ") || ex.message, true);
  } finally {
    drawing = false; lockComposer(); picEl("draw").disabled = !PIC_OPTIONS;
    $("#chat .pic").classList.remove("busy");
  }
}

$("#picture-btn").onclick = () => {
  const p = $("#p-picture");
  if (BARS.rail === false) foldBar("rail", true);            // the panel is in the rail: unfold it to show it
  p.open = true;
  if (!PIC_OPTIONS) picLoadOptions();
  picEl("text").focus();
  p.scrollIntoView({ block: "nearest" });
};
$("#p-picture").addEventListener("toggle", () => { if ($("#p-picture").open && !PIC_OPTIONS) picLoadOptions(); });
picEl("reload").onclick = picLoadOptions;
picEl("model").onchange = async () => { PIC.model = picEl("model").value; picMine(); await picModelChanged(); };
picEl("size").onchange = () => { PIC.size = picEl("size").value; picMine(); };
picEl("size").value = PIC.size || "";
picEl("translate").onclick = picTranslate;
picEl("how").onchange = () => setChatSetting("translate", picEl("how").value || null);
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
  picMine();
});
picEl("loras").addEventListener("change", e => {
  if (e.target.tagName !== "INPUT") return;
  const chip = e.target.closest(".lora"), m = picModel();
  if (chip && m && chip.classList.contains("on")) { (PIC.loras[m.name] = PIC.loras[m.name] || {})[chip.dataset.lora] = Number(e.target.value); picMine(); }
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
    await ensureSession();
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

// ── What a chat's model is given, layer by layer ───────────────────────
// Design note (22 Oct 2026): the whole system prompt stack, visible per chat,
// so default tools and anything else nobody uses can be seen and cleared
// rather than guessed at. The server works it
// out (GET /api/stack, seren_symposium.stack): for a chat, a chat not made
// yet (DRAFT), or a project's own (what its chats start with).
let STACK_FOR = null;
const STACK_GROUPS = { system: "The system prompt", loaded: "Read by the CLI as it starts",
                       tools: "Tools and the rest", turn: "This turn" };
const STACK_STATES = { replaced: "replaced", off: "left out", empty: "empty", unknown: "not known yet" };
const stackChars = n => `${Number(n).toLocaleString()} chars ≈ ${Math.ceil(n / 4).toLocaleString()} tokens (est.)`;
const chips = list => list?.length ? `<div class="st-chips">${list.map(x => `<span>${esc(x)}</span>`).join("")}</div>` : "";

// A room has a stack per seat: which one the sheet is showing ("" = the first).
let STACK_SEAT = "";
async function openStack(project) {
  if (project == null) return openChatSheet("stack");      // a chat's stack is a tab of its sheet
  STACK_FOR = project;
  STACK_SEAT = "";
  $("#stack-sheet").append($("#st-body"));
  $("#st-body").innerHTML = `<p class="muted">Looking…</p>`;
  if (!$("#stack-sheet").open) $("#stack-sheet").showModal();
  await drawStack();
}
$("#st-refresh").onclick = () => drawStack();
$("#st-body").addEventListener("click", e => {
  const seat = e.target.closest("[data-seat]");
  if (seat) { STACK_SEAT = seat.dataset.seat; drawStack(); }
});
$("#stack-sheet").addEventListener("click", e => { if (e.target === $("#stack-sheet")) $("#stack-sheet").close(); });

async function drawStack() {
  const q = STACK_FOR != null ? `folder=${encodeURIComponent(STACK_FOR)}`
    : current ? `session=${encodeURIComponent(current.id)}${STACK_SEAT ? `&seat=${encodeURIComponent(STACK_SEAT)}` : ""}`
    : `folder=${encodeURIComponent(folder)}&settings=${encodeURIComponent(JSON.stringify(DRAFT.settings))}`;
  let st;
  try { st = await call("GET", `/api/stack?${q}`); }
  catch (ex) { $("#st-body").innerHTML = `<p class="muted">${esc(ex.message)}</p>`; return; }
  $("#st-title").textContent = STACK_FOR != null ? `What chats in ${STACK_FOR} start with`
    : st.seat ? `What ${seatWho(st.seat)} is given`
    : `What ${current?.title ? `“${current.title}”` : "this chat"} is given`;
  $("#cs-stack-title").textContent = $("#st-title").textContent;
  const sized = st.layers.filter(l => l.chars && !["off", "replaced", "unknown"].includes(l.state));
  const bar = st.total_chars ? sized.map(l =>
    `<span class="g-${l.group}" style="flex:${l.chars}" title="${esc(l.title)}: ${esc(stackChars(l.chars))}"></span>`).join("") : "";
  // A room: one stack per seat, each its own folder, tools and what it has yet to hear.
  const seats = st.seats?.length ? `<div class="st-seats">` + st.seats.map(n =>
      `<button type="button" class="ghost${n === st.seat ? " on" : ""}" data-seat="${esc(n)}" style="--seat:${seatColor(n)}">`
      + `<i class="seat-dot"></i>${esc(n)}</button>`).join("") + `</div>` : "";
  let html = seats + `<p class="st-total">${esc(voiceName(st.voice))}${st.folder ? ` · ${esc(st.folder)}` : ""} · `
    + `${esc(stackChars(st.total_chars))} that can be counted here`
    + (st.voice === "claude" ? "; Claude Code's own prompt and its tools' descriptions come on top." : ".") + `</p>`
    + (bar ? `<div class="st-bar">${bar}</div>` : "");
  for (const [g, title] of Object.entries(STACK_GROUPS)) {
    const rows = st.layers.filter(l => l.group === g);
    if (!rows.length) continue;
    html += `<div class="st-group">${esc(title)}${st.chars?.[g] ? ` <small>${esc(stackChars(st.chars[g]))}</small>` : ""}</div>`
      + rows.map(stackLayer).join("");
  }
  $("#st-body").innerHTML = html;
}

function stackLayer(l) {
  let title = l.title, edit = l.edit, where = l.where;
  if (STACK_FOR != null && l.key === "chat") { title = "Each chat's own"; edit = ""; where = "each chat's settings (⚙), after this project's"; }
  const size = l.chars ? stackChars(l.chars) : l.count != null ? `${l.count}` : "";
  const state = STACK_STATES[l.state] ? `<span class="st-state">${STACK_STATES[l.state]}</span>` : "";
  const button = edit ? `<button type="button" class="ghost" data-edit="${edit}">edit</button>` : "";
  return `<details class="st-layer ${esc(l.state)}" data-key="${esc(l.key)}"><summary><b>${esc(title)}</b>`
    + `<span class="muted">${esc(where || "")}</span>${state}<span class="st-size">${esc(size)}</span>${button}</summary>`
    + `<div class="st-in">${stackBody(l)}${l.note ? `<p class="note">${esc(l.note)}</p>` : ""}</div></details>`;
}

function stackBody(l) {
  if (l.key === "tools" && l.servers) {
    return `<p class="st-sub">The CLI's own (${l.builtin.length})</p>${chips(l.builtin)}`
      + l.servers.map(s => `<details class="st-server"><summary>${esc(s.name)} · `
        + `<span class="st-status ${esc(s.status)}">${esc(s.status || "?")}</span>`
        + `${s.source ? ` · ${esc(s.source)}` : ""} · ${s.tools.length} tool${s.tools.length === 1 ? "" : "s"}</summary>`
        + `${chips(s.tools)}</details>`).join("");
  }
  if (l.files) {
    return l.files.map(f => `<details class="st-server"><summary>${esc(f.path)} · ${esc(f.kind)} · ${esc(stackChars(f.chars))}</summary>`
      + `<pre class="st-text">${esc(f.text)}</pre></details>`).join("") || `<p class="muted">None found.</p>`;
  }
  if (l.key === "permissions") {
    const n = l.counts || {};
    const from = Object.entries(n).filter(([, v]) => v).map(([k, v]) => `${v} from ${LAYER_NAMES[k] || k}`).join(", ");
    return `<p>${l.mode ? `Mode: <b>${esc(l.mode)}</b> (from ${esc(LAYER_NAMES[l.mode_from] || l.mode_from)})` : "Mode: the CLI's own"}`
      + `${from ? `. Allowed without asking: ${esc(from)}` : ""}.</p>${chips(l.allow)}`;
  }
  if (l.key === "sampling") {
    return `<dl class="facts">${Object.entries(l.values || {}).map(([k, v]) =>
      `<dt>${esc(k.replace("_override", "").replace("_", " "))}</dt><dd>${esc(v ?? "Lodestar's default")}</dd>`).join("")}</dl>`;
  }
  return (l.text ? `<pre class="st-text">${esc(l.text)}</pre>` : "") + chips(l.items);
}

$("#st-body").addEventListener("click", e => {
  const b = e.target.closest("[data-edit]");
  if (!b) return;
  e.preventDefault();
  if ($("#stack-sheet").open) $("#stack-sheet").close();
  if (b.dataset.edit === "project") {
    if ($("#chat-settings").open) $("#chat-settings").close();
    return openProjectSheet(STACK_FOR ?? (current ? current.folder : folder));
  }
  openChatSheet("chat"); $("#s-system_prompt").focus();
});
