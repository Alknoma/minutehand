/* The inspector: whatever is selected anywhere on the page, read whole. A selection is a ref the timeline and the
   views share: `finding:<n>`, `ev:<seq>`, `call:<i>`, `due:<i>`, `mc:<span>`, `pc:<i>`, `reply:<i>`, `wake:<n>`,
   `span:<id>`, `wait:<key>`. Each kind reads what it needs from the API (kept per run) and nothing more. */

import { esc, when, clock, span, seconds, words, title, plural, prettyJson, isJson, diffLines, layout, count } from "./util.js";

const ANSWERED = {
  provider: "answered by the provider's fake", declaration: "answered as declared, never sent", pass_through: "passed through to the real host",
  recording: "REPLAYED from a recording", emulator: "answered by an external emulator", model: "answered by a model standing in for the service, never sent",
  tunnel: "relayed unopened on a tunnel (a model host)", minutehand: "Minutehand's own call, as a person, to the agent's product", refused: "refused: nothing answered it"
};
const JOINED = { trace: "joined by its trace", content: "joined by content: the model answered this text verbatim", wake: "the nearest model call in the same wake, not proven" };
const WRITING = { script: "a model, from a step of their script", verbatim: "the script's exact words, no model", conversing: "a model, from their own facts", automatic: "their automatic reply, no model", by_hand: "written by hand by whoever drove the world" };
const DRAWN = { delay: "their reply's delay range", window: "their reply window, in their available time", reminded: "drawn again on a follow-up", pinned: "pinned by a fork, no draw", automatic: "at once, an automatic reply" };

export function link(sel, text) { return '<button type="button" class="link" data-sel="' + esc(sel) + '">' + esc(text) + "</button>"; }

function head(kind, titleText, whenText, chip) {
  return '<div class="insp-head"><div class="insp-kind">' + esc(kind) + (chip || "") + '</div><div class="insp-title">' + esc(titleText) + "</div>" +
    (whenText ? '<div class="insp-when">' + whenText + "</div>" : "") + "</div>";
}
function section(name, body) { return body ? '<section class="insp-section"><h3>' + esc(name) + "</h3>" + body + "</section>" : ""; }
function kv(pairs) {
  const rows = pairs.filter((p) => p[1] !== null && p[1] !== undefined && p[1] !== "");
  return rows.length ? '<dl class="kv">' + rows.map((p) => "<dt>" + esc(p[0]) + "</dt><dd>" + p[1] + "</dd>").join("") + "</dl>" : "";
}
function textBlock(text) { return text === null || text === undefined ? "" : isJson(text) ? '<pre class="json">' + prettyJson(text) + "</pre>" : '<p class="said">' + esc(text) + "</p>"; }
function diff(before, after) {
  if (before === null || before === undefined) { return textBlock(after === null ? "(deleted)" : after); }
  if (after === null || after === undefined) { return '<div class="diff">' + layout(before).split("\n").map((l) => '<div class="del">' + esc(l) + "</div>").join("") + "</div>"; }
  const lines = diffLines(layout(before), layout(after));
  if (!lines) { return section("Before", textBlock(before)) + section("After", textBlock(after)); }
  return '<div class="diff">' + lines.map(([k, l]) => '<div class="' + k + '">' + esc(l) + "</div>").join("") + "</div>";
}
function simWhen(t, wake) { return esc(when(t, { seconds: true })) + " simulated" + (wake !== undefined && wake !== null ? " · " + (wake ? "wake " + wake : "setup") : ""); }

/** What a model was asked or answered, as GenAI semantic conventions carry it: one block per message and part. */
function said(text) {
  if (text === null || text === undefined) { return ""; }
  let parsed;
  try { parsed = JSON.parse(text); } catch (e) { return text; }
  if (!Array.isArray(parsed)) { return layout(text); }
  return parsed.map((m) => {
    if (m.type === "text" && m.content !== undefined) { return layout(m.content); }
    const parts = (m.parts || []).map((part) => {
      if (part.type === "text") { return layout(part.content); }
      if (part.type === "tool_call") { return "[calls " + part.name + "]" + (part.arguments ? "\n" + layout(typeof part.arguments === "string" ? part.arguments : JSON.stringify(part.arguments)) : ""); }
      if (part.type === "tool_call_response") { return "[answer to a tool call]\n" + layout(typeof part.response === "string" ? part.response : JSON.stringify(part.response)); }
      return "[" + part.type + "]";
    });
    return (m.role ? m.role + ":\n" : "") + parts.join("\n");
  }).join("\n\n");
}

function exchangeBlocks(e) {
  const req = e.request_body !== null && e.request_body !== undefined ? textBlock(e.request_body) : e.request_bytes ? '<p class="muted">' + plural(atob(e.request_bytes).length, "byte") + " that are not text</p>" : '<p class="muted">No body</p>';
  const res = e.response_body !== null && e.response_body !== undefined ? textBlock(e.response_body) : e.response_bytes ? '<p class="muted">' + plural(atob(e.response_bytes).length, "byte") + " that are not text</p>" : '<p class="muted">No body</p>';
  return section("Request", '<p class="mono">' + esc(e.method + " " + e.host + e.path) + "</p>" + req) + section("Response · " + e.status, res);
}

export async function inspect(ctx, ref) {
  if (!ref) { return empty(ctx); }
  const kind = ref.split(":")[0], rest = ref.slice(kind.length + 1);
  const render = KINDS[kind];
  if (!render) { return head("Unknown", ref) + '<p class="insp-empty">Nothing here is called ' + esc(ref) + ".</p>"; }
  try { return await render(ctx, rest); } catch (e) { return head(kind, ref) + '<p class="insp-empty">Could not read it: ' + esc(e.message) + "</p>"; }
}

function empty(ctx) {
  const f = ctx.base.findings;
  return '<div class="insp-empty"><h2>Select anything to read it here</h2>' +
    "<p>A mark on the timeline, a finding above, a row in a view. Every message, call, model call, memory write, stored item, dispatch entry and finding opens whole, with links to what it touches.</p>" +
    "<p><kbd>j</kbd> <kbd>k</kbd> step through the marks · <kbd>←</kbd> <kbd>→</kbd> pan · <kbd>+</kbd> <kbd>−</kbd> zoom · <kbd>/</kbd> filter · <kbd>?</kbd> every key</p>" +
    (f.finished ? "" : "<p>The run is still going: the findings come when it finishes.</p>") + "</div>";
}

const KINDS = {
  async finding(ctx, rest) {
    const x = ctx.base.findings.findings.find((f) => String(f.number) === rest);
    if (!x) { return head("Finding", "No finding " + rest); }
    const f = x.finding, labels = ctx.labelOf;
    const evidence = f.evidence.length ? '<ul class="links">' + f.evidence.map((s) =>
      '<li><span class="w">seq ' + s + "</span>" + link("ev:" + s, labels("ev:" + s) || "event " + s) + "</li>").join("") + "</ul>" : '<p class="muted">It cites no event.</p>';
    return head("Finding " + x.number, x.pattern ? x.pattern.title : title(f.check), (f.at ? simWhen(f.at, f.wake) : ""), ' <span class="chip ' + f.kind + '">' + esc(f.kind === "informational" ? "note" : f.kind) + "</span>") +
      section("What it found", '<p class="said">' + esc(f.message) + "</p>") +
      section("Evidence", evidence) +
      (f.assessed ? section("Measured against the declared world", kv([["Is", esc(f.assessed.kind.replace("_", " "))], ["Item", f.assessed.item === null ? null : esc(f.assessed.item.replace("_", " "))], ["Against", esc(f.assessed.against)], ["Calls", f.calls.length ? esc(f.calls.join(", ")) : null]])) : "") +
      section("The rule", kv([["Check", esc(f.check)], ["Severity", esc(f.severity)], ["Wake", f.wake === null ? null : esc(f.wake)]])) +
      (x.pattern ? section("How a proactive agent avoids it", "<p>" + esc(x.pattern.failure) + "</p><p><b>" + esc(x.pattern.design) + "</b></p>") : "") +
      (f.judged ? section("Judged by a model", kv([["Model", esc(f.judged.model)], ["Prompt", esc(f.judged.prompt_version)]]) + '<p class="said">' + esc(f.judged.rationale) + "</p>") : "");
  },

  async ev(ctx, rest) {
    const d = await ctx.cache.read("event", { seq: rest });
    const e = d.event, a = e.after;
    let body = "", kindWord = words(e.entity.kind), name = e.entity.external_id;
    const facts = [["Provider", esc(e.entity.provider)], ["Entity", '<span class="mono">' + esc(e.entity.kind + " " + e.entity.external_id) + "</span>"],
      ["By", esc(e.actor)], ["Operation", esc(e.operation)], ["Real time", esc(clock(e.wall_time))], ["Seq", esc(e.seq)]];
    if (a && a.kind === "message") {
      kindWord = e.actor === "agent" ? "Message from the agent" : "Message from " + (d.reply !== null ? ctx.personName(d.reply) : "a person");
      name = a.text;
      body += section(e.operation === "update" ? "Now says" : e.operation === "delete" ? "Deleted; it said" : "Says", '<p class="said">' + esc(a.text) + "</p>");
      if (d.before && d.before.kind === "message" && d.before.text !== a.text) { body += section("Before", '<p class="said">' + esc(d.before.text) + "</p>"); }
      facts.unshift(["To", esc(a.recipient_emails.join(", ") || "nobody in the scenario")], ["Channel", esc(a.channel)]);
      if (d.written_by) { body += section("Written by", kv([["Model call", link("mc:" + d.written_by.span_id, d.written_by.model || "a model")], ["How joined", esc(JOINED[d.written_by.joined_by])]])); }
      if (d.reply !== null) { body += section("Delivered", "<p>" + link("reply:" + d.reply, "The reply this message delivered") + "</p>"); }
      if (d.thread.length > 1) {
        body += section("The conversation", '<div class="thread">' + d.thread.map((m) =>
          '<div class="msg ' + (m.actor === "agent" ? "agent" : "person") + (m.seq === e.seq ? " on" : "") + '" data-sel="ev:' + m.seq + '"><div class="who">' + esc((m.actor === "agent" ? "agent → " + (m.to.join(", ") || "channel") : m.actor) + " · " + when(m.at) + (m.change !== "sent" ? " · " + m.change : "")) + "</div>" + esc(m.text) + "</div>").join("") + "</div>");
      }
    } else if (a && a.kind === "memory") {
      kindWord = "Memory " + (e.operation === "read" || e.operation === "search" ? (a.listing ? "listing" : "read") : e.operation === "delete" ? "delete" : "write");
      name = a.collection + " / " + a.key;
      if (e.operation !== "read" && e.operation !== "search") {
        body += section(d.before ? "Before → after" : "Value", diff(d.before && d.before.kind === "memory" ? d.before.value : null, a.value));
      }
    } else if (a && a.kind === "stored") {
      kindWord = "Stored item"; name = a.collection + " / " + a.id;
      facts.unshift(["Host", esc(a.host)], ["Path", '<span class="mono">' + esc(a.path) + "</span>"]);
      body += section(d.before ? "Before → after" : "Item", diff(d.before && d.before.kind === "stored" ? d.before.item : null, a.item));
    } else if (a && a.kind === "inbox_item") {
      kindWord = "Item in the agent's product"; name = a.summary;
      body += section("Item", kv([["Waits on", esc(a.waits_on)], ["Status", esc(a.status)], ["Decisions", esc(a.decisions.join(", "))], ["Decision", esc(a.said || a.decision)], ["Refused", esc(a.refused)]]));
    } else if (a) {
      body += section("As it stood after", '<pre class="json">' + prettyJson(JSON.stringify(a)) + "</pre>");
    }
    if (e.exchange) { body += exchangeBlocks(e.exchange); }
    if (d.call !== null) { body += section("The call that made it", "<p>" + link("call:" + d.call, "Call " + d.call) + "</p>"); }
    if (d.findings.length) { body += section("Findings that cite it", '<ul class="links">' + d.findings.map((n) => "<li>" + link("finding:" + n, ctx.findingTitle(n)) + "</li>").join("") + "</ul>"); }
    return head(kindWord, name, simWhen(e.sim_time, e.wake)) + body + section("Record", kv(facts));
  },

  async call(ctx, rest) {
    const d = await ctx.cache.read("call", { call: rest });
    const c = d.call, e = c.exchange, cap = e.captured, row = d.row;
    const events = row.first_seq === null ? [] : Array.from({ length: row.last_seq - row.first_seq + 1 }, (_, i) => row.first_seq + i);
    const timing = cap ? span((Date.parse(cap.ended) - Date.parse(cap.started)) / 1000) : e.tunnelled ? span((Date.parse(e.tunnelled.ended) - Date.parse(e.tunnelled.started)) / 1000) : null;
    const facts = [["Who answered", esc(ANSWERED[row.answered_by])], ["Provider", esc(c.provider)], ["Outcome", esc(e.outcome ? words(e.outcome) : null)],
      ["Captured as", cap ? esc(words(cap.mode) + (cap.declared_as ? " (declared " + cap.declared_as + ")" : "")) : null],
      ["Replayed from", cap ? esc(cap.replayed_from) : null], ["Note", cap ? esc(cap.note) : null], ["Took", timing ? esc(timing) : null],
      ["Recipients", cap && cap.recipients.length ? esc(cap.recipients.map((r) => r.address + (r.person ? "" : " (nobody in the scenario)")).join(", ")) : null],
      ["gRPC", e.grpc ? esc(e.grpc.code + (e.grpc.message ? ": " + e.grpc.message : "")) : null],
      ["Trace", e.traceparent ? '<span class="mono">' + esc(e.traceparent) + "</span>" : null]];
    const chip = e.status >= 400 || row.answered_by === "refused" ? ' <span class="chip fail">' + esc(e.status) + "</span>" : ' <span class="chip plain">' + esc(e.status) + "</span>";
    let body = section("How it was answered", kv(facts));
    if (e.tunnelled) {
      body += section("Tunnel", kv([["Connection", esc(e.tunnelled.connection)], ["Burst", esc(e.tunnelled.burst)], ["Sent", esc(count(e.tunnelled.bytes_sent) + " bytes")], ["Received", esc(count(e.tunnelled.bytes_received) + " bytes")]]) + '<p class="muted">Its bodies were never opened.</p>');
    } else { body += exchangeBlocks(e); }
    if (e.failure) { body += section("Minutehand answered in the fake's place", '<p class="said">' + esc(e.failure.message) + "</p>" + (e.failure.traceback ? '<pre class="json">' + esc(e.failure.traceback) + "</pre>" : "")); }
    if (events.length) { body += section("Events it produced", '<ul class="links">' + events.map((s) => '<li><span class="w">seq ' + s + "</span>" + link("ev:" + s, ctx.labelOf("ev:" + s) || "event " + s) + "</li>").join("") + "</ul>"); }
    return head("HTTP call " + row.call_id, e.method + " " + e.host + e.path.split("?")[0], simWhen(c.sim_time, c.wake), chip) + body;
  },

  async due(ctx, rest) {
    const d = await ctx.cache.read("dispatch");
    const en = d.entries.find((r) => String(r.due_id) === rest);
    if (!en) { return head("Dispatch entry", "No entry " + rest); }
    const dr = en.drawn;
    const chip = en.fault || en.closed === "delayed" || en.closed === "dropped" ? ' <span class="chip fail">' + esc(en.fault || en.closed) + "</span>" : "";
    let body = section("The entry", kv([["What", esc(words(en.kind))], ["For", '<span class="mono">' + esc(en.ref) + "</span>"], ["Put in by", esc(words(en.source))],
      ["Entered", esc(when(en.entered_at)) + " · " + (en.entered_wake ? "wake " + en.entered_wake : "setup")], ["Due", esc(when(en.due_at, { seconds: true }))],
      ["Left", en.closed ? esc(en.closed + " " + when(en.closed_at)) + (en.closed_wake ? " · wake " + en.closed_wake : "") : "still in the table"],
      ["Dispatch rule", esc(en.fault)], ["Asked for", en.asked_for ? esc(when(en.asked_for, { seconds: true })) : null]]));
    if (dr) {
      body += section("How its moment was drawn", kv([["From", esc(DRAWN[dr.source] || dr.source)], ["Seed", esc(dr.seed)], ["Measured from", esc(when(dr.asked_at))],
        ["Range", dr.delay ? esc(span(seconds(dr.delay.shortest)) + " to " + span(seconds(dr.delay.longest))) : null],
        ["Window", dr.window ? '<span class="mono">' + esc(JSON.stringify(dr.window)) + "</span>" : null],
        ["The draw", esc(span(seconds(dr.offset)) + " after")], ["Lands", esc(when(dr.lands_at, { seconds: true }))]]));
    } else if (en.drawn_from) {
      body += section("How its moment was drawn", kv([["From", esc(DRAWN[en.drawn_from] || en.drawn_from)], ["The draw", en.drawn_offset_seconds === null ? null : esc(span(en.drawn_offset_seconds) + " after")]]));
    }
    return head("Dispatch entry", words(en.kind) + " (" + words(en.source) + ")", esc(when(en.due_at, { seconds: true })) + " simulated", chip) + body;
  },

  async mc(ctx, rest) {
    const d = await ctx.cache.read("modelCall", { span: rest });
    const c = d.call, took = (Date.parse(c.ended) - Date.parse(c.started)) / 1000;
    return head("Agent's model call", c.model || "model not named", esc(clock(c.started)) + " real · took " + esc(span(took)) + " · wake " + c.wake) +
      section("Cost", kv([["Tokens in", esc(c.input_tokens === null ? "not said" : c.input_tokens.toLocaleString("en"))], ["Tokens out", esc(c.output_tokens === null ? "not said" : c.output_tokens.toLocaleString("en"))], ["Provider", esc(c.system)], ["Span", '<span class="mono">' + esc(c.span_id) + "</span>"]])) +
      (c.system_instructions ? section("System", '<p class="said">' + esc(said(c.system_instructions)) + "</p>") : "") +
      section("Asked", c.input_messages ? '<p class="said">' + esc(said(c.input_messages)) + "</p>" : '<p class="muted">The span carries no input.</p>') +
      section("Answered", c.output_messages ? '<p class="said">' + esc(said(c.output_messages)) + "</p>" : '<p class="muted">The span carries no output.</p>') +
      (d.wrote.length ? section("It wrote", '<ul class="links">' + d.wrote.map((s) => "<li>" + link("ev:" + s, ctx.labelOf("ev:" + s) || "message " + s) + "</li>").join("") + "</ul>") : "");
  },

  async pc(ctx, rest) {
    const p = await ctx.cache.read("people");
    for (const person of p.people) {
      const line = person.model_calls.find((m) => String(m.index) === rest);
      if (!line) { continue; }
      const c = line.call;
      const reply = person.replies.find((r) => r.reply.written_by && r.reply.at === c.sim_time);
      return head("Model call for a person", c.wrote + " for " + person.name, simWhen(c.sim_time, c.wake), c.failure ? ' <span class="chip fail">failed</span>' : c.replayed ? ' <span class="chip plain">replayed</span>' : "") +
        section("Cost", kv([["Model", esc(c.model)], ["Prompt", esc(c.prompt_version)], ["Tokens in", esc(c.input_tokens === null ? "not said" : c.input_tokens.toLocaleString("en"))], ["Tokens out", esc(c.output_tokens === null ? "not said" : c.output_tokens.toLocaleString("en"))], ["Replayed", c.replayed ? "yes: answered from the world's record, no model called" : null]])) +
        (c.failure ? section("Failed", '<p class="said">' + esc(c.failure) + "</p>") : "") +
        section("Answer", textBlock(c.answer)) +
        (reply ? section("The reply it wrote", "<p>" + link("reply:" + reply.index, reply.reply.text) + "</p>") : "");
    }
    return head("Model call for a person", "No call " + rest);
  },

  async reply(ctx, rest) {
    const p = await ctx.cache.read("people");
    for (const person of p.people) {
      const line = person.replies.find((r) => String(r.index) === rest);
      if (!line) { continue; }
      const r = line.reply, dr = r.drawn;
      return head("Reply from " + person.name, r.text, simWhen(r.at)) +
        section("Said", '<p class="said">' + esc(r.text) + "</p>") +
        section("Written", kv([["By", esc(WRITING[r.writing] || r.writing)], ["Model", r.written_by ? esc(r.written_by.model + " · prompt " + r.written_by.prompt_version) : null],
          ["Facts it carries", r.facts.length ? esc(r.facts.join("; ")) : null], ["Pressed", r.press ? esc(r.press.label) : null], ["Decided", r.decides ? esc(r.decides.decision) : null]])) +
        (dr ? section("When it landed", kv([["Drawn from", esc(DRAWN[dr.source] || dr.source)], ["Asked", esc(when(dr.asked_at))], ["After", esc(span(seconds(dr.offset)))],
          ["Range", dr.delay ? esc(span(seconds(dr.delay.shortest)) + " to " + span(seconds(dr.delay.longest))) : null], ["Seed", esc(dr.seed)]])) : "") +
        section("Links", '<ul class="links">' + (line.asked ? "<li>" + link("ev:" + line.asked, "What it answers") + "</li>" : "") + (line.seq ? "<li>" + link("ev:" + line.seq, "The message that delivered it") + "</li>" : "<li class=\"muted\">Delivered to the agent directly: no event in the world's log</li>") + "</ul>");
    }
    return head("Reply", "No reply " + rest);
  },

  async wake(ctx, rest) {
    const w = (await ctx.cache.read("wakes")).wakes.find((x) => String(x.index) === rest);
    let body = w ? section("What it did", kv([["Changes", esc(w.world_changes)], ["Commitments changed", w.commitments_changed ? "yes" : "no"], ["Memory", esc(w.memory_reads + " reads, " + w.memory_writes + " writes")], ["Why", esc(w.reason)], ["Inferred", w.inferred ? "yes: a step nobody marked" : null]])) : "";
    try {
      const sp = (await ctx.cache.read("stepSpans", { step: rest })).spans;
      if (sp.length) {
        const t0 = Math.min(...sp.map((s) => Date.parse(s.start))), t1 = Math.max(...sp.map((s) => Date.parse(s.end))), wd = Math.max(1, t1 - t0);
        const byId = {}; sp.forEach((s) => { byId[s.span_id] = s; });
        const depth = (s) => { let n = 0, p = s.parent_span_id; while (p && byId[p] && n < 12) { n++; p = byId[p].parent_span_id; } return n; };
        body += section("Its spans · " + span(wd / 1000) + " real", '<div class="waterfall">' + sp.slice(0, 200).map((s) => {
          const l = (Date.parse(s.start) - t0) / wd * 100, r = Math.max(0.4, (Date.parse(s.end) - Date.parse(s.start)) / wd * 100);
          const sel = s.model_call ? "mc:" + s.span_id : "span:" + s.span_id;
          return '<div class="wf" data-sel="' + esc(sel) + '" title="' + esc(s.name + " · " + span((Date.parse(s.end) - Date.parse(s.start)) / 1000)) + '"><span style="padding-left:' + depth(s) * 8 + 'px">' + esc(s.name) + '</span><div class="track"><i class="' + (s.model_call ? "model" : s.status === "error" ? "error" : "") + '" style="left:' + l + "%;width:" + r + '%"></i></div></div>';
        }).join("") + "</div>" + (sp.length > 200 ? '<p class="muted">The first 200 of ' + sp.length + " spans.</p>" : ""));
      }
    } catch (e) { /* a wake with no spans */ }
    return head(ctx.base.run.driven ? "Step" : "Wake", (ctx.base.run.driven ? "Step " : "Wake ") + rest, w ? simWhen(w.sim_time) : "") + body;
  },

  async span(ctx, rest) {
    const s = (await ctx.cache.read("span", { span: rest })).span, sp = s.span;
    return head("Span", sp.name, esc(clock(sp.start)) + " real · took " + esc(span((Date.parse(sp.end) - Date.parse(sp.start)) / 1000)) + " · wake " + s.wake) +
      section("Span", kv([["Service", esc(sp.service_name)], ["Status", esc(sp.status + (sp.status_message ? ": " + sp.status_message : ""))], ["Trace", '<span class="mono">' + esc(sp.trace_id) + "</span>"], ["Source", esc(s.source)], ["Placed by", esc(s.placed_by)]])) +
      section("Attributes", sp.attributes.length ? kv(sp.attributes.map((at) => [at.key, at.value === null ? "" : '<span class="mono">' + esc(at.value.value !== undefined ? String(at.value.value) : JSON.stringify(at.value)) + "</span>"])) : '<p class="muted">None.</p>');
  },

  async wait(ctx, rest) {
    const w = (await ctx.cache.read("obligations")).obligations.find((x) => x.obligation.key === rest);
    if (!w) { return head("Wait", "No wait " + rest); }
    const o = w.obligation;
    const touches = o.agent_touches.map((s, i) => '<li><span class="w">' + esc(w.touched_at[i] ? when(w.touched_at[i]) : "") + "</span>" + link("ev:" + s, ctx.labelOf("ev:" + s) || "event " + s) + "</li>").join("");
    return head("Wait on " + ctx.personKey(o.person), words(o.kind), simWhen(o.opened_at), o.settled_at ? "" : ' <span class="chip review">open</span>') +
      section("The wait", kv([["Opened by", link("ev:" + o.opened_by, ctx.labelOf("ev:" + o.opened_by) || "event " + o.opened_by)], ["Expected by", o.expected_by ? esc(when(o.expected_by)) : "no date applies"],
        ["Patience", o.patience ? esc(span(seconds(o.patience))) : null], ["Settled", o.settled_at ? esc(when(o.settled_at)) + " · after " + esc(span((Date.parse(o.settled_at) - Date.parse(o.opened_at)) / 1000)) : "never: still open at the end"],
        ["Agent came back", w.came_back_at ? esc(when(w.came_back_at)) + (o.settled_at ? " · " + esc(span((Date.parse(w.came_back_at) - Date.parse(o.settled_at)) / 1000)) + " after it settled" : "") : null]])) +
      section("While it was open, the agent wrote " + plural(o.agent_touches.length, "time"), touches ? '<ul class="links">' + touches + "</ul>" : '<p class="muted">Nothing.</p>');
  }
};
