/* The agent's outputs: each write of the agent's, the kind of item it is, and what every check its kind takes said
   of it (`RunResult.items`). The calendar, the day and the inspector read them from here, and mark them one way:
   the shape says the kind of item, the colour the result. */

import { esc, words } from "./util.js";

/** What each item check looks for, as a reader says it. */
export const CHECK_WORD = {
  duplicate: "Sent again, word for word",
  repeated_without_news: "Told again with nothing new",
  inside_reply_window: "Chased before their time to answer",
  to_someone_away: "Written to someone away",
  breaks_thread: "Asked outside the open thread",
  after_deadline: "Written after the deadline",
  duplicate_ticket: "Filed while the same one is open",
  stale_state: "Wrote over a change it had not read",
  outside_working_hours: "Set outside someone's hours",
  double_booked: "Set over another event",
  moved_without_notice: "Moved without telling anyone",
  refused_move: "A move the service refused",
  abandoned: "Left where only the agent could move it",
  late_reaction: "Came back late",
  written_twice: "Written twice",
  redundant_reads: "Read again and again",
  before_decision: "Acted before the decision",
  deadline_missed: "Deadline missed"
};

/** Each kind of item: its name and the shape it is drawn as. */
export const KIND = {
  chat_message: ["Chat message", "circle"], email: ["Email", "square"], comment: ["Comment", "circle"],
  ticket: ["Ticket", "diamond"], calendar_event: ["Calendar event", "triangle"], document: ["Document", "bar"],
  service_item: ["Service item", "diamond"], stored_record: ["Stored record", "bar"]
};
export function kindWord(kind) { return (KIND[kind] || [words(kind)])[0]; }
export function shapeOf(kind) { return (KIND[kind] || [null, "circle"])[1]; }

/** One output's result: the worst any check said; `held` when every check measured it and found nothing, `unmeasured`
    when no check had anything declared to measure it against. */
export const RESULT_WORD = { failed: "Failed", review: "To review", held: "Passed", unmeasured: "Not measured" };
export function resultOf(item) {
  const said = item.checks.map((c) => c.outcome);
  if (said.indexOf("failed") >= 0) { return "failed"; }
  if (said.indexOf("review") >= 0) { return "review"; }
  if (said.indexOf("held") >= 0) { return "held"; }
  return "unmeasured";
}

/** A mark: the item's shape in its result's colour. */
export function markHtml(kind, result, extra) {
  return '<i class="shape ' + esc(shapeOf(kind)) + " " + esc(result) + '"' + (extra || "") + "></i>";
}

/** Every output of the run in time order, each with its moment, its result and the event it is. */
export function outputsOf(ctx) {
  const m = ctx.base.timeline.marks, at = new Map();
  for (let i = 0; i < m.ref.length; i++) { if (!at.has(m.ref[i])) { at.set(m.ref[i], i); } }
  return (ctx.base.findings.items || []).map((item) => {
    const i = at.get("ev:" + item.seq);
    return { item, ref: "ev:" + item.seq, sim: i === undefined ? null : m.sim[i], label: i === undefined ? "" : m.label[i], lane: i === undefined ? null : ctx.base.timeline.lanes[m.lane[i]], result: resultOf(item) };
  }).filter((o) => o.sim !== null).sort((a, b) => a.sim - b.sim || a.item.seq - b.item.seq);
}

/** The finding a check made of an output, if it made one: the finding citing it of that check. */
export function findingOf(ctx, seq, check) {
  return ctx.base.findings.findings.find((x) => x.finding.check === check && x.finding.evidence.indexOf(seq) >= 0) || null;
}
