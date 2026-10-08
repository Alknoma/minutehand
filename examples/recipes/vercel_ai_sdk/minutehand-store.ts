// What the agent remembers, in TypeScript: the same store as Python's `minutehand_agent.store`, over the same wire.
//
// In production (MINUTEHAND_ON unset) it keeps everything in a Map in this process, which is gone when the process
// exits: swap `local` for an adapter over your own database to keep it. Under Minutehand (`minutehand run` sets
// MINUTEHAND_ON and MINUTEHAND_AGENT_URL) every call goes to the run instead, as JSON over HTTP:
//
//     POST {MINUTEHAND_AGENT_URL}/store  {"op": "get", "collection": c, "key": k}        -> {"found": bool, "value": v}
//                                        {"op": "list", "collection": c, "prefix": p}    -> {"items": [{"key", "value"}]}
//                                        {"op": "write", "writes": [{"op": "put", "collection", "key", "value"},
//                                                                   {"op": "delete", "collection", "key"}]}  -> {"seq": n}
//
// Each write is recorded in the run, and each read answered from it, so a fork starts from what the agent
// remembered at its checkpoint. A call that never reaches the run (connection refused or reset, or a 502, 503 or 504)
// is tried a few times over a few seconds and then throws; it never falls back to the Map. The run's address is on
// this machine and in NO_PROXY, so Node's fetch reaches it directly.

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Write =
  | { op: "put"; collection: string; key: string; value: Json }
  | { op: "delete"; collection: string; key: string };

const local = new Map<string, Json>(); // production: `${collection}/${key}` -> value

const RETRIED = new Set([502, 503, 504]);

function underMinutehand(): string | null {
  if (!process.env.MINUTEHAND_ON) return null;
  const url = process.env.MINUTEHAND_AGENT_URL;
  if (!url) throw new Error("MINUTEHAND_ON is set and MINUTEHAND_AGENT_URL is not: there is no run to talk to");
  return url.replace(/\/+$/, "");
}

async function call(base: string, body: object): Promise<any> {
  let wait = 100;
  let last = "";
  for (let attempt = 1; attempt <= 6; attempt++) {
    try {
      const answer = await fetch(`${base}/store`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (answer.ok) return await answer.json();
      const said = await answer.text();
      if (!RETRIED.has(answer.status)) throw new Error(`Minutehand refused the store call: ${answer.status} ${said}`);
      last = `${answer.status} ${said}`;
    } catch (failed) {
      if (failed instanceof Error && failed.message.startsWith("Minutehand refused")) throw failed;
      last = String(failed);
    }
    await new Promise((done) => setTimeout(done, wait));
    wait *= 2;
  }
  throw new Error(
    `MINUTEHAND_ON is set and Minutehand did not answer at ${base}/store (the last: ${last}); nothing was written, ` +
      "and the store never falls back to the agent's own memory while it is set",
  );
}

export async function get(key: string, collection = "default"): Promise<Json | undefined> {
  const base = underMinutehand();
  if (base === null) return local.get(`${collection}/${key}`);
  const found = await call(base, { op: "get", collection, key });
  return found.found ? found.value : undefined;
}

export async function list(prefix = "", collection = "default"): Promise<[string, Json][]> {
  const base = underMinutehand();
  if (base === null) {
    const start = `${collection}/`;
    return [...local.entries()]
      .filter(([id]) => id.startsWith(start + prefix))
      .map(([id, value]): [string, Json] => [id.slice(start.length), value])
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  }
  const listed = await call(base, { op: "list", collection, prefix });
  return listed.items.map((item: { key: string; value: Json }): [string, Json] => [item.key, item.value]);
}

/** Every write applied together, all or none. */
export async function write(writes: Write[]): Promise<void> {
  if (writes.length === 0) return;
  const base = underMinutehand();
  if (base === null) {
    for (const one of writes) {
      if (one.op === "put") local.set(`${one.collection}/${one.key}`, structuredClone(one.value));
      else local.delete(`${one.collection}/${one.key}`);
    }
    return;
  }
  await call(base, { op: "write", writes });
}
