/* The run viewer. It reads the JSON API of `minutehand view` and draws one run of any length:

   - the top bar: the run, its verdict, seed, simulated and real span, deadline and how it stopped; the headline
     numbers, each a link to its view;
   - one page at a time beside the rail on the left: the calendar the run opens on, the timeline (timeline.js, one
     lane per kind of thing and per person, provider and host, binned when dense), and the views (views.js), one
     measure each;
   - the inspector (inspector.js), a column that opens on the right for whatever is selected, and closes with it.

   Every listener is bound once, here, when the page loads, to the page's own shell or to an element a render
   replaces whole: re-drawing never adds one to anything that outlives the draw (the old page added one to its main
   element on every draw, so each click doubled the next, and the tab hung).

   Everything it loads comes from this server: the libraries are vendored under /static/vendor. */

import { API, get, RunCache } from "./api.js";
import { esc, $, words, when, span, seconds, plural, remembered, remember } from "./util.js";
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
  runs: [], batches: [], run: null, base: null, cache: null, loaded: {}, view: "time", sel: null, memo: {}, filter: "",
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
  scaleTo: (sc, at) => scaleTo(sc, at),
  // the stretch the list reads: where a picked scale is going, while the axis animates there; else what it shows
  window: () => (S.going && Date.now() < S.going.until ? S.going.win.slice() : tl.win.slice()),
  remember: () => rememberWhere(),
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

let windowTimer = null, listTimer = null;
const tl = new Timeline($("timeline"), {
  onSelect: (ref) => select(ref, "timeline"),
  onWindow: () => {
    clearTimeout(windowTimer); windowTimer = setTimeout(rememberWhere, 250);
    clearTimeout(listTimer); listTimer = setTimeout(() => { if (S.handle.onWindow) { S.handle.onWindow(); } }, 120);
  },
  onFilter: (f) => { S.filter = f; if (S.handle.onFilter) { S.handle.onFilter(); } },
  // how long each frame of the timeline took, the last hundred, for whoever measures the page
  onDraw: (d) => { const kept = window.__viewerDraws || []; kept.push(d.ms); window.__viewerDraws = kept.slice(-100); }
});

/* ---------- the location ---------- */

function where() {
  const h = new URLSearchParams(location.hash.slice(1));
  return { run: h.get("run"), view: h.get("view"), sel: h.get("sel"), clock: h.get("clock"), w: h.get("w"), scale: h.get("scale"), at: h.get("at"), rows: h.get("rows") };
}
function rememberWhere() {
  const h = new URLSearchParams();
  if (S.run) { h.set("run", S.run); }
  if (S.view !== "time") { h.set("view", S.view); }
  if (S.view === "time" && S.memo.scale && S.memo.scale !== "all") { h.set("scale", S.memo.scale); h.set("at", String(S.memo.at)); }
  if (S.memo.allRows) { h.set("rows", "all"); }
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
        '<span class="chip ' + esc(v === "running" ? "review" : v === "passed" ? "passed" : v === "failed" || v === "tool_failed" || v === "simulation_incomplete" ? "fail" : "review") + '">' + esc(v === "running" ? "running" : VERDICT_WORD[v] || v) + "</span></li>";
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
  S.run = run; S.cache = new RunCache(run); S.loaded = {}; S.handle = {};
  S.memo = { scale: SCALES.some((f) => f[0] === at.scale) ? at.scale : "all", at: at.at ? Number(at.at) : null, allRows: at.rows === "all" };
  S.view = at.view && VIEWS.some((v) => v.id === at.view) ? at.view : "time";
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
    tl.setData(derive(timeline, findings.items, S.memo.allRows), findings.findings);
    if (fresh) {
      if (S.pendingWindow && S.pendingWindow.every(isFinite)) { tl.setWindow(S.pendingWindow[0], S.pendingWindow[1], false); } else if (S.memo.scale !== "all") { scaleTo(S.memo.scale, S.memo.at, false); } else { tl.fit(); }
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
  const names = [["people", "people"], ["modelUse", "modelUse"], ["assessments", "assessments"]];
  await Promise.all(names.map(async ([key, path]) => {
    try { const v = await S.cache.read(path); if (generation === S.generation) { S.loaded[key] = v; } } catch (e) { /* the view says why when opened */ }
  }));
  if (generation === S.generation) { renderHeader(); renderNav(); }
}

/* ---------- the top bar ---------- */

/** The top bar says which run this is and whether it passed: the scenario and run, the agent's code as it ran (its
    commit, and whether uncommitted changes were part of it), what a fork changed from its parent, the verdict, and how
    much time was simulated in how little. What went wrong is the pages' to show: a long run has too much of it for a
    bar. Reference facts (seed, the span in dates, deadline, how it stopped) are in the title of the time. */
function renderHeader() {
  const b = S.base, r = b.run, sc = r.scenario, rec = r.record;
  const row = S.runs.find((x) => x.run_id === r.run_id);
  $("picker-label").textContent = (row ? runLabel(row) : words(sc.name)) + " · " + r.run_id;
  const verdict = b.findings.verdict;
  const vKind = r.finished && verdict ? verdict.kind : "running";
  const simSecs = (Date.parse(r.reached) - Date.parse(sc.starts_at)) / 1000, simDays = simSecs / 86400;
  const simulated = (simDays >= 2 ? Math.round(simDays) + " days" : span(simSecs)) + " simulated" + (rec ? " in " + span(rec.wall_seconds) : " so far");
  const details = ["seed " + (rec ? rec.seed : sc.seed !== null && sc.seed !== undefined ? sc.seed : "—"), when(sc.starts_at) + " → " + when(r.reached),
    "deadline " + (sc.deadline_after ? span(seconds(sc.deadline_after)) + " in" : "none"), "stopped: " + (rec ? STOP_WORD[rec.stop] || words(rec.stop) : "not yet")].join(" · ");
  const p = r.program;
  const agent = p ? '<span class="agent-code" title="' + esc(p.command.join(" ") + "\nin " + p.directory) + '">agent ' + esc(p.commit ? p.commit.slice(0, 7) : p.code ? "code " + p.code.slice(0, 7) : "code not named") + (p.uncommitted ? " + uncommitted changes" : "") + "</span>" : "";
  const fork = row && row.parent_run ? '<a href="#" class="fork" data-open="' + esc(row.parent_run) + '">fork of ' + esc(row.parent_run) + "</a>" + (row.changed ? '<span class="changed">' + esc("changed: " + row.changed) + "</span>" : "") : "";
  const early = rec && ABNORMAL_STOP.has(rec.stop) ? '<span class="stopped">' + esc("Stopped early: " + (STOP_WORD[rec.stop] || words(rec.stop))) + "</span>" : "";
  $("identity").innerHTML = '<span class="verdict ' + esc(vKind) + '" title="' + esc(verdict ? verdict.words : "Still running") + '">' + esc(vKind === "running" ? "Running" : VERDICT_WORD[vKind]) + "</span>" +
    agent + fork + early + '<span class="spacer"></span><span class="simulated" title="' + esc(details) + '">' + esc(simulated) + "</span>";
  document.title = words(sc.name) + " · " + (vKind === "running" ? "running" : VERDICT_WORD[vKind]) + " · Minutehand";
}

const ABNORMAL_STOP = new Set(["wake_limit", "agent_failed", "closed", "environment_failed"]);

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
  // the run over time is one page in the format picked; the timeline's lanes are one of its formats
  // the run over time is one page: its axis, at the scale picked, and the list of what the axis shows under it
  document.querySelector(".center").classList.toggle("time-page", v.id === "time");
  renderScales();
  S.handle = {};
  // each draw gets a page of its own: a view still reading when another is opened draws into a page no longer shown
  host.innerHTML = '<div class="view-page"><p class="empty">Reading…</p></div>';
  const page = host.firstChild;
  try {
    const handle = await v.render(page, ctx);
    if (generation === S.generation && S.view === v.id && page.isConnected) { S.handle = handle || {}; }
  } catch (e) {
    if (generation === S.generation) { page.innerHTML = '<p class="empty">Could not read this view: ' + esc(e.message) + "</p>"; }
  }
}

/* ---------- the run over time: one axis, its scale picked ---------- */

const SCALES = [["all", "Whole run"], ["month", "Month"], ["week", "Week"], ["day", "Day"]];
const UNIT = { month: () => d3.utcMonth, week: () => d3.utcMonday, day: () => d3.utcDay };

/** The timeline's data for the time page: three rows read first (when the agent woke, each of its outputs in its
    result's colour, each reply from a person), then, with every row shown, the record's own lanes. */
function derive(tl, items, all) {
  const result = new Map((items || []).map((i) => [ "ev:" + i.seq, outcomeOf(i) ]));
  // the outputs' row is banded by result, so a mark's height in it says what the checks made of it
  const bands = [["out_failed", "failed"], ["out_review", "to review"], ["out_held", "passed"], ["out_unmeasured", "not measured"]];
  const lanes = [{ key: "sum:woke", kind: "wakes", label: "Agent woke", marks: 0, height: 18 },
    { key: "sum:outputs", kind: "wakes", label: "Outputs", marks: 0, height: 64, bands },
    { key: "sum:heard", kind: "person", label: "Replies from people", marks: 0 }];
  const drawn = new Set(), m = tl.marks, out = { sim: [], sim_end: [], real: [], real_end: [], lane: [], kind: [], ref: [], label: [] };
  const push = (i, lane, kind) => {
    out.sim.push(m.sim[i]); out.sim_end.push(kind === "wake" ? null : m.sim_end[i]); out.real.push(m.real[i]); out.real_end.push(m.real_end[i]);
    out.lane.push(lane); out.kind.push(kind); out.ref.push(m.ref[i]); out.label.push(m.label[i]); lanes[lane].marks += 1;
  };
  if (all) { tl.lanes.forEach((l) => lanes.push(Object.assign({}, l, { marks: 0 }))); }
  for (let i = 0; i < m.sim.length; i++) {
    if (m.kind[i] === "wake") { push(i, 0, "wake"); }
    if (result.has(m.ref[i]) && !drawn.has(m.ref[i])) { drawn.add(m.ref[i]); push(i, 1, "out_" + result.get(m.ref[i])); }
    if (m.kind[i] === "said") { push(i, 2, "said"); }
    if (all) { push(i, 3 + m.lane[i], m.kind[i]); }
  }
  // keep the record's order: by simulated time, then as written
  const order = out.sim.map((_, k) => k).sort((p, q) => out.sim[p] - out.sim[q] || p - q);
  const sorted = {};
  for (const key in out) { sorted[key] = order.map((k) => out[key][k]); }
  return Object.assign({}, tl, { lanes, marks: sorted });
}
function outcomeOf(item) {
  const said = item.checks.map((c) => c.outcome);
  return said.indexOf("failed") >= 0 ? "failed" : said.indexOf("review") >= 0 ? "review" : said.indexOf("held") >= 0 ? "held" : "unmeasured";
}

/** The scale bar: the scale, the period it is on with a step either way, and whether every row is shown. */
function renderScales() {
  const bar = $("formats");
  bar.hidden = S.view !== "time" || !S.base;
  if (bar.hidden) { return; }
  const sc = S.memo.scale || "all", at = S.memo.at;
  const label = sc === "all" ? "" : sc === "month" ? d3.utcFormat("%B %Y")(new Date(at)) : sc === "week" ? "Week of " + d3.utcFormat("%-d %B %Y")(new Date(at)) : d3.utcFormat("%A %-d %B %Y")(new Date(at));
  bar.innerHTML = '<span class="seg">' + SCALES.map((f) => '<button type="button" data-scale="' + f[0] + '" aria-pressed="' + (sc === f[0]) + '">' + f[1] + "</button>").join("") + "</span>" +
    (sc !== "all" ? '<span class="on-day"><button type="button" data-step="-1" aria-label="Before">‹</button><b>' + esc(label) + '</b><button type="button" data-step="1" aria-label="After">›</button></span>' : "") +
    '<label class="all-rows"><input type="checkbox" data-rows' + (S.memo.allRows ? " checked" : "") + "> Show all rows</label>" +
    // what each mark is: left to right is time; up and down means something only in a banded row
    '<div class="legend"><span><i class="lg-find fail"></i><i class="lg-find review"></i>a finding: failed, to review</span>' +
    '<span><i class="lg-wake"></i>the agent woke</span>' +
    '<span><i class="lg-dot failed"></i><i class="lg-dot review"></i><i class="lg-dot held"></i><i class="lg-dot unmeasured"></i>an output, in the band and colour of its result</span>' +
    '<span><i class="lg-ring"></i>a reply from a person</span><span class="muted">marks at one moment sit side by side</span></div>';
}

/** Show a period at a scale: the axis moves to it (animated), and the list under it follows. */
function scaleTo(sc, at, animate) {
  if (!SCALES.some((x) => x[0] === sc)) { return; }
  S.memo.scale = sc;
  const [lo, hi] = S.base ? [S.base.timeline.starts, S.base.timeline.reached] : [0, 0];
  let win;
  if (sc === "all") { S.memo.at = null; win = [lo, hi]; } else {
    const unit = UNIT[sc](), start = unit.floor(new Date(at === null || at === undefined ? (tl.win[0] + tl.win[1]) / 2 : at));
    S.memo.at = +start;
    win = [+start, +unit.offset(start, 1)];
  }
  S.going = { win, until: Date.now() + 600 };
  tl.setWindow(win[0], win[1], animate !== false);
  if (S.handle.onWindow) { S.handle.onWindow(); }
  renderScales();
  rememberWhere();
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
  if (S.handle.onSelect) { S.handle.onSelect(S.sel); }
  if (from !== "refresh") { renderInspector(); }
  rememberWhere();
}

async function renderInspector() {
  const ref = S.sel, box = $("inspector");
  if (!S.base) { return; }
  box.hidden = !ref; $("body").classList.toggle("inspecting", Boolean(ref));
  if (!ref) { box.innerHTML = ""; return; }
  box.innerHTML = '<p class="insp-empty">Reading…</p>';
  const html = await inspect(ctx, ref);
  if (S.sel === ref) { box.innerHTML = '<button type="button" class="insp-close" data-close aria-label="Close">×</button>' + html; box.scrollTop = 0; }
}

/* ---------- every listener, bound once ---------- */

document.addEventListener("click", (ev) => {
  if (ev.target.closest("[data-close]")) { ev.preventDefault(); select(null, "page"); return; }
  const stepper = ev.target.closest("#formats [data-step]");
  if (stepper) { ev.preventDefault(); const unit = UNIT[S.memo.scale](); scaleTo(S.memo.scale, +unit.offset(new Date(S.memo.at), Number(stepper.getAttribute("data-step")))); return; }
  const scale = ev.target.closest("[data-scale]");
  if (scale) { ev.preventDefault(); scaleTo(scale.getAttribute("data-scale"), S.memo.at); return; }
  if (ev.target.closest("[data-rows]")) { S.memo.allRows = ev.target.closest("[data-rows]").checked; tl.setData(derive(S.base.timeline, S.base.findings.items, S.memo.allRows), S.base.findings.findings); rememberWhere(); return; }
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
    const order = S.base.findings.findings.filter((x) => x.finding.kind !== "informational").sort((p, q) => KIND_ORDER[p.finding.kind] - KIND_ORDER[q.finding.kind] || p.number - q.number).map((x) => "finding:" + x.number);
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
