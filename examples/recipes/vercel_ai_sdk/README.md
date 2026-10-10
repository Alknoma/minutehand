# Vercel AI SDK

`agent.ts` is one `generateText` call per wake, with three `tool`s and a stop condition (the AI SDK's tool loop),
behind three endpoints Minutehand calls, on Node's own `http` server. Node 22.18 or later runs the TypeScript as it
is; there is no build step. `../README.md` describes the agent and the contract all five recipes share.

## The lines that connect it

```ts
const memory: { work: string | null; waits: Map<string, Wait> } = { work: null, waits: new Map() };

remember_wait: tool({
  inputSchema: z.object({ email: z.string(), expected_by: z.iso.datetime({ offset: true }) }),
  execute: async ({ email, expected_by }) => {
    memory.waits.set(email, { expectedBy: new Date(expected_by), asks: (memory.waits.get(email)?.asks ?? 0) + 1 });
    return `waiting on ${email} until ${expected_by}`;
  },
}),

async function wake(request) {                               // POST /wake
  const now = new Date(request.now);
  if (request.reason === "start") {
    memory.work = WORK;
    await run(now, "Nobody has been asked yet.");            // generateText({ model, instructions, prompt, tools, stopWhen })
    return;
  }
  for (const [email, wait] of [...memory.waits]) {           // reason "due": whatever has passed its date
    if (wait.expectedBy <= now) await run(now, `No answer yet from ${email}, ... Follow-ups sent: ${wait.asks - 1}.`);
  }
}

function report() {                                          // GET /report, after every wake
  if (memory.work === null) return { status: "idle", next_wake: null };
  const dates = [...memory.waits.values()].map((w) => w.expectedBy.getTime());
  if (dates.length === 0) return { status: "done", next_wake: null };
  return { status: "idle", next_wake: iso(new Date(Math.min(...dates))) };
}
```

- **The wake handler** makes one `generateText` per wake, with `stopWhen: isStepCount(5)` so the model can call
  tools and see their results. A Slack event from a person the agent waits on is one more call (`message`). Wakes
  and events are taken one at a time (`inTurn`).
- **`next_wake`** is read from `memory`, recalled from the store, which the tools write. The AI SDK hands a failed tool's error back to the
  model; this agent throws instead, so a broken Slack call fails the wake where Minutehand sees it.
- **The Slack tool** uses `@slack/web-api`, which calls Node's own `fetch`. That ignores `HTTPS_PROXY`, so the agent
  takes Slack's base URL from `SLACK_API_URL`, and `agent.yaml` asks Minutehand to set it (`base_urls`) to its proxy's
  `http://127.0.0.1:<port>/_host/slack.com/api/`. Without that line, and without the variable, the client calls the
  real `slack.com`. The other way, which also works: start the agent as `node --use-env-proxy agent.ts` (a Node recent
  enough to have the flag; tried here on Node 25) and leave `base_urls` out; Node's `fetch` then follows Minutehand's `HTTPS_PROXY` and trusts its
  CA through `NODE_EXTRA_CA_CERTS`.
- **Slack's signature** on each pushed event is checked with `node:crypto` (`signedBySlack`), as `@slack/bolt`
  would.

## What it remembers

Its work and the waits are kept in a store (`minutehand-store.ts`): `recall()` reads them into `memory` before every
wake, Slack event and report, and `keep()` writes them back, in one batch, after every wake and event (`remembering`).
It is the TypeScript twin of Python's `minutehand.agent.store`, about eighty lines over the same wire: in production
a `Map` in this process (swap it for an adapter over your own database to keep it across restarts); under Minutehand
(`MINUTEHAND_ON` and `MINUTEHAND_AGENT_URL` set by `minutehand run`) a `POST` to the run's `/store` for every call,
retried a few times when the run cannot be reached and then thrown, never answered from the `Map`. So every run
starts from the scenario's memory, and a fork from any checkpoint starts from what the agent remembered there, with
nothing to snapshot or restore. The run is on `127.0.0.1`, in the `NO_PROXY` Minutehand hands out, so `fetch`
reaches it directly.

```ts
async function recall(): Promise<void> {
  const work = await store.get("work");
  memory.work = typeof work === "string" ? work : null;
  memory.waits = new Map();
  for (const [email, kept] of await store.list("", "waits")) {
    const wait = kept as { expected_by: string; asks: number };
    memory.waits.set(email, { expectedBy: new Date(wait.expected_by), asks: wait.asks });
  }
}
```

## The model

`createOpenAI({ baseURL: process.env.MODEL_BASE_URL ?? "http://127.0.0.1:8790/v1" })` and `openai.chat(...)`: the
provider's base URL, pointed at `../fake_model.py` over chat completions (`openai(...)` alone would use the Responses
API, which the fake does not speak). The call goes straight to `127.0.0.1`, which is in the `NO_PROXY` Minutehand
hands out, and Node's `fetch` does not use a proxy anyway. With a real model, drop `baseURL` and set
`OPENAI_API_KEY`; the calls go to `api.openai.com` directly, or through Minutehand's proxy with `--use-env-proxy`,
which tunnels them, or records them with `--record-model-calls`.

## Run both scenarios

From this folder:

```bash
npm ci                          # once: ai, @ai-sdk/openai, @slack/web-api, zod
python3 ../fake_model.py &      # once: the fake model on 127.0.0.1:8790

uv run minutehand run scenario_late.yaml --agent agent.yaml -- node agent.ts
# exits 0: Rosa answers after the follow-up; the agent tells Owen "... says: The lakeside hall, booked for the 14th."

uv run minutehand run scenario_silent.yaml --agent agent.yaml -- node agent.ts
# exits 0: one follow-up two days in, then Owen is told Rosa never answered; no follows_up_when_due finding
```

Minutehand installed on its own (`uv tool install .` from a checkout) runs the same without `uv run`.
