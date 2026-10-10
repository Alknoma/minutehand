// A Vercel AI SDK agent that asks a colleague in Slack, remembers when it expects an answer, and follows up once.
//
// It is proactive: it decides when it next wakes. Nothing wakes it on a schedule; its report names the moment it
// wants waking next (`next_wake`), or none.
//
// Each wake or Slack event is one `generateText` call with three tools and a stop condition, the AI SDK's
// tool loop. The tools write the waits into `memory` (`remember_wait`, `close_wait`), which is recalled from the
// store (`minutehand-store.ts`) on every wake, event and report and kept back after; the report Minutehand asks for
// after every wake reads it: `next_wake` is the earliest moment a wait is expected by. Under Minutehand the store is
// the run's own memory, so a fork starts from what the agent remembered at its checkpoint.
//
//     POST /wake          {"now": ..., "reason": "start" | "due" | ...}: run the agent on what is due
//     GET  /report        {"status": "idle" | "done", "next_wake": the earliest expected-by date, or null}
//     POST /slack/events  Slack's Events API: a person's answer, run through the agent
//
// It never reads the machine's clock: the time is the wake's `now`, or a Slack event's own timestamp.
//
//     npm install && node agent.ts        (Node 22.18 or later runs TypeScript as it is)
//
// Environment:
//     MODEL_BASE_URL              the chat-completions API (default the recipes' fake model, http://127.0.0.1:8790/v1)
//     OPENAI_API_KEY              its key (default a made-up one, which the fake model accepts)
//     AGENT_SLACK_SIGNING_SECRET  the secret Slack signs its events with; Minutehand makes one per run
//     SLACK_API_URL               Slack's Web API (default https://slack.com/api/); Minutehand hands out its proxy's
//     PORT                        where to listen (default 8715, the port agent.yaml names)

import { createHmac, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import { createOpenAI } from "@ai-sdk/openai";
import { WebClient } from "@slack/web-api";
import { generateText, isStepCount, tool } from "ai";
import { z } from "zod";

import * as store from "./minutehand-store.ts";

const OWNER = "owen@example.com"; // whom it tells the outcome
// Its work, as its own configuration says: the run hands it none.
const WORK = "Confirm the venue for the team offsite with Rosa, and tell Owen what she said.";
const ASK = "rosa@example.com"; // who knows the answer
const SYSTEM =
  "You keep your work moving by asking a colleague in Slack. Whenever you ask, remember the wait with " +
  "the date you expect an answer by. Follow up once; after that, tell the owner. When the answer comes, thank " +
  "the colleague, tell the owner what they said, and close the wait.";

// @slack/web-api calls Node's own fetch, which ignores HTTPS_PROXY unless Node runs with --use-env-proxy, so it is
// given Slack's base URL instead:
// Minutehand hands the agent SLACK_API_URL, its proxy's /_host/slack.com/api/ (agent.yaml, `base_urls`).
const slack = new WebClient("xoxb-recipe-agent", { slackApiUrl: process.env.SLACK_API_URL ?? "https://slack.com/api/" });
const openai = createOpenAI({
  baseURL: process.env.MODEL_BASE_URL ?? "http://127.0.0.1:8790/v1",
  apiKey: process.env.OPENAI_API_KEY ?? "sk-recipe",
});

type Wait = { expectedBy: Date; asks: number };
// What the agent knows between runs: its work and who owes it an answer by when. Recalled from the store before
// each wake, event and report, and kept back after each wake and event.
const memory: { work: string | null; waits: Map<string, Wait> } = { work: null, waits: new Map() };

async function recall(): Promise<void> {
  const work = await store.get("work");
  memory.work = typeof work === "string" ? work : null;
  memory.waits = new Map();
  for (const [email, kept] of await store.list("", "waits")) {
    const wait = kept as { expected_by: string; asks: number };
    memory.waits.set(email, { expectedBy: new Date(wait.expected_by), asks: wait.asks });
  }
}

async function keep(): Promise<void> {
  const writes: store.Write[] = [{ op: "put", collection: "default", key: "work", value: memory.work }];
  for (const [email] of await store.list("", "waits")) {
    if (!memory.waits.has(email)) writes.push({ op: "delete", collection: "waits", key: email });
  }
  for (const [email, wait] of memory.waits) {
    const value = { expected_by: iso(wait.expectedBy), asks: wait.asks };
    writes.push({ op: "put", collection: "waits", key: email, value });
  }
  await store.write(writes);
}

async function remembering(work: () => Promise<void>): Promise<void> {
  await recall();
  try {
    await work();
  } finally {
    await keep();
  }
}

const tools = {
  send_slack_message: tool({
    description: "Send a direct message in Slack to the person with this email address.",
    inputSchema: z.object({ email: z.string(), text: z.string() }),
    execute: async ({ email, text }) => {
      const found = await slack.users.lookupByEmail({ email });
      const opened = await slack.conversations.open({ users: found.user!.id! });
      await slack.chat.postMessage({ channel: opened.channel!.id!, text });
      return `sent to ${email}`;
    },
  }),
  remember_wait: tool({
    description: "Remember that `email` owes an answer, expected by `expected_by` (ISO 8601). Call it after every ask.",
    inputSchema: z.object({ email: z.string(), expected_by: z.iso.datetime({ offset: true }) }),
    execute: async ({ email, expected_by }) => {
      const asks = (memory.waits.get(email)?.asks ?? 0) + 1;
      memory.waits.set(email, { expectedBy: new Date(expected_by), asks });
      return `waiting on ${email} until ${expected_by}`;
    },
  }),
  close_wait: tool({
    description: "Nothing more is owed by `email`: they answered, or the owner has been told they did not.",
    inputSchema: z.object({ email: z.string() }),
    execute: async ({ email }) => {
      memory.waits.delete(email);
      return `closed ${email}`;
    },
  }),
};

async function run(now: Date, happened: string): Promise<void> {
  const situation = `It is now ${iso(now)}.\nWork: ${memory.work}\nReport to: ${OWNER}. Ask: ${ASK}.\n${happened}`;
  const result = await generateText({
    model: openai.chat("gpt-4.1-mini"),
    instructions: SYSTEM,
    prompt: situation,
    tools,
    stopWhen: isStepCount(5),
  });
  // The tool loop hands a failed tool's error back to the model; this agent fails its wake instead, so a broken
  // Slack call is seen rather than talked around.
  const failed = result.steps.flatMap((step) => step.content).filter((part) => part.type === "tool-error");
  if (failed.length > 0) throw new Error(`a tool failed: ${failed.map((part) => String(part.error)).join("; ")}`);
}

function iso(moment: Date): string {
  return moment.toISOString().replace(".000Z", "+00:00");
}

// -- the Minutehand side: three endpoints ------------------------------------------------------------------------

async function wake(request: { now: string; reason: string }): Promise<void> {
  const now = new Date(request.now);
  if (request.reason === "start") {
    memory.work = WORK;
    await run(now, "Nobody has been asked yet.");
    return;
  }
  for (const [email, wait] of [...memory.waits]) {
    if (wait.expectedBy <= now) {
      const overdue = `No answer yet from ${email}, expected by ${iso(wait.expectedBy)}. Follow-ups sent: ${wait.asks - 1}.`;
      await run(now, overdue);
    }
  }
}

function report(): { status: string; next_wake: string | null } {
  if (memory.work === null) return { status: "idle", next_wake: null };
  const dates = [...memory.waits.values()].map((w) => w.expectedBy.getTime());
  if (dates.length === 0) return { status: "done", next_wake: null };
  return { status: "idle", next_wake: iso(new Date(Math.min(...dates))) };
}

async function message(event: { user: string; text: string; ts: string }): Promise<void> {
  // Someone wrote to the agent: an answer from a person it waits on goes through the agent.
  const who = await slack.users.info({ user: event.user });
  const email = who.user?.profile?.email;
  if (email !== undefined && memory.waits.has(email)) {
    await run(new Date(Number(event.ts) * 1000), `${email} answered: ${event.text}`);
  }
}

function signedBySlack(request: IncomingMessage, body: string): boolean {
  // Slack's request signing: v0=HMAC-SHA256(secret, "v0:{timestamp}:{body}").
  const timestamp = String(request.headers["x-slack-request-timestamp"]);
  const signature = Buffer.from(String(request.headers["x-slack-signature"]));
  const expected = createHmac("sha256", process.env.AGENT_SLACK_SIGNING_SECRET!)
    .update(`v0:${timestamp}:${body}`)
    .digest("hex");
  const wanted = Buffer.from(`v0=${expected}`);
  return signature.length === wanted.length && timingSafeEqual(signature, wanted);
}

let queue: Promise<unknown> = Promise.resolve(); // a wake and a Slack event can arrive together; take them in turn
function inTurn<T>(work: () => Promise<T>): Promise<T> {
  const next = queue.then(work);
  queue = next.catch(() => undefined);
  return next;
}

function answer(response: ServerResponse, status: number, payload: object): void {
  response.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(payload));
}

createServer(async (request, response) => {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(chunk as Buffer);
  const body = Buffer.concat(chunks).toString("utf8");
  try {
    if (request.method === "POST" && request.url === "/wake") {
      await inTurn(() => remembering(() => wake(JSON.parse(body))));
      answer(response, 200, { ok: true });
    } else if (request.method === "POST" && request.url === "/slack/events" && signedBySlack(request, body)) {
      await inTurn(() => remembering(() => message(JSON.parse(body).event)));
      answer(response, 200, { ok: true });
    } else if (request.method === "GET" && request.url === "/report") {
      answer(response, 200, await inTurn(async () => (await recall(), report())));
    } else {
      answer(response, 404, { error: "no such endpoint, or a Slack event that is not signed" });
    }
  } catch (failed) {
    console.error(failed);
    answer(response, 500, { error: String(failed) });
  }
}).listen(Number(process.env.PORT ?? "8715"), "127.0.0.1", () => {
  console.log(`listening on ${process.env.PORT ?? "8715"}`);
});
