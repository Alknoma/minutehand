/* Every path of the JSON API the page reads, in one place (`tests/web` checks each exists), and one way to read
   them: a run's answers are kept per run, so moving between views reads each path once. */

export const API = {
  runs: "/api/runs",
  batches: "/api/batches",
  run: "/api/runs/{run}",
  timeline: "/api/runs/{run}/timeline",
  findings: "/api/runs/{run}/findings",
  scorecard: "/api/runs/{run}/scorecard",
  wakes: "/api/runs/{run}/wakes",
  obligations: "/api/runs/{run}/obligations",
  people: "/api/runs/{run}/people",
  modelTraffic: "/api/runs/{run}/model-traffic",
  callRows: "/api/runs/{run}/call-rows",
  dispatch: "/api/runs/{run}/dispatch",
  memory: "/api/runs/{run}/memory",
  stored: "/api/runs/{run}/stored",
  assessments: "/api/runs/{run}/assessments",
  event: "/api/runs/{run}/events/{seq}",
  call: "/api/runs/{run}/calls/{index}",
  modelCall: "/api/runs/{run}/model-calls/{span}",
  span: "/api/runs/{run}/spans/{span}",
  stepSpans: "/api/runs/{run}/steps/{step}/spans"
};

export function path(p, run, more) {
  let out = p.replace("{run}", encodeURIComponent(run));
  for (const k in more || {}) { out = out.replace("{" + k + "}", encodeURIComponent(more[k])); }
  return out;
}

export async function get(p) {
  const r = await fetch(p, { headers: { Accept: "application/json" } });
  if (!r.ok) {
    let said = "answered " + r.status;
    try { said = (await r.json()).error || said; } catch (e) { /* not JSON */ }
    throw new Error(said);
  }
  return r.json();
}

/** Answers kept for one run; dropped whole when another run is opened or a running one moves on. */
export class RunCache {
  constructor(run) { this.run = run; this.kept = new Map(); }
  read(name, more) {
    const p = path(API[name], this.run, more);
    if (!this.kept.has(p)) {
      const pending = get(p);
      this.kept.set(p, pending);
      pending.catch(() => this.kept.delete(p));
    }
    return this.kept.get(p);
  }
  forget() { this.kept.clear(); }
}
