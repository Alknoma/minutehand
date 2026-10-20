/* The list under the time page's axis: what the axis shows, in rows, at whatever scale it is on. The axis (the
   timeline, with its rows for when the agent woke, its outputs in their results' colours, and replies) is the
   picture; this is the reading of it, and it follows the axis as it moves.

   - A day or less in view: a row for each wake (why the agent woke), each output (its kind, to whom, its first line,
     and the checks it failed, or "Passed") and each reply, in the order the record kept them. An output opens in the
     inspector with what it was, exactly, and what every check said of it.
   - More than a day in view: a row for each day, counting its outputs by result and its replies, and naming the kinds
     of failure first seen on it. A row zooms the axis to its day. */

import { esc, words, plural, hhmm, day } from "./util.js";
import { outputsOf, markHtml, kindWord, CHECK_WORD, RESULT_WORD } from "./outputs.js";
import { failureSort } from "./views.js";

const DAY = 86400000;
const WOKE = { start: "started its work", due: "woke when it had chosen to", reply: "woke for a reply", message: "woke for a message", event: "woke for an event" };

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

/* ----- a row for each day ----- */

function daysHtml(days, from, to) {
  const rows = [];
  for (let i = from; i <= to; i++) {
    const d = days[i], t = d.tally;
    rows.push('<button type="button" class="row day-row ' + (t.failed ? "failed" : t.review ? "review" : d.outputs.length ? "held" : "quiet") + '" data-day="' + d.start + '">' +
      '<span class="date">' + esc(d3.utcFormat("%a %-d %b")(new Date(d.start))) + "</span>" +
      '<span class="tallies">' + count(t.failed, "failed", "✗", "failed output") + count(t.review, "review", "?", "output to review") + count(t.held, "held", "✓", "passed output") +
      (t.unmeasured ? count(t.unmeasured, "unmeasured", "", "output not measured") : "") + count(d.heard.length, "heard", "↩", "reply from a person") + "</span>" +
      '<span class="what">' + (d.newKinds.length ? '<span class="new-tag">new</span> ' + esc(d.newKinds.join(", ")) : d.outputs.length || d.heard.length ? "" : '<span class="muted">nothing happened</span>') + "</span></button>");
  }
  return '<div class="rows days"><div class="row head"><span>Day</span><span>Outputs and replies</span><span>New kinds of failure</span></div>' + rows.join("") + "</div>";
}

/** One row per wake, output and reply, in time order. */
function rowsHtml(ctx, d, whole, a, b) {
  const rows = [], inside = (t) => t >= a && t < b;
  d.wakes.filter((w) => inside(w.sim)).forEach((w) => {
    const why = /\((.+)\)$/.exec(w.label);
    rows.push({ sim: w.sim, seq: -1, html: '<button type="button" class="row wake" data-sel="' + esc(w.ref) + '"><time>' + hhmm(w.sim) + '</time><span class="what">The agent ' + esc(why ? WOKE[why[1]] || "woke: " + words(why[1]) : "woke") + "</span></button>" });
  });
  d.outputs.filter((o) => inside(o.sim)).forEach((o) => {
    const text = whole.get(o.ref) || o.label, parts = text.split("\n\n"), first = parts[0];
    const flagged = o.item.checks.filter((c) => c.outcome === "failed" || c.outcome === "review").map((c) => CHECK_WORD[c.check] || words(c.check));
    const to = o.lane && o.lane.kind === "person" ? o.lane.label : o.item.provider;
    rows.push({ sim: o.sim, seq: o.item.seq, html: '<button type="button" class="row output ' + o.result + '" data-sel="' + esc(o.ref) + '"><time>' + hhmm(o.sim) + "</time>" +
      '<span class="kind">' + markHtml(o.item.kind, o.result) + esc(kindWord(o.item.kind)) + '</span><span class="to">' + esc(to) + '</span><span class="text">' + esc(first) + "</span>" +
      '<span class="result ' + o.result + '">' + esc(flagged.length ? flagged.join(", ") : RESULT_WORD[o.result]) + "</span></button>" });
  });
  d.heard.filter((h) => inside(h.sim)).forEach((h) => {
    const text = (whole.get(h.ref) || h.label).split("\n\n").pop();
    rows.push({ sim: h.sim, seq: Number(h.ref.split(":")[1]) || 0, html: '<button type="button" class="row heard" data-sel="' + esc(h.ref) + '"><time>' + hhmm(h.sim) + '</time><span class="kind"><i class="heard"></i>Reply</span><span class="to">' + esc(h.who) + '</span><span class="text">' + esc(text) + "</span></button>" });
  });
  // within a minute, in the order the record kept them: a reply before what the agent did about it
  rows.sort((p, q) => p.sim - q.sim || p.seq - q.seq);
  return rows;
}

const DAY_MS = 86400000;

export async function timeList(host, ctx) {
  const days = runDays(ctx);
  const whole = new Map();
  (await ctx.cache.read("people")).people.forEach((p) => p.conversation.forEach((l) => { if (l.seq !== null) { whole.set("ev:" + l.seq, l.text); } }));
  const first = days.length ? days[0].start : 0;
  const mark = (ref) => {
    host.querySelectorAll("[data-sel].on").forEach((el) => el.classList.remove("on"));
    if (ref) { host.querySelectorAll('[data-sel="' + CSS.escape(ref) + '"]').forEach((el) => el.classList.add("on")); }
  };
  const draw = () => {
    const [a, b] = ctx.window();
    const from = Math.max(0, Math.floor((a - first) / DAY_MS)), to = Math.min(days.length - 1, Math.floor((b - 1 - first) / DAY_MS));
    if (!(ctx.base.findings.items || []).length && ctx.base.findings.finished) {
      host.innerHTML = '<p class="empty">The agent wrote nothing a check reads in this run: no message, email, ticket, event, document or record.</p>';
      return;
    }
    let html;
    if (b - a <= 1.5 * DAY_MS) {
      const rows = [];
      for (let i = from; i <= to; i++) { rows.push(...rowsHtml(ctx, days[i], whole, a, b)); }
      rows.sort((p, q) => p.sim - q.sim || p.seq - q.seq);
      html = rows.length ? '<div class="rows"><div class="row head"><span>Time</span><span>What</span><span>Who</span><span>Says</span><span>Result</span></div>' + rows.map((r) => r.html).join("") + "</div>" : '<p class="empty">Nothing happened in this stretch.</p>';
    } else {
      html = to >= from ? daysHtml(days, from, to) : '<p class="empty">No day of the run is in view.</p>';
    }
    const again = ctx.base.findings.items_reassessed ? '<p class="muted note">This run was checked before each output\'s result was kept: they are read again from its record.</p>' : "";
    const top = host.firstChild ? host.firstChild.scrollTop : 0;
    host.innerHTML = '<div class="view-body time-list">' + again + html + "</div>";
    host.firstChild.scrollTop = top;
    mark(ctx.sel);
  };
  // bound to what this view's draw made: the host is this draw's own page, which a later draw replaces
  host.addEventListener("click", (ev) => { const go = ev.target.closest(".day-row[data-day]"); if (go) { ctx.scaleTo("day", Number(go.getAttribute("data-day"))); } });
  draw();
  return { onSelect: mark, onWindow: draw };
}
