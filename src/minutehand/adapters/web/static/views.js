/* The views under the timeline: one measure each, said large, with what it is made of beneath (a chart where the
   shape matters, a table to find the row). Each view reads only the paths it needs, when it is first opened. */

import { esc, when, span, seconds, words, title, plural, count, clip, median } from "./util.js";
import { VList } from "./vlist.js";

const VERDICT_WORD = { passed: "Passed", failed: "Failed", unfinished: "Not finished", tool_failed: "Not scored", not_judged: "Not judged", environment_failed: "Environment failed" };
const KIND_ORDER = { fail: 0, review: 1, informational: 2 };
const ANSWER_WORD = { provider: "fake", declared: "declared", stored: "stored", real_host: "real host", replayed: "REPLAYED", emulator: "emulator", model: "model", tunnelled: "tunnelled", as_person: "as a person", refused: "refused" };

function headHtml(measure, caption, cls, tools) {
  return '<div class="view-head"><span class="measure ' + (cls || "") + '">' + esc(measure) + '</span><span class="measure-caption">' + caption + "</span>" +
    (tools ? '<span class="tools">' + tools + "</span>" : "") + "</div>";
}
function chip(kind, text) { return '<span class="chip ' + esc(kind) + '">' + esc(text) + "</span>"; }
function plotInto(host, spec) {
  try {
    const el = Plot.plot(Object.assign({ style: { background: "transparent", color: "var(--ink-2)", fontSize: "11px" }, marginLeft: 48 }, spec));
    host.appendChild(el);
  } catch (e) { host.textContent = "Could not draw the chart: " + e.message; }
}
function ms(t) { return typeof t === "number" ? t : Date.parse(t); }
function laneCount(ctx, kind) { return ctx.base.timeline.lanes.filter((l) => l.kind === kind).reduce((a, l) => a + l.marks, 0); }
function sortedFindings(ctx) {
  return ctx.base.findings.findings.slice().sort((a, b) => KIND_ORDER[a.finding.kind] - KIND_ORDER[b.finding.kind] || a.number - b.number);
}
export function findingTitle(x) { return x.pattern ? x.pattern.title : title(x.finding.check); }

/** A virtualised table bound to the selection: picking a row selects its ref, and the selection marks its row. */
function table(host, ctx, columns, rows, refOf, rowHeight) {
  const list = new VList(host, { columns, rowHeight: rowHeight || 30, key: refOf, onPick: (r) => ctx.select(refOf(r)) });
  list.setRows(rows);
  if (ctx.sel) { list.select(ctx.sel, true); }
  return { onSelect: (ref) => list.select(ref, true) };
}

export const VIEWS = [
  {
    id: "findings", group: "Outcome", label: "Findings",
    count: (ctx) => { const f = ctx.base.findings.findings; const fails = f.filter((x) => x.finding.kind === "fail").length; return { n: fails || f.length, cls: fails ? "fail" : "" }; },
    render(host, ctx) {
      const fs = ctx.base.findings;
      if (!fs.finished) { host.innerHTML = headHtml("—", "The checks run when the run finishes.") ; return {}; }
      const f = sortedFindings(ctx), c = { fail: 0, review: 0, informational: 0 };
      f.forEach((x) => { c[x.finding.kind] += 1; });
      host.innerHTML = headHtml(String(c.fail), "failed · " + c.review + " to review · " + plural(c.informational, "note") + (fs.blocked.length ? " · " + plural(fs.blocked.length, "check") + " could not run" : ""), c.fail ? "fail" : "pass") + '<div class="view-body" data-role="t"></div>';
      return table(host.querySelector('[data-role="t"]'), ctx, [
        { label: "Kind", width: "84px", html: (x) => chip(x.finding.kind, x.finding.kind === "informational" ? "note" : x.finding.kind), sort: (x) => KIND_ORDER[x.finding.kind] },
        { label: "Finding", width: "minmax(160px, 1fr)", html: (x) => esc(findingTitle(x)) },
        { label: "What it found", width: "minmax(200px, 2.4fr)", html: (x) => esc(x.finding.message) },
        { label: "When", width: "130px", html: (x) => esc(x.finding.at ? when(x.finding.at) : "—"), sort: (x) => x.finding.at || "" },
        { label: "Evidence", width: "74px", align: "r", html: (x) => String(x.finding.evidence.length), sort: (x) => x.finding.evidence.length }
      ], f, (x) => "finding:" + x.number, 34);
    }
  },
  {
    id: "assessments", group: "Outcome", label: "Assessments",
    count: (ctx) => { const a = ctx.loaded.assessments; if (!a) { return { n: "…" }; } const failed = a.rules.filter((r) => r.status === "failed").length; return { n: failed ? failed : a.rules.length, cls: failed ? "fail" : "" }; },
    async render(host, ctx) {
      const a = await ctx.cache.read("assessments");
      const rules = a.rules, held = rules.filter((r) => r.status === "passed").length, failed = rules.filter((r) => r.status === "failed").length;
      if (!rules.length) { host.innerHTML = headHtml("0", "rules: neither the agent file nor the scenario declares an <code>assess</code> rule") + '<p class="empty">The verdict then reads only the scenario\'s expectations and Minutehand\'s integrity checks (docs/assessments.md).</p>'; return {}; }
      const bound = (r) => [r.exactly !== null ? "exactly " + r.exactly : "", r.at_least !== null ? "at least " + r.at_least : "", r.at_most !== null ? "at most " + r.at_most : "", r.gap_at_least ? "gap ≥ " + span(seconds(r.gap_at_least)) : ""].filter(Boolean).join(", ");
      const counted = (r) => Object.keys(r.count).find((k) => r.count[k] !== null && k !== "since" && k !== "until") || "";
      host.innerHTML = headHtml(held + " of " + rules.length, "rules held" + (failed ? " · " + failed + " failed" : ""), failed ? "fail" : held === rules.length ? "pass" : "") +
        '<div class="view-body"><table class="grid"><thead><tr><th>Status</th><th>Rule</th><th>For each</th><th>Counts</th><th class="r">Read</th><th class="r">Unread</th><th class="r">Findings</th></tr></thead><tbody>' +
        rules.map((o) => '<tr class="' + (o.findings.length ? "clickable" : "") + '"' + (o.findings.length ? ' data-sel="finding:' + o.findings[0] + '"' : "") + "><td>" + chip(o.status, words(o.status)) + "</td><td><b>" + esc(o.rule.id) + "</b>" +
          (o.rule.message ? '<div class="muted">' + esc(clip(o.rule.message, 120)) + "</div>" : "") +
          (o.findings.length > 1 ? '<div class="muted">' + o.findings.slice(0, 12).map((n) => '<button type="button" class="link" data-sel="finding:' + n + '">#' + n + "</button>").join(" ") + (o.findings.length > 12 ? " …" : "") + "</div>" : "") +
          "</td><td>" + esc(o.rule.each) + "</td><td>" + esc(words(counted(o.rule)) + " " + bound(o.rule) + (o.rule.count.since ? " from " + o.rule.count.since : "") + (o.rule.count.until ? " to " + o.rule.count.until : "")) +
          '</td><td class="r">' + o.read + '</td><td class="r">' + o.unread + '</td><td class="r">' + o.findings.length + "</td></tr>").join("") + "</tbody></table></div>";
      return {};
    }
  },
  {
    id: "conversations", group: "People", label: "Conversations",
    count: (ctx) => ({ n: ctx.loaded.people ? count(ctx.loaded.people.people.reduce((a, x) => a + x.conversation.length, 0)) : "…" }),
    async render(host, ctx) {
      const p = await ctx.cache.read("people");
      const people = p.people.filter((x) => x.conversation.length);
      const total = people.reduce((a, x) => a + x.conversation.length, 0);
      host.innerHTML = headHtml(count(total), "messages with " + plural(people.length, "person", "people") + " · who wrote each: the agent, a script, a model, or the script's own words") +
        '<div class="view-split"><div class="side side-list" data-role="people"></div><div class="main" data-role="chat"></div></div>';
      if (!people.length) { host.querySelector('[data-role="chat"]').innerHTML = '<p class="empty">Nobody in the scenario was written to.</p>'; return {}; }
      let current = ctx.memo.person && people.find((x) => x.key === ctx.memo.person) ? ctx.memo.person : people[0].key;
      const side = host.querySelector('[data-role="people"]'), chatHost = host.querySelector('[data-role="chat"]');
      const WRITTEN = { script: "script, worded by a model", verbatim: "the script's words", conversing: "a model", automatic: "automatic reply", by_hand: "by hand" };
      const refOf = (l) => (l.seq !== null && l.from_agent ? "ev:" + l.seq : l.reply !== null ? "reply:" + l.reply : "ev:" + l.seq);
      const list = new VList(chatHost, {
        rowHeight: 66, key: refOf, onPick: (l) => ctx.select(refOf(l)),
        rowHtml: (l, on, i, y, rh) => {
          const who = l.from_agent ? "agent" : (people.find((x) => x.key === current) || {}).name;
          const how = l.from_agent ? (l.change !== "sent" ? l.change : "") : WRITTEN[l.writing] + (l.written_by ? " (" + l.written_by + ")" : "");
          return '<div class="chat-row ' + (l.from_agent ? "agent" : "person") + (on ? " on" : "") + '" data-row="' + i + '" style="top:' + y + "px;height:" + rh + 'px"><div class="bubble"><div class="who"><b>' + esc(who) + "</b><span>" + esc(when(l.at)) + "</span>" +
            (how ? '<span class="' + (l.change === "edited" || l.change === "deleted" ? "edited" : "") + '">' + esc(how) + "</span>" : "") + '</div><div class="text">' + esc(l.text) + "</div></div></div>";
        }
      });
      const show = () => {
        side.innerHTML = people.map((x) => '<button type="button" data-person="' + esc(x.key) + '" aria-pressed="' + (x.key === current) + '"><span>' + esc(x.name) + '</span><span class="n">' + x.conversation.length + '</span><span class="s">' + esc(x.replies.length + " replies · " + x.model_calls.length + " model calls") + "</span></button>").join("");
        list.setRows(people.find((x) => x.key === current).conversation);
        if (ctx.sel) { list.select(ctx.sel, true); }
      };
      side.addEventListener("click", (ev) => { const b = ev.target.closest("[data-person]"); if (b) { current = b.getAttribute("data-person"); ctx.memo.person = current; show(); } });
      show();
      return { onSelect: (ref) => list.select(ref, true) };
    }
  },
  {
    id: "replies", group: "People", label: "Replies",
    count: (ctx) => { const p = ctx.loaded.people; if (!p) { return { n: "…" }; } return { n: p.people.reduce((a, x) => a + x.replies.length, 0) }; },
    async render(host, ctx) {
      const p = await ctx.cache.read("people");
      const rows = [];
      p.people.forEach((x) => x.replies.forEach((r) => {
        const d = r.reply.drawn, asked = d ? ms(d.asked_at) : null;
        rows.push({ person: x.name, r, asked, took: asked !== null ? (ms(r.reply.at) - asked) / 1000 : null, tokens: x.model_calls.filter((m) => m.call.sim_time === r.reply.at).reduce((a, m) => a + (m.call.input_tokens || 0) + (m.call.output_tokens || 0), 0) });
      }));
      const tooks = rows.map((x) => x.took).filter((t) => t !== null);
      const med = median(tooks);
      host.innerHTML = headHtml(med === null ? "—" : span(med), "the median time a person took to answer · " + plural(rows.length, "reply", "replies") + (tooks.length ? " · slowest " + span(Math.max(...tooks)) : "")) +
        '<div class="chart" data-role="chart"></div><div class="view-body" data-role="t" style="display:flex;flex-direction:column;min-height:200px"></div>';
      if (!rows.length) { host.querySelector('[data-role="t"]').innerHTML = '<p class="empty">Nobody answered.</p>'; return {}; }
      const chartHost = host.querySelector('[data-role="chart"]');
      if (tooks.length) {
        plotInto(chartHost, {
          width: Math.max(320, chartHost.clientWidth - 32), height: 150, x: { type: "utc", label: null }, y: { label: "hours to answer", grid: true },
          marks: [Plot.dot(rows.filter((x) => x.took !== null), { x: (x) => new Date(x.asked), y: (x) => x.took / 3600, r: 3, fill: "var(--mark)", title: (x) => x.person + ": " + span(x.took) }), Plot.ruleY([0])]
        });
      }
      return table(host.querySelector('[data-role="t"]'), ctx, [
        { label: "Person", width: "130px", html: (x) => esc(x.person) },
        { label: "Asked", width: "130px", html: (x) => esc(x.asked !== null ? when(x.asked) : "—"), sort: (x) => x.asked || 0 },
        { label: "Answered", width: "130px", html: (x) => esc(when(x.r.reply.at)), sort: (x) => x.r.reply.at },
        { label: "Took", width: "90px", align: "r", html: (x) => esc(span(x.took)), sort: (x) => x.took || 0 },
        { label: "Moment drawn from", width: "140px", html: (x) => esc(x.r.reply.drawn ? words(x.r.reply.drawn.source) : "by hand") },
        { label: "Words by", width: "120px", html: (x) => esc(words(x.r.reply.writing)) },
        { label: "Model tokens", width: "100px", align: "r", html: (x) => (x.tokens ? x.tokens.toLocaleString("en") : "—"), sort: (x) => x.tokens },
        { label: "Said", width: "minmax(160px, 1fr)", html: (x) => esc(x.r.reply.text) }
      ], rows, (x) => "reply:" + x.r.index);
    }
  },
  {
    id: "response", group: "People", label: "Response times",
    count: (ctx) => { const s = ctx.base.scorecard; return { n: s && s.slowest_reaction ? span(seconds(s.slowest_reaction)) : "—" }; },
    async render(host, ctx) {
      const o = (await ctx.cache.read("obligations")).obligations.filter((w) => w.obligation.settled_at && w.obligation.person);
      const rows = o.map((w) => ({ w, back: w.came_back_at ? (ms(w.came_back_at) - ms(w.obligation.settled_at)) / 1000 : null }));
      const known = rows.filter((x) => x.back !== null).map((x) => x.back), never = rows.filter((x) => x.back === null).length;
      const card = ctx.base.scorecard;
      host.innerHTML = headHtml(card && card.slowest_reaction ? span(seconds(card.slowest_reaction)) : known.length ? span(Math.max(...known)) : "—",
        "the longest the agent took to come back once someone answered · " + plural(rows.length, "settled wait") + (never ? " · " + never + " it never came back to" : "") + (known.length ? " · median " + span(median(known)) : ""), never ? "fail" : "") +
        '<div class="chart" data-role="chart"></div><div class="view-body" data-role="t" style="display:flex;flex-direction:column;min-height:200px"></div>';
      if (!rows.length) { host.querySelector('[data-role="t"]').innerHTML = '<p class="empty">No wait was settled, so no comeback can be timed.</p>'; return {}; }
      const chartHost = host.querySelector('[data-role="chart"]');
      if (known.length) {
        plotInto(chartHost, {
          width: Math.max(320, chartHost.clientWidth - 32), height: 140, x: { type: "utc", label: null }, y: { label: "hours to come back", grid: true },
          marks: [Plot.ruleX(rows.filter((x) => x.back !== null), { x: (x) => new Date(x.w.obligation.settled_at), y1: 0, y2: (x) => x.back / 3600, stroke: "var(--mark)", strokeWidth: 2 }), Plot.ruleY([0])]
        });
      }
      return table(host.querySelector('[data-role="t"]'), ctx, [
        { label: "Person", width: "130px", html: (x) => esc(ctx.personKey(x.w.obligation.person)) },
        { label: "Asked", width: "130px", html: (x) => esc(when(x.w.obligation.opened_at)), sort: (x) => x.w.obligation.opened_at },
        { label: "Answered", width: "130px", html: (x) => esc(when(x.w.obligation.settled_at)), sort: (x) => x.w.obligation.settled_at },
        { label: "Agent came back", width: "140px", html: (x) => (x.w.came_back_at ? esc(when(x.w.came_back_at)) : '<span class="bad">never</span>') },
        { label: "Took", width: "100px", align: "r", html: (x) => (x.back === null ? '<span class="bad">—</span>' : esc(span(x.back))), sort: (x) => (x.back === null ? Infinity : x.back) }
      ], rows, (x) => "wait:" + x.w.obligation.key);
    }
  },
  {
    id: "followups", group: "People", label: "Follow-ups",
    count: (ctx) => ({ n: ctx.base.scorecard ? ctx.base.scorecard.follow_ups_made : "—" }),
    async render(host, ctx) {
      const waits = (await ctx.cache.read("obligations")).obligations.filter((w) => w.obligation.person);
      const card = ctx.base.scorecard;
      const total = card ? card.follow_ups_made : waits.reduce((a, w) => a + w.obligation.agent_touches.length, 0);
      const burden = card ? card.burden : [];
      host.innerHTML = headHtml(String(total), "follow-ups the agent made on waits still open · " + plural(waits.length, "wait") + " · " + waits.filter((w) => !w.obligation.settled_at).length + " still open at the end") +
        '<div class="chart" data-role="chart"></div>' +
        (burden.length ? '<div class="chart"><table class="grid" style="width:auto"><thead><tr><th>Person</th><th class="r">Messages</th><th class="r">Of them follow-ups</th></tr></thead><tbody>' +
          burden.map((b) => "<tr><td>" + esc(ctx.personKey(b.person)) + '</td><td class="r">' + b.messages + '</td><td class="r">' + b.follow_ups + "</td></tr>").join("") + "</tbody></table></div>" : "") +
        '<div class="view-body" data-role="t" style="display:flex;flex-direction:column;min-height:200px"></div>';
      if (!waits.length) { host.querySelector('[data-role="t"]').innerHTML = '<p class="empty">The agent waited on nobody.</p>'; return {}; }
      const touches = waits.flatMap((w) => w.touched_at.map((t) => ({ at: new Date(t), person: ctx.personKey(w.obligation.person) })));
      if (touches.length) {
        const chartHost = host.querySelector('[data-role="chart"]'), times = touches.map((x) => +x.at), extent = Math.max(...times) - Math.min(...times);
        const unit = extent > 120 * 864e5 ? ["week", d3.utcMonday] : extent > 3 * 864e5 ? ["day", d3.utcDay] : ["hour", d3.utcHour];
        plotInto(chartHost, {
          width: Math.max(320, chartHost.clientWidth - 32), height: 130, x: { type: "utc", label: null }, y: { label: "follow-ups per " + unit[0], grid: true },
          marks: [Plot.rectY(touches, Plot.binX({ y: "count" }, { x: "at", fill: "var(--mark)", interval: unit[1] })), Plot.ruleY([0])]
        });
      }
      return table(host.querySelector('[data-role="t"]'), ctx, [
        { label: "Person", width: "130px", html: (w) => esc(ctx.personKey(w.obligation.person)) },
        { label: "Kind", width: "130px", html: (w) => esc(words(w.obligation.kind)) },
        { label: "Opened", width: "130px", html: (w) => esc(when(w.obligation.opened_at)), sort: (w) => w.obligation.opened_at },
        { label: "Expected by", width: "130px", html: (w) => esc(w.obligation.expected_by ? when(w.obligation.expected_by) : "—") },
        { label: "Settled", width: "130px", html: (w) => (w.obligation.settled_at ? esc(when(w.obligation.settled_at)) : chip("review", "open")), sort: (w) => w.obligation.settled_at || "~" },
        { label: "Follow-ups", width: "90px", align: "r", html: (w) => String(w.obligation.agent_touches.length), sort: (w) => w.obligation.agent_touches.length },
        { label: "Last", width: "minmax(120px, 1fr)", html: (w) => esc(w.touched_at.length ? when(w.touched_at[w.touched_at.length - 1]) : "—") }
      ], waits, (w) => "wait:" + w.obligation.key);
    }
  },
  {
    id: "cost", group: "Agent", label: "Model cost",
    count: (ctx) => { if (!ctx.loaded.traffic || !ctx.loaded.people) { return { n: laneCount(ctx, "agent_model") + laneCount(ctx, "people_model") }; } return { n: count(tokensOf(ctx.loaded.traffic, ctx.loaded.people).total) }; },
    async render(host, ctx) {
      const [traffic, people] = await Promise.all([ctx.cache.read("modelTraffic"), ctx.cache.read("people")]);
      const t = tokensOf(traffic, people);
      host.innerHTML = headHtml(count(t.total), "tokens · the agent " + count(t.agentIn) + " in, " + count(t.agentOut) + " out over " + plural(traffic.calls.length, "call") +
        " · people's words " + count(t.peopleIn) + " in, " + count(t.peopleOut) + " out over " + plural(t.people.length, "call") +
        (traffic.hosts.length ? " · " + traffic.hosts.map((h) => plural(h.calls, "call") + " to " + h.host + " relayed unopened, its tokens unknown").join(", ") : "") +
        ' · <span class="muted">counted in tokens: the run records no prices</span>') +
        '<div class="chart" data-role="chart"></div><div class="view-body" data-role="t" style="display:flex;flex-direction:column;min-height:200px"></div>';
      const rows = traffic.calls.map((c) => ({ ref: "mc:" + c.span_id, who: "agent", model: c.model || "not named", at: ctx.simOf("mc:" + c.span_id), tin: c.input_tokens || 0, tout: c.output_tokens || 0, took: (ms(c.ended) - ms(c.started)) / 1000, note: c.wrote.length ? "wrote " + plural(c.wrote.length, "message") : "" }))
        .concat(t.people.map((x) => ({ ref: "pc:" + x.line.index, who: x.person, model: x.line.call.model, at: ms(x.line.call.sim_time), tin: x.line.call.input_tokens || 0, tout: x.line.call.output_tokens || 0, took: null, note: x.line.call.replayed ? "replayed" : x.line.call.failure ? "failed" : x.line.call.wrote })));
      if (!rows.length) { host.querySelector('[data-role="t"]').innerHTML = '<p class="empty">The run holds no model call: the agent sent no telemetry of one, and no person\'s words were written by a model.</p>'; return {}; }
      const chartHost = host.querySelector('[data-role="chart"]');
      const extent = Math.max(...rows.map((r) => r.at)) - Math.min(...rows.map((r) => r.at));
      const unit = extent > 120 * 864e5 ? ["week", d3.utcMonday] : extent > 3 * 864e5 ? ["day", d3.utcDay] : extent > 6 * 36e5 ? ["hour", d3.utcHour] : ["minute", d3.utcMinute];
      plotInto(chartHost, {
        width: Math.max(320, chartHost.clientWidth - 32), height: 150, x: { type: "utc", label: null }, y: { label: rows.length < 40 ? "tokens per call" : "tokens per " + unit[0], grid: true, tickFormat: "s" },
        color: { domain: ["agent", "people"], range: ["var(--mark)", "var(--mark-soft)"], legend: true },
        marks: rows.length < 40
          ? [Plot.dot(rows, { x: (r) => new Date(r.at), y: (r) => r.tin + r.tout, fill: (r) => (r.who === "agent" ? "agent" : "people"), r: 4, title: (r) => r.who + ": " + (r.tin + r.tout) + " tokens" }), Plot.ruleY([0])]
          : [Plot.rectY(rows, Plot.binX({ y: "sum" }, { x: (r) => new Date(r.at), y: (r) => r.tin + r.tout, fill: (r) => (r.who === "agent" ? "agent" : "people"), interval: unit[1] })), Plot.ruleY([0])]
      });
      return table(host.querySelector('[data-role="t"]'), ctx, [
        { label: "When", width: "140px", html: (r) => esc(when(r.at)), sort: (r) => r.at },
        { label: "For", width: "110px", html: (r) => esc(r.who) },
        { label: "Model", width: "140px", html: (r) => esc(r.model) },
        { label: "In", width: "80px", align: "r", html: (r) => r.tin.toLocaleString("en"), sort: (r) => r.tin },
        { label: "Out", width: "80px", align: "r", html: (r) => r.tout.toLocaleString("en"), sort: (r) => r.tout },
        { label: "Took", width: "80px", align: "r", html: (r) => esc(r.took === null ? "—" : span(r.took)), sort: (r) => r.took || 0 },
        { label: "", width: "minmax(80px, 1fr)", html: (r) => esc(r.note) }
      ], rows, (r) => r.ref);
    }
  },
  {
    id: "memory", group: "Agent", label: "Memory",
    count: (ctx) => ({ n: count(laneCount(ctx, "memory")) }),
    async render(host, ctx) {
      const m = await ctx.cache.read("memory");
      host.innerHTML = headHtml(count(m.keys.length), "keys · " + count(m.changes.length) + " writes · " + count(m.reads) + " reads") +
        '<div class="view-split"><div class="side" data-role="keys" style="display:flex;flex-direction:column"></div><div class="main" data-role="history"></div></div>';
      if (!m.keys.length) { host.querySelector('[data-role="history"]').innerHTML = '<p class="empty">The agent kept nothing in its memory (minutehand.agent.store).</p>'; return {}; }
      const keyOf = (k) => k.collection + "/" + k.key;
      let current = ctx.memo.memoryKey || null;
      const historyHost = host.querySelector('[data-role="history"]');
      const history = new VList(historyHost, {
        columns: [
          { label: "When", width: "140px", html: (c) => esc(when(c.at)), sort: (c) => c.seq },
          { label: "Key", width: "minmax(120px, 1fr)", html: (c) => esc(c.collection + " / " + c.key) },
          { label: "Value", width: "minmax(160px, 2fr)", html: (c) => (c.value === null ? '<span class="bad">deleted</span>' : esc(clip(c.value, 160))) }
        ], rowHeight: 30, key: (c) => "ev:" + c.seq, onPick: (c) => ctx.select("ev:" + c.seq)
      });
      const keys = new VList(host.querySelector('[data-role="keys"]'), {
        rowHeight: 44, key: keyOf, onPick: (k) => { current = current === keyOf(k) ? null : keyOf(k); ctx.memo.memoryKey = current; keys.select(current, false); show(); },
        rowHtml: (k, on, i, y, rh) => '<div class="vt-row' + (keyOf(k) === current ? " on" : "") + '" data-row="' + i + '" style="top:' + y + "px;height:" + rh + 'px;display:block;padding:4px 12px"><div style="display:flex;justify-content:space-between;gap:6px"><span>' + esc(k.key) + '</span><span class="muted num">' + k.writes + "w " + k.reads + 'r</span></div><div class="muted" style="font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(k.collection + " · " + (k.writes === 0 ? "read, never written" : k.value === null ? "deleted" : clip(k.value, 60))) + "</div></div>"
      });
      keys.setRows(m.keys);
      const show = () => {
        history.setRows(current ? m.changes.filter((c) => c.collection + "/" + c.key === current) : m.changes);
        if (ctx.sel) { history.select(ctx.sel, true); }
      };
      show();
      return { onSelect: (ref) => history.select(ref, true) };
    }
  },
  {
    id: "stored", group: "Agent", label: "Stored items",
    count: (ctx) => ({ n: laneCount(ctx, "stored") }),
    async render(host, ctx) {
      const s = await ctx.cache.read("stored");
      const items = new Set(s.changes.map((c) => c.host + c.collection + c.id));
      host.innerHTML = headHtml(count(items.size), "items kept for hosts declared <code>store</code> · " + plural(s.changes.length, "write")) + '<div class="view-body" data-role="t" style="display:flex;flex-direction:column"></div>';
      if (!s.changes.length) { host.querySelector('[data-role="t"]').innerHTML = '<p class="empty">No host the agent file declares <code>store</code> was written to.</p>'; return {}; }
      return table(host.querySelector('[data-role="t"]'), ctx, [
        { label: "When", width: "140px", html: (c) => esc(when(c.at)), sort: (c) => c.seq },
        { label: "Host", width: "150px", html: (c) => esc(c.host) },
        { label: "Collection", width: "120px", html: (c) => esc(c.collection) },
        { label: "Id", width: "90px", html: (c) => esc(c.id) },
        { label: "Op", width: "70px", html: (c) => esc(c.operation) },
        { label: "Item", width: "minmax(160px, 1fr)", html: (c) => esc(c.item === null ? "deleted" : clip(c.item, 200)) }
      ], s.changes, (c) => "ev:" + c.seq);
    }
  },
  {
    id: "calls", group: "Wire", label: "Calls",
    count: (ctx) => { const n = laneCount(ctx, "provider") + laneCount(ctx, "host"); return { n: count(n) }; },
    async render(host, ctx) {
      const all = (await ctx.cache.read("callRows")).calls;
      const failedOf = (c) => c.answered === "refused" || c.status >= 400;
      const kinds = Array.from(new Set(all.map((c) => c.answered)));
      let only = ctx.memo.callsOnly || "all";
      const failed = all.filter(failedOf).length;
      host.innerHTML = headHtml(count(all.length), "HTTP calls · " + failed + " failed or refused · " + Array.from(new Set(all.map((c) => c.host))).length + " hosts",
        failed ? "" : "", '<span class="seg" data-role="only"><button type="button" data-only="all">All</button><button type="button" data-only="failed">Failed</button>' + kinds.map((k) => '<button type="button" data-only="' + esc(k) + '">' + esc(ANSWER_WORD[k] || k) + "</button>").join("") + "</span>") +
        '<div class="view-body" data-role="t" style="display:flex;flex-direction:column"></div>';
      const list = new VList(host.querySelector('[data-role="t"]'), {
        columns: [
          { label: "When", width: "130px", html: (c) => esc(when(c.at)), sort: (c) => c.index },
          { label: "Wake", width: "54px", align: "r", html: (c) => String(c.wake), sort: (c) => c.wake },
          { label: "Host", width: "150px", html: (c) => esc(c.provider ? c.provider : c.host) },
          { label: "Method", width: "64px", html: (c) => esc(c.method) },
          { label: "Path", width: "minmax(160px, 1fr)", html: (c) => esc(c.path) },
          { label: "Status", width: "60px", align: "r", html: (c) => (failedOf(c) ? '<span class="bad">' + c.status + "</span>" : String(c.status)), sort: (c) => c.status },
          { label: "Answered", width: "100px", html: (c) => esc(ANSWER_WORD[c.answered] || c.answered) },
          { label: "Bytes", width: "80px", align: "r", html: (c) => count(c.request_bytes + c.response_bytes), sort: (c) => c.request_bytes + c.response_bytes }
        ], rowHeight: 28, key: (c) => "call:" + c.index, onPick: (c) => ctx.select("call:" + c.index)
      });
      const seg = host.querySelector('[data-role="only"]');
      const show = () => {
        seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.getAttribute("data-only") === only)));
        const f = (ctx.filter || "").toLowerCase();
        list.setRows(all.filter((c) => (only === "all" || (only === "failed" ? failedOf(c) : c.answered === only)) && (!f || (c.host + c.path).toLowerCase().indexOf(f) >= 0)));
        if (ctx.sel) { list.select(ctx.sel, true); }
      };
      seg.addEventListener("click", (ev) => { const b = ev.target.closest("[data-only]"); if (b) { only = b.getAttribute("data-only"); ctx.memo.callsOnly = only; show(); } });
      show();
      return { onSelect: (ref) => list.select(ref, true), onFilter: show };
    }
  },
  {
    id: "dispatch", group: "Wire", label: "Dispatch table",
    count: (ctx) => ({ n: count(laneCount(ctx, "dispatch")) }),
    async render(host, ctx) {
      const d = await ctx.cache.read("dispatch");
      const faulted = (e) => e.fault || e.closed === "delayed" || e.closed === "dropped";
      const rows = d.entries, faults = rows.filter((r) => faulted(r.entry)).length, drawn = rows.filter((r) => r.entry.drawn).length;
      host.innerHTML = headHtml(count(rows.length), "entries the run loop's table held · " + faults + " held back, dropped or repeated by a dispatch rule · " + plural(drawn, "reply moment") + " drawn", faults ? "fail" : "") +
        '<div class="view-body" data-role="t" style="display:flex;flex-direction:column"></div>';
      if (!rows.length) { host.querySelector('[data-role="t"]').innerHTML = '<p class="empty">This run kept no table of what was due (a standing world is driven from outside).</p>'; return {}; }
      return table(host.querySelector('[data-role="t"]'), ctx, [
        { label: "Due", width: "140px", html: (r) => esc(when(r.entry.due.at)), sort: (r) => r.entry.due.at },
        { label: "What", width: "120px", html: (r) => esc(words(r.entry.due.kind)) },
        { label: "Put in by", width: "100px", html: (r) => esc(words(r.entry.source)) },
        { label: "Entered", width: "140px", html: (r) => esc(when(r.entry.entered_at)), sort: (r) => r.entry.entered_at },
        { label: "Left", width: "150px", html: (r) => (r.entry.closed ? (faulted(r.entry) ? '<span class="bad">' + esc(r.entry.closed) + "</span>" : esc(r.entry.closed)) + " " + esc(r.entry.closed_at ? when(r.entry.closed_at, { year: false }) : "") : "pending") },
        { label: "Fault", width: "80px", html: (r) => (r.entry.fault ? '<span class="bad">' + esc(r.entry.fault) + "</span>" : "") },
        { label: "Drawn", width: "minmax(140px, 1fr)", html: (r) => (r.entry.drawn ? esc(words(r.entry.drawn.source) + ": " + span(seconds(r.entry.drawn.offset)) + " after the ask") : "") }
      ], rows, (r) => "due:" + r.index);
    }
  },
  {
    id: "forks", group: "Runs", label: "Forks",
    count: (ctx) => ({ n: treeOf(ctx).size - 1 }),
    render(host, ctx) {
      const members = treeOf(ctx), root = rootOf(ctx, ctx.base.run.run_id);
      host.innerHTML = headHtml(String(members.size - 1), "forks of this run's tree · each split at a checkpoint, with what it changed and how it came out") + '<div class="view-body"><ul class="tree" data-role="tree"></ul></div>';
      const byId = {}; ctx.runs.forEach((r) => { byId[r.run_id] = r; });
      const node = (r) => '<li><span class="node' + (r.run_id === ctx.base.run.run_id ? " current" : "") + '" data-open="' + esc(r.run_id) + '">' + chip(r.verdict || "running", r.verdict ? VERDICT_WORD[r.verdict] : "running") +
        "<b>" + esc(r.parent_run ? r.changed || "rerun" : words(r.scenario)) + '</b><span class="muted mono">' + esc(r.run_id) + "</span></span>" +
        (r.parent_run ? '<span class="split">split after ' + (r.forked_after_wake !== null ? "wake " + r.forked_after_wake : "seq " + r.forked_at) + (r.forked_ran_on ? ", the clock had run on" : "") + "</span>" : "") +
        (r.children.length ? "<ul>" + r.children.map((c) => (byId[c] ? node(byId[c]) : "")).join("") + "</ul>" : "") + "</li>";
      host.querySelector('[data-role="tree"]').innerHTML = byId[root] ? node(byId[root]) : "";
      return {};
    }
  },
  {
    id: "samples", group: "Runs", label: "Samples",
    count: (ctx) => { const b = batchOf(ctx); return { n: b ? b.scenarios.filter((s) => s.matched).length + "/" + b.scenarios.length : (ctx.batches.length ? ctx.batches.length : "—"), cls: b && b.scenarios.some((s) => !s.matched) ? "fail" : "" }; },
    render(host, ctx) {
      const mine = batchOf(ctx), shown = mine ? [mine] : ctx.batches.slice().reverse();
      if (!shown.length) { host.innerHTML = headHtml("—", "This state directory holds no <code>run-all --samples</code> batch.") ; return {}; }
      const b0 = shown[0], matched = b0.scenarios.filter((s) => s.matched).length;
      host.innerHTML = headHtml(matched + " of " + b0.scenarios.length, "scenarios reached what they expect" + (mine ? " in the batch this run belongs to" : " in the latest batch") + " · " + plural(b0.samples, "sample") + " each", matched === b0.scenarios.length ? "pass" : "fail") +
        '<div class="view-body">' + shown.map((b) => '<table class="grid"><thead><tr><th>Scenario <span class="muted mono">' + esc(b.batch_id) + '</span></th><th>Expects</th><th>Pass rate</th><th class="r">Passed</th><th>Seeds that missed it</th></tr></thead><tbody>' +
          b.scenarios.map((s) => {
            const rate = s.samples.length ? s.passed / s.samples.length : 0;
            return "<tr><td>" + chip(s.matched ? "passed" : "fail", s.matched ? "met" : "missed") + " <b>" + esc(words(s.scenario)) + '</b><div class="muted">' + esc(s.file) + "</div></td><td>" + esc(s.expected) +
              '</td><td><div class="bar' + (s.matched ? "" : " low") + '"><i style="width:' + (rate * 100).toFixed(1) + '%"></i></div></td><td class="r">' + s.passed + "/" + s.samples.length +
              "</td><td>" + (s.samples.filter((x) => s.failing_seeds.indexOf(x.seed) >= 0).map((x) => (x.run_id ? '<button type="button" class="link" data-open="' + esc(x.run_id) + '" title="' + esc(x.words) + '">' + x.seed + "</button>" : '<span title="' + esc(x.words) + '">' + x.seed + "</span>")).join(" ") || '<span class="muted">none</span>') +
              '<div class="muted">every seed: ' + s.samples.map((x) => (x.run_id ? '<button type="button" class="link" data-open="' + esc(x.run_id) + '" title="' + esc(x.words) + '">' + x.seed + "</button>" : x.seed)).join(" ") + "</div></td></tr>";
          }).join("") + "</tbody></table>").join("") + "</div>";
      return {};
    }
  }
];

function tokensOf(traffic, people) {
  const t = { agentIn: 0, agentOut: 0, peopleIn: 0, peopleOut: 0, people: [] };
  traffic.calls.forEach((c) => { t.agentIn += c.input_tokens || 0; t.agentOut += c.output_tokens || 0; });
  people.people.forEach((p) => p.model_calls.forEach((line) => { t.peopleIn += line.call.input_tokens || 0; t.peopleOut += line.call.output_tokens || 0; t.people.push({ person: p.name, line }); }));
  t.total = t.agentIn + t.agentOut + t.peopleIn + t.peopleOut;
  return t;
}

function rootOf(ctx, id) {
  const byId = {}; ctx.runs.forEach((r) => { byId[r.run_id] = r; });
  let r = byId[id];
  while (r && r.parent_run && byId[r.parent_run]) { r = byId[r.parent_run]; }
  return r ? r.run_id : id;
}
function treeOf(ctx) {
  const byId = {}; ctx.runs.forEach((r) => { byId[r.run_id] = r; });
  const out = new Set(), walk = (id) => { if (!byId[id] || out.has(id)) { return; } out.add(id); byId[id].children.forEach(walk); };
  walk(rootOf(ctx, ctx.base.run.run_id));
  if (!out.size) { out.add(ctx.base.run.run_id); }
  return out;
}
export function batchOf(ctx) {
  const id = ctx.base.run.run_id;
  for (let i = ctx.batches.length - 1; i >= 0; i--) {
    if (ctx.batches[i].scenarios.some((s) => s.samples.some((x) => x.run_id === id))) { return ctx.batches[i]; }
  }
  return null;
}
export { VERDICT_WORD };
