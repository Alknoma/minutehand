/* The calendar the run opens on, and the day it opens into: two pages, one at a time.

   - The calendar is the run seen from above: a cell a day, each counting the agent's outputs that day by their result
     (failed, to review, passed). It lists nothing: however many outputs and failures a run holds, a cell says how
     many, and the day says which. The first day a kind of failure appears is marked new; a failure seen before is
     only counted.
   - The day is one day close up: its twenty-four hours drawn (when the agent woke, each output as its kind's shape in
     its result's colour, each word from a person), and below, in time order, each wake, each output and each reply,
     one row each. An output opens in the inspector with what it was, exactly, and what every check said of it. */

import { esc, words, plural, hhmm, day } from "./util.js";
import { outputsOf, markHtml, kindWord, CHECK_WORD, RESULT_WORD } from "./outputs.js";
import { failureSort } from "./views.js";

const DAY = 86400000;
const WOKE = { start: "started its work", due: "woke when it had chosen to", reply: "woke for a reply", message: "woke for a message", event: "woke for an event" };

function longDay(t) { return new Date(t).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }); }
function count(n, cls, sign, what) { return n ? '<span class="tally ' + cls + '" title="' + esc(plural(n, what)) + '">' + sign + n + "</span>" : ""; }

/** The run's days, each with its outputs, wakes and replies, and the kinds of failure first seen on it. */
function runDays(ctx) {
  const t = ctx.base.timeline, m = t.marks;
  const first = Math.floor(t.starts / DAY), last = Math.max(first, Math.floor((t.reached - 1) / DAY));
  const days = [];
  for (let d = first; d <= last; d++) { days.push({ start: d * DAY, outputs: [], wakes: [], heard: [], newKinds: [] }); }
  const of = (sim) => days[Math.min(days.length - 1, Math.max(0, Math.floor(sim / DAY) - first))];
  outputsOf(ctx).forEach((o) => of(o.sim).outputs.push(o));
  for (let i = 0; i < m.sim.length; i++) {
    if (m.kind[i] === "wake") { of(m.sim[i]).wakes.push({ ref: m.ref[i], sim: m.sim[i], label: m.label[i] }); }
    if (m.kind[i] === "said") { of(m.sim[i]).heard.push({ ref: m.ref[i], sim: m.sim[i], who: t.lanes[m.lane[i]].label, label: m.label[i] }); }
  }
  const seen = new Set();
  ctx.base.findings.findings.filter((x) => x.finding.kind === "fail" && x.finding.at).sort((p, q) => p.finding.at.localeCompare(q.finding.at)).forEach((x) => {
    const k = failureSort(x);
    if (!seen.has(k)) { seen.add(k); of(Date.parse(x.finding.at)).newKinds.push(k); }
  });
  days.forEach((d) => {
    d.tally = { failed: 0, review: 0, held: 0, unmeasured: 0 };
    d.outputs.forEach((o) => { d.tally[o.result] += 1; });
  });
  return days;
}

/* ----- the calendar ----- */

function cellHtml(d, i) {
  const t = d.tally, worst = t.failed ? "failed" : t.review ? "review" : d.outputs.length ? "held" : d.heard.length ? "heard" : "quiet";
  const tip = day(d.start) + ": " + (d.outputs.length ? plural(d.outputs.length, "output") + (t.failed ? ", " + t.failed + " failed" : "") + (t.review ? ", " + t.review + " to review" : "") : "no output") + (d.newKinds.length ? "; new: " + d.newKinds.join(", ") : "");
  return '<button type="button" class="cal-cell ' + worst + (d.newKinds.length ? " new" : "") + '" data-day="' + i + '" title="' + esc(tip) + '">' +
    '<span class="date">' + new Date(d.start).getUTCDate() + "</span>" + (d.newKinds.length ? '<span class="new-tag">new</span>' : "") +
    '<span class="tallies">' + count(t.failed, "failed", "✗", "failed output") + count(t.review, "review", "?", "output to review") + count(t.held, "held", "✓", "passed output") +
    (d.outputs.length && !t.failed && !t.review && !t.held ? '<span class="tally unmeasured">' + d.outputs.length + "</span>" : "") + count(d.heard.length, "heard", "↩", "reply from a person") + "</span></button>";
}

function calendarHtml(days, again) {
  const months = [];
  days.forEach((d, i) => {
    const at = new Date(d.start), key = at.getUTCFullYear() * 12 + at.getUTCMonth();
    if (!months.length || months[months.length - 1].key !== key) { months.push({ key, at, cells: [] }); }
    months[months.length - 1].cells.push(i);
  });
  return '<div class="cal-legend"><span><span class="tally failed">✗</span>failed</span><span><span class="tally review">?</span>to review</span><span><span class="tally held">✓</span>passed</span><span><span class="tally heard">↩</span>replies from people</span>' +
    '<span><span class="new-tag">new</span>the first day of a kind of failure</span><span class="muted">counts are the agent\'s outputs that day; a day opens on a click</span>' +
    (again ? '<span class="muted">this run was checked before each output\'s result was kept: they are read again from its record</span>' : "") + "</div>" +
    months.map((mo) => {
      const lead = (new Date(days[mo.cells[0]].start).getUTCDay() + 6) % 7;
      return '<section class="cal-month"><h2>' + esc(mo.at.toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" })) + '</h2><div class="cal-grid">' +
        ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((w) => '<span class="dow">' + w + "</span>").join("") +
        '<span class="pad"></span>'.repeat(lead) + mo.cells.map((i) => cellHtml(days[i], i)).join("") + "</div></section>";
    }).join("");
}

/* ----- the day ----- */

function stripHtml(d) {
  const pos = (t) => (100 * (t - d.start) / DAY).toFixed(3) + "%";
  const rows = [
    ["Woke", d.wakes.map((w) => '<i class="tick" style="left:' + pos(w.sim) + '" data-sel="' + esc(w.ref) + '" title="' + esc(hhmm(w.sim) + " the agent woke") + '"></i>')],
    ["Outputs", d.outputs.map((o) => '<span class="at" style="left:' + pos(o.sim) + '">' + markHtml(o.item.kind, o.result, ' data-sel="' + esc(o.ref) + '" title="' + esc(hhmm(o.sim) + " " + kindWord(o.item.kind) + ": " + RESULT_WORD[o.result]) + '"') + "</span>")],
    ["Replies", d.heard.map((h) => '<i class="heard" style="left:' + pos(h.sim) + '" data-sel="' + esc(h.ref) + '" title="' + esc(hhmm(h.sim) + " " + h.who) + '"></i>')]
  ];
  const kinds = Array.from(new Set(d.outputs.map((o) => o.item.kind)));
  return '<div class="strip"><div class="strip-hours"><span></span><div>' + [0, 6, 12, 18, 24].map((h) => '<span style="left:' + (100 * h / 24) + '%">' + (h < 10 ? "0" : "") + h + ":00</span>").join("") + "</div></div>" +
    rows.map(([name, marks]) => '<div class="strip-row"><span class="strip-name">' + name + '</span><div class="strip-track">' + marks.join("") + "</div></div>").join("") +
    (kinds.length ? '<div class="strip-key">' + kinds.map((k) => '<span>' + markHtml(k, "plain") + esc(kindWord(k)) + "</span>").join("") + "</div>" : "") + "</div>";
}

/** One row per wake, output and reply, in time order. */
function rowsHtml(ctx, d, whole) {
  const rows = [];
  d.wakes.forEach((w) => {
    const why = /\((.+)\)$/.exec(w.label);
    rows.push({ sim: w.sim, seq: -1, html: '<button type="button" class="row wake" data-sel="' + esc(w.ref) + '"><time>' + hhmm(w.sim) + '</time><span class="what">The agent ' + esc(why ? WOKE[why[1]] || "woke: " + words(why[1]) : "woke") + "</span></button>" });
  });
  d.outputs.forEach((o) => {
    const text = whole.get(o.ref) || o.label, parts = text.split("\n\n"), first = parts[0];
    const flagged = o.item.checks.filter((c) => c.outcome === "failed" || c.outcome === "review").map((c) => CHECK_WORD[c.check] || words(c.check));
    const to = o.lane && o.lane.kind === "person" ? o.lane.label : o.item.provider;
    rows.push({ sim: o.sim, seq: o.item.seq, html: '<button type="button" class="row output ' + o.result + '" data-sel="' + esc(o.ref) + '"><time>' + hhmm(o.sim) + "</time>" +
      '<span class="kind">' + markHtml(o.item.kind, o.result) + esc(kindWord(o.item.kind)) + '</span><span class="to">' + esc(to) + '</span><span class="text">' + esc(first) + "</span>" +
      '<span class="result ' + o.result + '">' + esc(flagged.length ? flagged.join(", ") : RESULT_WORD[o.result]) + "</span></button>" });
  });
  d.heard.forEach((h) => {
    const text = (whole.get(h.ref) || h.label).split("\n\n").pop();
    rows.push({ sim: h.sim, seq: Number(h.ref.split(":")[1]) || 0, html: '<button type="button" class="row heard" data-sel="' + esc(h.ref) + '"><time>' + hhmm(h.sim) + '</time><span class="kind"><i class="heard"></i>Reply</span><span class="to">' + esc(h.who) + '</span><span class="text">' + esc(text) + "</span></button>" });
  });
  // within a minute, in the order the record kept them: a reply before what the agent did about it
  rows.sort((a, b) => a.sim - b.sim || a.seq - b.seq);
  return rows.length ? '<div class="rows"><div class="row head"><span>Time</span><span>What</span><span>Who</span><span>Says</span><span>Result</span></div>' + rows.map((r) => r.html).join("") + "</div>" : '<p class="empty">Nothing happened this day.</p>';
}

function dayHtml(ctx, days, i, whole) {
  const d = days[i], t = d.tally;
  const said = [plural(d.outputs.length, "output"), t.failed ? t.failed + " failed" : "", t.review ? t.review + " to review" : "", t.held ? t.held + " passed" : "", plural(d.heard.length, "reply", "replies") + " received"].filter(Boolean).join(" · ");
  return '<header class="day-top"><button type="button" class="back" data-calendar>← Calendar</button>' +
    '<div class="day-title"><h2>' + esc(longDay(d.start)) + "</h2><p>" + esc(said) + (d.newKinds.length ? ' · <span class="new-tag">new</span> ' + esc(d.newKinds.join(", ")) : "") + "</p></div>" +
    '<div class="day-nav"><button type="button" data-day="' + (i - 1) + '"' + (i ? "" : " disabled") + ' aria-label="The day before">‹</button><button type="button" data-day="' + (i + 1) + '"' + (i < days.length - 1 ? "" : " disabled") + ' aria-label="The day after">›</button></div></header>' +
    stripHtml(d) + rowsHtml(ctx, d, whole);
}

export async function calendarView(host, ctx) {
  const days = runDays(ctx);
  if (!(ctx.base.findings.items || []).length && ctx.base.findings.finished) {
    host.innerHTML = '<p class="empty">The agent wrote nothing a check reads in this run: no message, email, ticket, event, document or record.</p>';
    return {};
  }
  const whole = new Map();
  (await ctx.cache.read("people")).people.forEach((p) => p.conversation.forEach((l) => { if (l.seq !== null) { whole.set("ev:" + l.seq, l.text); } }));
  const dayOf = new Map();
  days.forEach((d, i) => { d.outputs.forEach((o) => dayOf.set(o.ref, i)); d.heard.forEach((h) => dayOf.set(h.ref, i)); d.wakes.forEach((w) => dayOf.set(w.ref, i)); });
  const page = (i) => {
    ctx.memo.day = i;
    if (i === null || i === undefined) { host.innerHTML = '<div class="view-body cal-page">' + calendarHtml(days, ctx.base.findings.items_reassessed) + "</div>"; } else { host.innerHTML = '<div class="view-body day-page">' + dayHtml(ctx, days, i, whole) + "</div>"; }
    mark(ctx.sel);
    ctx.remember();
  };
  const mark = (ref) => {
    host.querySelectorAll("[data-sel].on").forEach((el) => el.classList.remove("on"));
    if (ref) { host.querySelectorAll('[data-sel="' + CSS.escape(ref) + '"]').forEach((el) => el.classList.add("on")); }
  };
  // bound to what this view's draw made: the host is this draw's own page, which a later draw replaces
  host.addEventListener("click", (ev) => {
    const go = ev.target.closest("[data-day]");
    if (go && !go.disabled) { page(Number(go.getAttribute("data-day"))); return; }
    if (ev.target.closest("[data-calendar]")) { page(null); }
  });
  const opened = ctx.memo.day;
  page(opened !== undefined && opened !== null && opened < days.length ? opened : dayOf.has(ctx.sel) ? dayOf.get(ctx.sel) : null);
  return { onSelect: (ref) => { if (ref && dayOf.has(ref) && ctx.memo.day !== dayOf.get(ref)) { page(dayOf.get(ref)); } mark(ref); } };
}

