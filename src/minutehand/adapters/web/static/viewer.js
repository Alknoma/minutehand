/* The run viewer. It reads the JSON API of `minutehand view` and draws one run of any length:

   - the top bar: the run, its verdict, seed, simulated and real span, deadline and how it stopped; the headline
     numbers, each a link to its view; the failed and review findings by severity, each a link to its moment;
   - the timeline (timeline.js), the full width of the middle, one lane per kind of thing and per person, provider
     and host, binned when dense;
   - the views under it (views.js), one measure each, reached from the left;
   - the inspector on the right (inspector.js), for whatever is selected anywhere.

   Every listener is bound once, here, when the page loads, to the page's own shell or to an element a render
   replaces whole: re-drawing never adds one to anything that outlives the draw (the old page added one to its main
   element on every draw, so each click doubled the next, and the tab hung).

   Everything it loads comes from this server: the libraries are vendored under /static/vendor. */

import { API, get, RunCache } from "./api.js";
import { esc, $, words, when, span, seconds, count, plural, clip, median, remembered, remember } from "./util.js";
import { Timeline } from "./timeline.js";
import { VIEWS, VERDICT_WORD, findingTitle, batchOf } from "./views.js";
import { inspect } from "./inspector.js";

const LIST_REFRESH = 5000, RUNNING_REFRESH = 3000;
const KIND_ORDER = { fail: 0, review: 1, informational: 2 };
const STOP_WORD = {
  agent_done: "the agent reported done", wake_limit: "wake limit reached", deadline_passed: "deadline passed", nothing_pending: "nothing was due",
  agent_failed: "the agent failed", closed: "closed by its driver", environment_failed: "an emulator was unavailable"
};

const S = {
  runs: [], batches: [], run: null, base: null, cache: null, loaded: {}, view: "findings", sel: null, memo: {}, filter: "",
  handle: {}, timer: null, generation: 0, pendingWindow: null
};

/* ---------- the context views and the inspector read ---------- */

const ctx = {
  get cache() { return S.cache; },
  get base() { return S.base; },
  get sel() { return S.sel; },
  get memo() { return S.memo; },
  get filter() { return S.filter; },
  get runs() { return S.runs; },
  get batches() { return S.batches; },
  get loaded() { return S.loaded; },
  select: (ref) => select(ref, "view"),
  labelOf(ref) { const d = tl.data; const l = d && d.byRef.get(ref); return l ? d.label[l[0]] : null; },
  simOf(ref) { const d = tl.data; const l = d && d.byRef.get(ref); return l ? d.sim[l[0]] : NaN; },
  personKey(key) { const p = S.base.run.scenario.people.find((x) => x.key === key); return p ? p.name : key || "someone"; },
  personName(replyIndex) {
    const p = S.loaded.people;
    const who = p && p.people.find((x) => x.replies.some((r) => r.index === replyIndex));
    return who ? who.name : "a person";
  },
  findingTitle(n) { const x = S.base.findings.findings.find((f) => f.number === n); return x ? findingTitle(x) : "finding " + n; }
};

/* ---------- the timeline, made once ---------- */

let windowTimer = null;
const tl = new Timeline($("timeline"), {
  onSelect: (ref) => select(ref, "timeline"),
  onWindow: () => { clearTimeout(windowTimer); windowTimer = setTimeout(rememberWhere, 250); },
  onFilter: (f) => { S.filter = f; if (S.handle.onFilter) { S.handle.onFilter(); } },
  // how long each frame of the timeline took, the last hundred, for whoever measures the page
  onDraw: (d) => { const kept = window.__viewerDraws || []; kept.push(d.ms); window.__viewerDraws = kept.slice(-100); }
});

/* ---------- the location ---------- */

function where() {
  const h = new URLSearchParams(location.hash.slice(1));
  return { run: h.get("run"), view: h.get("view"), sel: h.get("sel"), clock: h.get("clock"), w: h.get("w") };
}
function rememberWhere() {
  const h = new URLSearchParams();
  if (S.run) { h.set("run", S.run); }
  if (S.view !== "findings") { h.set("view", S.view); }
  if (S.sel) { h.set("sel", S.sel); }
  if (tl.clock !== "sim") { h.set("clock", tl.clock); }
  if (tl.data) { h.set("w", Math.round(tl.win[0]) + "~" + Math.round(tl.win[1])); }
  const next = "#" + h.toString();
  if (location.hash !== next) { history.replaceState(null, "", next); }
}

/* ---------- the run picker ---------- */

function runLabel(r) {
  if (r.case) { return r.case; }
  if (r.parent_run) { return r.changed || "rerun"; }
  return words(r.scenario);
}
function renderPicker() {
  const f = $("picker-filter").value.trim().toLowerCase();
  const byParent = {};
  S.runs.forEach((r) => { (byParent[r.parent_run || ""] = byParent[r.parent_run || ""] || []).push(r); });
  const batchOfRun = {};
  S.batches.forEach((b) => b.scenarios.forEach((s) => s.samples.forEach((x) => { if (x.run_id) { batchOfRun[x.run_id] = "seed " + x.seed; } })));
  let html = "";
  const walk = (parent, depth) => (byParent[parent] || []).forEach((r) => {
    const text = runLabel(r) + " " + r.run_id + " " + r.scenario;
    if (!f || text.toLowerCase().indexOf(f) >= 0) {
      const v = r.finished ? r.verdict : "running";
      html += '<li role="option" data-open="' + esc(r.run_id) + '" class="' + (depth ? "fork" : "") + '" aria-selected="' + (r.run_id === S.run) + '" style="padding-left:' + (8 + depth * 16) + 'px">' +
        '<span><span class="name">' + esc(runLabel(r)) + '</span><div class="sub mono">' + esc(r.run_id + (batchOfRun[r.run_id] ? " · " + batchOfRun[r.run_id] : "") + (r.failed ? " · " + plural(r.failed, "failure") : "") + (r.to_review ? " · " + r.to_review + " to review" : "")) + "</div></span>" +
        '<span class="chip ' + esc(v === "running" ? "review" : v === "passed" ? "passed" : v === "failed" || v === "tool_failed" ? "fail" : "review") + '">' + esc(v === "running" ? "running" : VERDICT_WORD[v] || v) + "</span></li>";
    }
    walk(r.run_id, depth + 1);
  });
  walk("", 0);
  $("picker-list").innerHTML = html || '<li class="sub">' + (S.runs.length ? "No run matches." : "No runs yet. Play a scenario with minutehand run.") + "</li>";
}
function openPicker(open) {
  $("picker-panel").hidden = !open;
  $("picker").setAttribute("aria-expanded", String(open));
  if (open) { renderPicker(); $("picker-filter").focus(); }
}

async function loadList() {
  try {
    const [runs, batches] = await Promise.all([get(API.runs), get(API.batches)]);
    S.runs = runs.runs; S.batches = batches.batches;
    if (!$("picker-panel").hidden) { renderPicker(); }
    if (!S.run && S.runs.length) {
      const at = where();
      const known = S.runs.find((r) => r.run_id === at.run);
      open((known || S.runs[0]).run_id, known ? at : {});
    }
    if (!S.runs.length) { $("view").innerHTML = '<p class="empty">No runs yet under this state directory. Play a scenario with <code>minutehand run</code>.</p>'; }
  } catch (e) {
    $("picker-label").textContent = "Could not read the runs: " + e.message;
  }
}

/* ---------- one run ---------- */

function open(run, at) {
  clearTimeout(S.timer);
  S.generation += 1;
  S.run = run; S.cache = new RunCache(run); S.loaded = {}; S.memo = {}; S.handle = {};
  S.view = at.view && VIEWS.some((v) => v.id === at.view) ? at.view : "findings";
  S.sel = at.sel || null;
  S.pendingWindow = at.w ? at.w.split("~").map(Number) : null;
  tl.hidden = new Set();
  if (at.clock === "real" || at.clock === "sim") { tl.clock = at.clock; tl.root.querySelectorAll("[data-clock]").forEach((b) => b.setAttribute("aria-pressed", String(b.getAttribute("data-clock") === at.clock))); }
  openPicker(false);
  $("view").innerHTML = '<p class="empty">Reading the run…</p>';
  load(true);
}

async function load(fresh) {
  const generation = S.generation, run = S.run;
  const t0 = performance.now();
  try {
    const [r, findings, scorecard, timeline] = await Promise.all(["run", "findings", "scorecard", "timeline"].map((n) => S.cache.read(n)));
    if (generation !== S.generation) { return; }
    S.base = { run: r, findings, scorecard: scorecard.scorecard, timeline };
    timeline.run = run;
    tl.setData(timeline, findings.findings);
    if (fresh) {
      if (S.pendingWindow && S.pendingWindow.every(isFinite)) { tl.setWindow(S.pendingWindow[0], S.pendingWindow[1], false); } else { tl.fit(); }
    }
    renderHeader(); renderNav();
    if (fresh) { await renderView(); } else if (S.handle.refresh) { S.handle.refresh(); }
    select(S.sel, fresh && !S.pendingWindow ? "location" : "refresh");
    window.__viewerLoaded = { run, ms: Math.round(performance.now() - t0), marks: timeline.marks.sim.length };
    $("live").hidden = r.finished;
    prefetch(generation);
    if (!r.finished) {
      S.timer = setTimeout(() => { if (generation === S.generation) { S.cache.forget(); load(false); } }, RUNNING_REFRESH);
    }
  } catch (e) {
    if (generation === S.generation) { $("view").innerHTML = '<p class="empty">Could not read this run: ' + esc(e.message) + "</p>"; }
  }
}

/** What the headline numbers need beyond the first screen, read once it is drawn. */
async function prefetch(generation) {
  const names = [["people", "people"], ["traffic", "modelTraffic"], ["assessments", "assessments"]];
  await Promise.all(names.map(async ([key, path]) => {
    try { const v = await S.cache.read(path); if (generation === S.generation) { S.loaded[key] = v; } } catch (e) { /* the view says why when opened */ }
  }));
  if (generation === S.generation) { renderHeader(); renderNav(); }
}

/* ---------- the top bar ---------- */

function renderHeader() {
  const b = S.base, r = b.run, sc = r.scenario, rec = r.record, card = b.scorecard;
  const row = S.runs.find((x) => x.run_id === r.run_id);
  $("picker-label").textContent = (row ? runLabel(row) : words(sc.name)) + " · " + r.run_id;
  const verdict = b.findings.verdict;
  const vKind = r.finished && verdict ? verdict.kind : "running";
  const simSecs = (Date.parse(r.reached) - Date.parse(sc.starts_at)) / 1000;
  const facts = [
    ["seed", rec ? rec.seed : sc.seed !== null && sc.seed !== undefined ? sc.seed : "—"],
    ["simulated", when(sc.starts_at, { year: true }) + " → " + when(r.reached, { year: true }) + " (" + span(simSecs) + ")"],
    ["real", rec ? span(rec.wall_seconds) : "running"],
    ["deadline", sc.deadline_after ? span(seconds(sc.deadline_after)) + " in" : "none"],
    ["stopped", rec ? STOP_WORD[rec.stop] || words(rec.stop) : "not yet"]
  ];
  $("identity").innerHTML = '<span class="verdict ' + esc(vKind) + '" title="' + esc(verdict ? verdict.words : "Still running") + '">' + esc(vKind === "running" ? "Running" : VERDICT_WORD[vKind]) + "</span>" +
    "<h1>" + esc(words(sc.name)) + "</h1>" +
    '<span class="facts">' + facts.map((f) => esc(f[0]) + " <b>" + esc(f[1]) + "</b>").join("") + (row && row.parent_run ? " <b>fork of " + esc(row.parent_run) + "</b>" : "") + "</span>";
  document.title = words(sc.name) + " · " + (vKind === "running" ? "running" : VERDICT_WORD[vKind]) + " · Minutehand";

  const f = b.findings.findings, fails = f.filter((x) => x.finding.kind === "fail").length, reviews = f.filter((x) => x.finding.kind === "review").length;
  const a = S.loaded.assessments, held = a ? a.rules.filter((x) => x.status === "passed").length : null;
  const replies = S.loaded.people ? S.loaded.people.people.flatMap((p) => p.replies.map((x) => x.reply.drawn ? (Date.parse(x.reply.at) - Date.parse(x.reply.drawn.asked_at)) / 1000 : null)).filter((x) => x !== null) : null;
  const tokens = S.loaded.traffic && S.loaded.people ? S.loaded.traffic.calls.reduce((s, c) => s + (c.input_tokens || 0) + (c.output_tokens || 0), 0) +
    S.loaded.people.people.reduce((s, p) => s + p.model_calls.reduce((t, m) => t + (m.call.input_tokens || 0) + (m.call.output_tokens || 0), 0), 0) : null;
  const numbers = [
    ["findings", r.finished ? String(fails) : "—", fails ? "failed" + (reviews ? ", " + reviews + " to review" : "") : reviews ? reviews + " to review" : "failed", fails ? "fail" : reviews ? "review" : r.finished ? "pass" : ""],
    ["assessments", a ? (a.rules.length && a.rules.some((x) => x.status !== "not_checked") ? held + "/" + a.rules.length : a.rules.length ? "—" : "none") : "…", "rules held", a && a.rules.some((x) => x.status === "failed") ? "fail" : ""],
    ["followups", card ? String(card.follow_ups_made) : "—", "follow-ups", ""],
    ["replies", replies ? (replies.length ? span(median(replies)) : "—") : "…", "median reply", ""],
    ["response", card && card.slowest_reaction ? span(seconds(card.slowest_reaction)) : "—", "slowest comeback", ""],
    ["cost", tokens === null ? "…" : count(tokens), "model tokens", ""],
    ["conversations", card ? String(card.messages_to_people) : "—", "messages sent", ""]
  ];
  $("headline").innerHTML = numbers.map((n) => '<a href="#" data-view="' + n[0] + '"><span class="v ' + n[3] + '">' + esc(n[1]) + '</span><span class="k">' + esc(n[2]) + "</span></a>").join("");

  const problems = f.filter((x) => x.finding.kind !== "informational").sort((p, q) => KIND_ORDER[p.finding.kind] - KIND_ORDER[q.finding.kind] || p.number - q.number);
  $("problems").innerHTML = !r.finished ? '<span class="none">The checks run when the run finishes.</span>'
    : problems.length ? problems.slice(0, 30).map((x) => '<button type="button" class="problem" data-sel="finding:' + x.number + '" aria-pressed="' + (S.sel === "finding:" + x.number) + '" title="' + esc(x.finding.message) + '"><span class="chip ' + x.finding.kind + '">' + esc(x.finding.kind) + '</span><span class="t">' + esc(findingTitle(x) + ": " + clip(x.finding.message, 90)) + "</span></button>").join("") +
      (problems.length > 30 ? '<a href="#" data-view="findings" class="none">and ' + (problems.length - 30) + " more</a>" : "")
      : '<span class="none">' + esc(verdict ? verdict.words : "No check failed.") + "</span>";
}

function renderNav() {
  let html = "", group = null;
  for (const v of VIEWS) {
    if (v.group !== group) { group = v.group; html += "<h2>" + esc(group) + "</h2>"; }
    let c = { n: "" };
    try { c = v.count(ctx); } catch (e) { c = { n: "" }; }
    html += '<a href="#" data-view="' + v.id + '"' + (S.view === v.id ? ' aria-current="page"' : "") + "><span>" + esc(v.label) + '</span><span class="n ' + (c.cls || "") + '">' + esc(c.n) + "</span></a>";
  }
  $("views").innerHTML = html;
}

async function renderView() {
  const v = VIEWS.find((x) => x.id === S.view) || VIEWS[0], generation = S.generation, host = $("view");
  S.handle = {};
  host.innerHTML = '<p class="empty">Reading…</p>';
  try {
    const handle = await v.render(host, ctx);
    if (generation === S.generation && S.view === v.id) { S.handle = handle || {}; }
  } catch (e) {
    if (generation === S.generation) { host.innerHTML = '<p class="empty">Could not read this view: ' + esc(e.message) + "</p>"; }
  }
}

function showView(id) {
  if (!VIEWS.some((v) => v.id === id)) { return; }
  S.view = id;
  renderNav();
  renderView();
  rememberWhere();
}

/* ---------- selection: one thing at a time, from anywhere ---------- */

function select(ref, from) {
  S.sel = ref || null;
  let refs = ref ? [ref] : [], focus = ref;
  if (ref && ref.indexOf("finding:") === 0 && S.base) {
    const x = S.base.findings.findings.find((f) => "finding:" + f.number === ref);
    if (x) {
      refs = [ref].concat(x.finding.evidence.map((s) => "ev:" + s));
      focus = refs.find((r) => tl.data && tl.data.byRef.has(r)) || null;
    }
  }
  tl.select(refs, focus);
  if (ref && from !== "timeline" && from !== "refresh") {
    const fm = ref.indexOf("finding:") === 0 && tl.data ? tl.data.findings.find((f) => "finding:" + f.number === ref) : null;
    tl.reveal(refs.filter((r) => tl.data && tl.data.byRef.has(r)), fm ? [fm.sim] : []);
  }
  document.querySelectorAll(".problem").forEach((b) => b.setAttribute("aria-pressed", String(b.getAttribute("data-sel") === S.sel)));
  if (S.handle.onSelect) { S.handle.onSelect(S.sel); }
  if (from !== "refresh") { renderInspector(); }
  rememberWhere();
}

async function renderInspector() {
  const ref = S.sel, box = $("inspector");
  if (!S.base) { return; }
  if (ref) { box.innerHTML = '<p class="insp-empty">Reading…</p>'; }
  const html = await inspect(ctx, ref);
  if (S.sel === ref) { box.innerHTML = html; box.scrollTop = 0; }
}

/* ---------- every listener, bound once ---------- */

document.addEventListener("click", (ev) => {
  const sel = ev.target.closest("[data-sel]");
  if (sel) { ev.preventDefault(); select(sel.getAttribute("data-sel"), "page"); return; }
  const openRun = ev.target.closest("[data-open]");
  if (openRun) { ev.preventDefault(); open(openRun.getAttribute("data-open"), {}); return; }
  const view = ev.target.closest("[data-view]");
  if (view) { ev.preventDefault(); showView(view.getAttribute("data-view")); return; }
  if (ev.target.closest("#picker")) { openPicker($("picker-panel").hidden); return; }
  if (!ev.target.closest("#picker-panel")) { openPicker(false); }
  if (ev.target.closest("#theme")) { toggleTheme(); }
  if (ev.target.closest("#help-button")) { help(true); } else if (ev.target.closest("#help")) { help(false); }
});
$("picker-filter").addEventListener("input", renderPicker);

document.addEventListener("keydown", (ev) => {
  const typing = ev.target.closest("input, textarea, select");
  if (ev.key === "Escape") {
    if (!$("help").hidden) { help(false); return; }
    if (!$("picker-panel").hidden) { openPicker(false); return; }
    if (typing) { if (ev.target === tl.filterEl && tl.filterEl.value) { tl.setFilter(""); S.filter = ""; if (S.handle.onFilter) { S.handle.onFilter(); } } ev.target.blur(); return; }
    if (S.sel) { select(null, "page"); }
    return;
  }
  if (typing || ev.metaKey || ev.ctrlKey || ev.altKey) { return; }
  const k = ev.key;
  if (k === "/") { ev.preventDefault(); tl.filterEl.focus(); tl.filterEl.select(); return; }
  if (k === "?") { help($("help").hidden); return; }
  if (!S.base) { return; }
  if (k === "j" || k === "k") { const ref = tl.step(k === "j" ? 1 : -1); if (ref) { select(ref, "keys"); } return; }
  if (k === "ArrowLeft" || k === "ArrowRight") { ev.preventDefault(); tl.pan((k === "ArrowLeft" ? -1 : 1) * (ev.shiftKey ? 0.5 : 0.2)); return; }
  if (k === "+" || k === "=") { tl.zoom(0.5); return; }
  if (k === "-" || k === "_") { tl.zoom(2); return; }
  if (k === "0") { tl.fit(); return; }
  if (k === "f") {
    const order = Array.from(document.querySelectorAll(".problem")).map((b) => b.getAttribute("data-sel"));
    if (order.length) { select(order[(order.indexOf(S.sel) + 1) % order.length], "keys"); }
    return;
  }
  if (k === "[" || k === "]") {
    const i = VIEWS.findIndex((v) => v.id === S.view), n = VIEWS.length;
    showView(VIEWS[(i + (k === "]" ? 1 : n - 1)) % n].id);
    return;
  }
  if (k === "c") { tl.setClock(tl.clock === "sim" ? "real" : "sim"); rememberWhere(); }
});

window.addEventListener("hashchange", () => {
  const at = where();
  if (at.run && at.run !== S.run) { open(at.run, at); return; }
  if (at.view && at.view !== S.view) { showView(at.view); }
  if ((at.sel || null) !== S.sel) { select(at.sel, "page"); }
});

/* ---------- theme and help ---------- */

function applyTheme(t) { if (t === "light" || t === "dark") { document.documentElement.setAttribute("data-theme", t); } else { document.documentElement.removeAttribute("data-theme"); } }
function toggleTheme() {
  const dark = document.documentElement.getAttribute("data-theme") ? document.documentElement.getAttribute("data-theme") === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  applyTheme(next); remember("minutehand.theme", next);
  tl.request();
  if (S.base) { renderView(); }
}
function help(show) {
  const box = $("help");
  if (show && !box.innerHTML) {
    box.innerHTML = '<div class="card"><h2>Keys</h2><dl>' + [
      ["j / k", "the next or previous mark on the lanes shown, selected"], ["← / →", "pan the timeline (shift: further)"], ["+ / −", "zoom in or out"],
      ["0", "the whole run"], ["c", "the simulated or the real clock"], ["/", "filter the marks and the calls"], ["f", "the next finding to act on"],
      ["[ / ]", "the previous or next view"], ["Esc", "clear the filter or the selection"], ["?", "this"]
    ].map((p) => "<dt><kbd>" + esc(p[0]) + "</kbd></dt><dd>" + esc(p[1]) + "</dd>").join("") +
      "</dl><p class=\"muted\">On the timeline: drag to pan, wheel or pinch to zoom, shift+wheel to pan. Click a lane's name to hide it.</p></div>";
  }
  box.hidden = !show;
}

applyTheme(remembered("minutehand.theme", ""));
loadList();
setInterval(loadList, LIST_REFRESH);
