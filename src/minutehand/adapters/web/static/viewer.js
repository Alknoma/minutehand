/* The run viewer. It reads the JSON API of `minutehand view` and draws one run:

   - the first screen: what was asked, the verdict in the API's own words and the findings behind it, how long
     and how much;
   - the timeline (vis-timeline): swim-lanes for the agent's steps, its model calls by model, what it waited on,
     each provider's changes and reads, and calls to hosts no fake answers; on the real clock, where the agent's
     work is spread out, or the simulated one, where waits fall due;
   - the detail panel, driven by whatever is selected anywhere on the page: a step (its messages, provider calls,
     model calls, findings and span waterfall), a model call (what it was asked and answered, what it wrote), a
     change in the world (the provider call behind it and the model call that wrote it), a wait, a finding (its
     evidence, selected on the timeline);
   - below: the model calls as charts per step and a sortable table, every message, outbound calls, and a fork's
     account of itself.

   Everything it loads comes from this server: the libraries are vendored under /static/vendor. */
(function () {
  "use strict";

  var API = {
    runs: "/api/runs",
    run: "/api/runs/{run}",
    wakes: "/api/runs/{run}/wakes",
    events: "/api/runs/{run}/events",
    obligations: "/api/runs/{run}/obligations",
    findings: "/api/runs/{run}/findings",
    scorecard: "/api/runs/{run}/scorecard",
    modelCalls: "/api/runs/{run}/model-calls",
    calls: "/api/runs/{run}/calls",
    messages: "/api/runs/{run}/messages",
    modelTraffic: "/api/runs/{run}/model-traffic",
    steps: "/api/runs/{run}/steps",
    stepSpans: "/api/runs/{run}/steps/{step}/spans",
    modelCall: "/api/runs/{run}/model-calls/{span}"
  };
  var CAPTURED_AS = {
    acknowledge: "acknowledged here, never sent",
    pass_through: "passed through to the real host",
    replay: "declared to replay",
    discovered: "passed through, undeclared (--capture-unknown)",
    store: "kept and read back as declared, never sent"
  };
  var ANSWERED_BY = {
    declaration: "answered as declared",
    real_host: "answered by the real host",
    recording: "REPLAYED from a recording",
    refusal: "refused: no recording held it"
  };
  var JOINED = {
    trace: "joined by its trace",
    content: "joined by content: the model answered this text verbatim",
    wake: "the nearest model call in the same step, not proven"
  };
  var KIND_LABEL = { fail: "Fail", review: "To review", informational: "Note" };
  var KIND_ORDER = { fail: 0, review: 1, informational: 2 };
  var VERDICT_CLASS = { passed: "ok", failed: "fail", unfinished: "review", tool_failed: "fail", not_judged: "review" };
  var VERDICT_WORD = { passed: "Passed", failed: "Failed", unfinished: "Not finished", tool_failed: "Not scored", not_judged: "Not judged" };
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var RUNNING_REFRESH = 2000, LIST_REFRESH = 5000;
  var OPEN_ALL = /(^|[?&])open(=|&|$)/.test(location.search.slice(1));

  var S = {
    list: [], run: null, v: null, timeline: null, items: null, groups: null, meta: {},
    sel: null, clock: null, hidden: {}, tab: "calls", sort: { key: "started", dir: 1 },
    timer: null, spans: {}, callCache: {}, compare: true, onlyStep: false
  };

  /* ---------- small things ---------- */

  function path(p, run, more) {
    var out = p.replace("{run}", encodeURIComponent(run));
    for (var k in more || {}) { out = out.replace("{" + k + "}", encodeURIComponent(more[k])); }
    return out;
  }
  function get(p) {
    return fetch(p, { headers: { Accept: "application/json" } }).then(function (r) {
      if (!r.ok) { return r.json().then(function (b) { throw new Error(b.error || ("answered " + r.status)); }); }
      return r.json();
    });
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function $(id) { return document.getElementById(id); }
  function words(name) { return String(name).replace(/_/g, " "); }
  function title(name) { return words(name).replace(/\b\w/g, function (c) { return c.toUpperCase(); }); }
  function ms(iso) { return Date.parse(iso); }
  function pad(n) { return String(n).padStart(2, "0"); }
  function when(iso) {
    var d = new Date(iso);
    return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }
  function clockOf(iso) {
    var d = new Date(iso);
    return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds());
  }
  function seconds(duration) {
    var m = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$/.exec(duration || "");
    if (!m) { return 0; }
    return (+(m[1] || 0)) * 86400 + (+(m[2] || 0)) * 3600 + (+(m[3] || 0)) * 60 + (+(m[4] || 0));
  }
  function span(secs) {
    if (secs < 90) { return Math.round(secs) + " seconds"; }
    if (secs < 5400) { return Math.round(secs / 60) + " minutes"; }
    if (secs < 172800) { var h = Math.round(secs / 3600); return h + (h === 1 ? " hour" : " hours"); }
    return tenths(secs / 86400) + " days";
  }
  // one decimal, a tie to the even digit, as the server's text rounds it
  function tenths(v) {
    var t = v * 10, f = Math.floor(t), n = t - f === 0.5 ? (f % 2 === 0 ? f : f + 1) : Math.round(t);
    return (n / 10).toFixed(1);
  }
  function short(secs) {
    if (secs < 1) { return Math.round(secs * 1000) + " ms"; }
    if (secs < 60) { return (secs < 10 ? secs.toFixed(1) : Math.round(secs)) + " s"; }
    if (secs < 3600) { return Math.floor(secs / 60) + " min " + pad(Math.round(secs % 60)) + " s"; }
    if (secs < 86400) { return Math.floor(secs / 3600) + " h " + pad(Math.round((secs % 3600) / 60)) + " min"; }
    return tenths(secs / 86400) + " days";
  }
  function count(n) {
    if (n >= 1e6) { return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M"; }
    if (n >= 1e4) { return Math.round(n / 1e3) + "k"; }
    if (n >= 1e3) { return (n / 1e3).toFixed(1) + "k"; }
    return String(n);
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
  function clip(s, n) { s = String(s); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function took(c) { return (ms(c.ended) - ms(c.started)) / 1000; }
  function tokens(c) { return (c.input_tokens || 0) + (c.output_tokens || 0); }

  // a part that is itself JSON (a structured prompt, a JSON answer) is laid out on lines, so it can be read
  function pretty(content) {
    var s = String(content), t = s.trim();
    if (t.charAt(0) !== "{" && t.charAt(0) !== "[") { return s; }
    try { return JSON.stringify(JSON.parse(t), null, 2); } catch (e) { return s; }
  }
  function said(text) {
    var parsed;
    try { parsed = JSON.parse(text); } catch (e) { return text; }
    if (!Array.isArray(parsed)) { return pretty(text); }
    return parsed.map(function (m) {
      if (m.type === "text" && m.content !== undefined) { return pretty(m.content); }
      if (m.event !== undefined) { return m.event + ":\n" + pretty(JSON.stringify(m.body)); }
      var parts = (m.parts || []).map(function (part) {
        if (part.type === "text") { return pretty(part.content); }
        if (part.type === "tool_call") { return "[calls " + part.name + "]" + (part.arguments ? "\n" + pretty(typeof part.arguments === "string" ? part.arguments : JSON.stringify(part.arguments)) : ""); }
        if (part.type === "tool_call_response") { return "[answer to a tool call]\n" + pretty(typeof part.response === "string" ? part.response : JSON.stringify(part.response)); }
        return "[" + part.type + "]";
      });
      return (m.role ? m.role + ":\n" : "") + parts.join("\n");
    }).join("\n\n");
  }

  /* ---------- the list of runs and the location ---------- */

  function where() {
    var h = new URLSearchParams(location.hash.slice(1));
    return { run: h.get("run"), sel: h.get("sel"), tab: h.get("tab"), clock: h.get("clock") };
  }
  function remember() {
    var h = new URLSearchParams();
    if (S.run) { h.set("run", S.run); }
    if (S.sel) { h.set("sel", S.sel); }
    if (S.tab !== "calls") { h.set("tab", S.tab); }
    if (S.clock) { h.set("clock", S.clock); }
    var next = "#" + h.toString();
    if (location.hash !== next) { history.replaceState(null, "", next); }
  }

  function runLabel(row) {
    if (row.case !== null && row.case !== undefined) { return row.case; }
    if (row.parent_run !== null) { return "  ↳ " + (row.changed || "Rerun"); }
    var roots = S.list.filter(function (r) { return r.parent_run === null && r.scenario === row.scenario; });
    var name = words(row.scenario);
    return roots.length > 1 ? name + ", run " + (roots.indexOf(row) + 1) : name;
  }
  function runStatus(row) {
    if (!row.finished) { return "running"; }
    var word = VERDICT_WORD[row.verdict] || "";
    if (row.failed) { word += ", " + plural(row.failed, "problem", "problems"); }
    if (row.to_review) { word += ", " + row.to_review + " to look at"; }
    return word;
  }

  function loadList() {
    return get(API.runs).then(function (body) {
      S.list = body.runs;
      var select = $("runs");
      var byParent = {};
      S.list.forEach(function (r) { (byParent[r.parent_run || ""] = byParent[r.parent_run || ""] || []).push(r); });
      var html = "";
      (function walk(parent, depth) {
        (byParent[parent] || []).forEach(function (r) {
          html += '<option value="' + esc(r.run_id) + '"' + (r.run_id === S.run ? " selected" : "") + ">" +
            esc((depth ? "  ".repeat(depth) + "↳ " + (r.changed || "Rerun") : runLabel(r)) + " — " + runStatus(r)) + "</option>";
          walk(r.run_id, depth + 1);
        });
      })("", 0);
      select.innerHTML = html || "<option>No runs yet. Play a scenario with minutehand run.</option>";
      if (!S.run && S.list.length) {
        var wanted = where().run;
        var known = S.list.filter(function (r) { return r.run_id === wanted; })[0];
        open((known || S.list[0]).run_id, where());
      }
    }).catch(function (e) {
      $("runs").innerHTML = "<option>Could not read the runs: " + esc(e.message) + "</option>";
    });
  }
  $("runs").addEventListener("change", function () { open($("runs").value, {}); });

  function open(run, at) {
    S.run = run; S.sel = at.sel || null; S.tab = at.tab || "calls"; S.clock = at.clock || null;
    S.spans = {}; S.callCache = {}; S.hidden = {}; S.onlyStep = false;
    if (S.timeline) { S.timeline.destroy(); S.timeline = null; }
    $("runs").value = run;
    load(run, true);
  }

  /* ---------- one run ---------- */

  function load(run, fresh) {
    clearTimeout(S.timer);
    var names = ["run", "wakes", "events", "obligations", "findings", "scorecard", "modelCalls", "calls", "messages", "modelTraffic", "steps"];
    Promise.all(names.map(function (n) { return get(path(API[n], run)); })).then(function (parts) {
      if (S.run !== run) { return; }
      var v = {
        run: parts[0], wakes: parts[1].wakes, events: parts[2].events, obligations: parts[3].obligations,
        findings: parts[4], scorecard: parts[5].scorecard, modelCalls: parts[6], calls: parts[7].calls,
        messages: parts[8].messages, traffic: parts[9], steps: parts[10].steps
      };
      var fork = v.run.fork;
      var parent = fork
        ? Promise.all([get(path(API.wakes, fork.parent_run)), get(path(API.events, fork.parent_run))]).then(function (more) {
          v.parent = { wakes: more[0].wakes, events: more[1].events };
        })
        : Promise.resolve();
      return parent.then(function () {
        if (S.run !== run) { return; }
        S.v = index(v);
        if (!S.clock) { S.clock = v.traffic.calls.length || !v.obligations.length ? "real" : "sim"; }
        draw(fresh);
        $("live").hidden = v.run.finished;
        if (!v.run.finished) { S.timer = setTimeout(function () { load(run, false); }, RUNNING_REFRESH); }
      });
    }).catch(function (e) {
      $("main").innerHTML = '<div class="empty">Could not read this run: ' + esc(e.message) + "</div>";
    });
  }

  // lookups every view needs, built once per load
  function index(v) {
    var people = { email: {}, key: {} };
    v.run.scenario.people.forEach(function (p) { people.email[p.email] = p.name; people.key[p.key] = p.name; });
    v.who = {
      email: function (e) { return people.email[e] || e; },
      key: function (k) { return people.key[k] || words(k); }
    };
    v.bySeq = {}; v.events.forEach(function (e) { v.bySeq[e.seq] = e; });
    v.lineBySeq = {}; v.messages.forEach(function (m) { v.lineBySeq[m.seq] = m; });
    v.wakeByIndex = {}; v.wakes.forEach(function (w) { v.wakeByIndex[w.index] = w; });
    v.stepBy = {}; v.steps.forEach(function (s) { v.stepBy[s.step] = s; });
    v.callBySpan = {}; v.traffic.calls.forEach(function (c) { v.callBySpan[c.span_id] = c; });
    // models in the order they first answered, each with its slot (three colours, the rest one "other")
    v.models = [];
    v.traffic.calls.forEach(function (c) { var m = c.model || "model not named"; if (v.models.indexOf(m) < 0) { v.models.push(m); } });
    v.slot = function (model) { var i = v.models.indexOf(model || "model not named"); return i >= 0 && i < 3 ? i + 1 : 0; };
    v.providers = [];
    v.events.forEach(function (e) { if (v.providers.indexOf(e.entity.provider) < 0) { v.providers.push(e.entity.provider); } });
    v.calls.forEach(function (c) { if (c.provider && v.providers.indexOf(c.provider) < 0) { v.providers.push(c.provider); } });
    // a model host's tunnels are summarised, one line per host, never listed call by call
    v.outbound = v.calls.filter(function (c) { return (c.exchange.captured !== null || c.provider === null) && !c.exchange.tunnelled; });
    // the simulated moment of each real one the log pairs, to place what has only a simulated time (a wait) on
    // the real clock, and the reverse
    v.pairs = v.events.map(function (e) { return [ms(e.sim_time), ms(e.wall_time)]; }).sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    v.stepWord = v.run.driven ? "step" : "wake";
    return v;
  }

  function simToReal(v, t) {
    var best = null;
    v.pairs.forEach(function (p) { if (p[0] <= t) { best = p; } });
    return best ? best[1] : (v.pairs.length ? v.pairs[0][1] : t);
  }

  function stepName(v, n) {
    if (n === 0) { return "Setup"; }
    if (v.wakeByIndex[n]) { return (v.run.driven ? "Step " : "Wake ") + n; }
    return v.run.driven ? "Outside any step" : "Wake " + n;
  }

  /* ---------- the verdict, and how long and how much ---------- */

  function verdictBox(v) {
    if (!v.run.finished) {
      return '<div class="verdict review"><b>Running</b><span>Reached ' + esc(when(v.run.reached)) + " on the simulated clock. This page follows it.</span></div>";
    }
    var verdict = v.findings.verdict;
    var unjudged = verdict.unjudged && verdict.unjudged.length
      ? "<ul>" + verdict.unjudged.map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("") + "</ul>" : "";
    return '<div class="verdict ' + VERDICT_CLASS[verdict.kind] + '"><b>' + esc(VERDICT_WORD[verdict.kind]) + "</b><span>" +
      esc(verdict.words) + unjudged + "</span></div>";
  }

  function tiles(v) {
    var card = v.scorecard, run = v.run, rec = run.record;
    var calls = v.traffic.calls;
    var tin = calls.reduce(function (a, c) { return a + (c.input_tokens || 0); }, 0);
    var tout = calls.reduce(function (a, c) { return a + (c.output_tokens || 0); }, 0);
    var modelSecs = calls.reduce(function (a, c) { return a + took(c); }, 0);
    var simSecs = (ms(run.reached) - ms(run.scenario.starts_at)) / 1000;
    var out = [];
    function tile(k, val, sub) { out.push('<div class="tile"><div class="k">' + esc(k) + '</div><div class="v">' + esc(val) + '</div><div class="s">' + esc(sub || " ") + "</div></div>"); }
    tile("Expectations met", card ? card.expectations_met + " of " + card.expectations_total : "—", card ? plural(card.follow_ups_made, "follow-up", "follow-ups") + " made" : "once finished");
    var idle = v.wakes.filter(function (w) { return w.world_changes === 0 && !w.commitments_changed; }).length;
    tile(run.driven ? "Steps" : "Wakes", String(v.wakes.length), idle ? idle + " changed nothing" : "each changed something");
    tile("Model calls", calls.length ? String(calls.length) : "none seen", calls.length ? short(modelSecs) + " answering" : (v.traffic.hosts.length ? "tunnelled, not opened" : "no telemetry"));
    tile("Tokens", calls.length ? count(tin + tout) : "—", calls.length ? count(tin) + " in · " + count(tout) + " out" : "");
    tile("Real time", rec ? short(rec.wall_seconds) : "running", span(simSecs) + " simulated");
    var sent = v.messages.filter(function (m) { return m.actor === "agent" && m.change === "sent"; }).length;
    tile("Messages sent", String(sent), card && (card.messages_edited || card.messages_deleted) ? card.messages_edited + " rewritten, " + card.messages_deleted + " deleted" : "none rewritten");
    return '<div class="tiles">' + out.join("") + "</div>";
  }

  function scoreLines(v) {
    var card = v.scorecard, lines = [];
    lines.push(card.expectations_met + " of " + card.expectations_total + " of the scenario's expectations were met.");
    lines.push(plural(card.waits_opened, "wait was", "waits were") + " opened" +
      (card.waits_open_at_end ? "; " + card.waits_open_at_end + " still open at the end." : "; none was left open."));
    lines.push("Follow-ups the agent made on a wait still open: " + card.follow_ups_made + ".");
    if (card.slowest_reaction) { lines.push("The longest it took to come back to a settled wait: " + span(seconds(card.slowest_reaction)) + "."); }
    if (v.run.driven) {
      var inferred = v.wakes.filter(function (w) { return w.inferred; }).length;
      lines.push(card.wakes
        ? "It took " + plural(card.wakes, "step", "steps") + (inferred ? " (inferred from the clock's moves)" : "") +
          (card.idle_wakes ? "; " + card.idle_wakes + " of those changed nothing." : ", and every step changed something.")
        : "No step was recorded: nobody marked one and the clock never moved forward.");
    } else {
      lines.push("It woke " + plural(card.wakes, "time", "times") + (card.idle_wakes ? "; " + card.idle_wakes + " of those changed nothing." : ", and every wake changed something."));
    }
    lines.push("It sent " + plural(card.messages_to_people, "message", "messages") + " to people" +
      (card.messages_edited || card.messages_deleted
        ? "; " + plural(card.messages_edited, "rewrite", "rewrites") + " of a message already sent, " + card.messages_deleted + " deleted."
        : "; none was rewritten or deleted after it was sent."));
    if (card.decisions_asked) {
      lines.push("It left " + plural(card.decisions_asked, "decision", "decisions") + " waiting on people in its own product: " +
        card.decisions_made + " decided, " + card.decisions_pending + " still pending at the end.");
    }
    return lines;
  }

  function sortedFindings(v) {
    return v.findings.findings.slice().sort(function (a, b) {
      return KIND_ORDER[a.finding.kind] - KIND_ORDER[b.finding.kind] || a.number - b.number;
    });
  }
  function findingTitle(x) { return x.pattern ? x.pattern.title : title(x.finding.check); }

  function findingsBox(v) {
    var f = sortedFindings(v), counts = { fail: 0, review: 0, informational: 0 };
    f.forEach(function (x) { counts[x.finding.kind] += 1; });
    var head = '<div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap"><h2>Why: the findings</h2><span class="note">' +
      (v.findings.finished ? [counts.fail ? counts.fail + " failed" : "", counts.review ? counts.review + " to review" : "", counts.informational ? counts.informational + " notes" : ""].filter(Boolean).join(" · ") || "none" : "checked when the run finishes") +
      "</span></div>";
    if (!v.findings.finished) { return head + '<p class="note">The checks run when the run finishes.</p>'; }
    if (!f.length) { return head + '<p class="note">No check found anything wrong.</p>'; }
    var rows = f.map(function (x) {
      return '<li><button type="button" class="frow" data-sel="finding:' + x.number + '" aria-pressed="' + (S.sel === "finding:" + x.number) + '">' +
        '<span class="chip ' + x.finding.kind + '">' + KIND_LABEL[x.finding.kind] + '</span><span class="t">' + esc(findingTitle(x)) + "</span>" +
        '<span class="m">' + esc(x.finding.message) + "</span></button></li>";
    }).join("");
    var blocked = v.findings.blocked.length
      ? '<p class="note">' + plural(v.findings.blocked.length, "check", "checks") + " could not read what they needed and did not run.</p>" : "";
    return head + "<ol>" + rows + "</ol>" + blocked;
  }

  /* ---------- the page ---------- */

  function draw(fresh) {
    var v = S.v, run = v.run, sc = run.scenario;
    var row = S.list.filter(function (r) { return r.run_id === run.run_id; })[0];
    var eyebrow = (row && row.case ? "case · " + plural(row.worlds.length, "world", "worlds") + " · " + v.providers.map(title).join(", ") : words(sc.name)) +
      " · simulated from " + when(sc.starts_at) + " UTC" + (row && row.parent_run !== null ? " · a fork" : "");
    var keepTab = S.tab;
    var html = '<section class="overview">' +
      '<div class="card ask"><div class="eyebrow">' + esc(eyebrow) + "</div><h1>" + esc(sc.goal) + "</h1>" + verdictBox(v) + tiles(v) +
      (v.scorecard ? '<details class="score"><summary>Every line of the scorecard</summary><ul>' + scoreLines(v).map(function (l) { return "<li>" + esc(l) + "</li>"; }).join("") + "</ul></details>" : "") +
      "</div>" +
      '<div class="card why" id="why">' + findingsBox(v) + "</div></section>";
    html += '<section class="work"><div class="card tl">' +
      '<div class="tools"><h2>Timeline</h2>' +
      '<span class="seg" role="group" aria-label="Clock"><button type="button" data-clock="real" aria-pressed="' + (S.clock === "real") + '">Real clock</button><button type="button" data-clock="sim" aria-pressed="' + (S.clock === "sim") + '">Simulated clock</button></span>' +
      '<span class="seg" role="group" aria-label="Zoom"><button type="button" data-zoom="fit">Fit</button><button type="button" data-zoom="in" aria-label="Zoom in">+</button><button type="button" data-zoom="out" aria-label="Zoom out">−</button></span>' +
      '<span class="lanes" id="lanes"></span></div>' +
      '<p class="clocknote" id="clocknote"></p>' +
      '<div id="timeline"></div><div class="legend" id="legend"></div></div>' +
      '<aside class="card detail" id="detail" aria-live="polite"></aside></section>';
    html += '<section class="card"><div class="tabs" role="tablist" id="tabs"></div><div class="pane" id="pane"></div></section>';
    $("main").innerHTML = html;
    S.tab = keepTab;
    wire();
    timeline(fresh);
    tabs();
    showSelection(false);
    if (OPEN_ALL) { document.querySelectorAll("main details").forEach(function (d) { d.open = true; }); }
  }

  function wire() {
    $("main").addEventListener("click", function (ev) {
      var t = ev.target.closest("[data-sel]");
      if (t) { ev.preventDefault(); select(t.getAttribute("data-sel"), true); return; }
      var c = ev.target.closest("[data-clock]");
      if (c) { S.clock = c.getAttribute("data-clock"); remember(); draw(true); return; }
      var z = ev.target.closest("[data-zoom]");
      if (z && S.timeline) {
        var how = z.getAttribute("data-zoom");
        if (how === "fit") { S.timeline.fit(); } else if (how === "in") { S.timeline.zoomIn(0.5); } else { S.timeline.zoomOut(0.5); }
        return;
      }
      var tab = ev.target.closest("[data-tab]");
      if (tab) { S.tab = tab.getAttribute("data-tab"); remember(); tabs(); }
    });
    $("main").addEventListener("keydown", function (ev) {
      if (ev.key !== "Enter" && ev.key !== " ") { return; }
      var t = ev.target.closest("tr[data-sel], li[data-sel]");
      if (t) { ev.preventDefault(); select(t.getAttribute("data-sel"), true); }
    });
  }

  /* ---------- the timeline ---------- */

  function describe(v, ev) {
    var a = ev.after, actor = ev.actor === "agent" ? "The agent" : ev.actor === "person" ? "Someone" : "The scenario";
    var verb = { create: "created", update: "changed", "delete": "deleted", read: "read", search: "searched" }[ev.operation] || ev.operation;
    var line = v.lineBySeq[ev.seq];
    if (line && line.words) { return { head: (line.actor === "agent" ? "The agent " : "") + line.words.split(": ")[0], body: line.text, short: line.words }; }
    if (line && line.change !== "sent") {
      var whom = line.to.length ? line.to.join(", ") : "a channel";
      return {
        head: actor + (line.change === "edited" ? " rewrote the message to " : " deleted the message to ") + whom,
        body: line.change === "edited" ? "from “" + line.before + "” to “" + line.text + "”" : line.text,
        short: (line.change === "edited" ? "rewrote → " : "deleted → ") + whom
      };
    }
    if (a && a.kind === "message") {
      var to = a.recipient_emails.map(v.who.email).join(", ");
      if (ev.actor === "agent") { return { head: "Message to " + (to || "a channel"), body: a.text, short: "→ " + (to || "channel") + ": " + a.text }; }
      return { head: (to ? "Message to " + to : "Message") + (ev.actor === "person" ? ", from a person" : ", set up by the scenario"), body: a.text, short: a.text };
    }
    if (a && a.kind === "ticket") {
      var holder = a.assignee_email ? " for " + v.who.email(a.assignee_email) : "";
      return { head: actor + " " + verb + " a ticket" + holder, body: "“" + a.title + "”, " + a.state, short: verb + " ticket “" + a.title + "”" };
    }
    if (a && a.kind === "document") { return { head: actor + " " + verb + " a document", body: "“" + a.title + "”", short: verb + " “" + a.title + "”" }; }
    if (a && a.kind === "record") { return { head: actor + " " + verb + " a " + a.resource + " record", body: a.text, short: verb + " " + a.resource }; }
    if (a && a.kind === "interaction") {
      return { head: v.who.key(a.person) + (a.interaction === "press" ? " pressed “" : " submitted “") + a.label + "”", body: a.form.join("\n"), short: (a.interaction === "press" ? "pressed “" : "submitted “") + a.label + "”" };
    }
    return { head: actor + " " + verb + " a " + words(ev.entity.kind) + " in " + title(ev.entity.provider), body: "", short: verb + " " + words(ev.entity.kind) };
  }

  function obligationLabel(v, o) {
    if (o.kind === "answer_from_person") { return "An answer from " + v.who.key(o.person); }
    if (o.kind === "work_with_person") { return "Work handed to " + v.who.key(o.person); }
    return "The scenario's deadline";
  }

  function isChange(e) { return e.operation !== "read" && e.operation !== "search"; }

  // every lane and mark, for the clock in use; S.meta maps an item's id to what it stands for
  function build(v, clock) {
    var real = clock === "real", groups = [], items = [], meta = {};
    function at(e) { return real ? ms(e.wall_time) : ms(e.sim_time); }
    function add(item, what) { items.push(item); meta[item.id] = what; }
    var order = 0;
    function group(id, content, more) { var g = { id: id, content: content, order: order++ }; for (var k in more || {}) { g[k] = more[k]; } groups.push(g); return g; }

    // the agent's steps
    group("steps", v.run.driven ? "Agent steps" : "Agent wakes");
    var stepNums = {};
    v.steps.forEach(function (s) { stepNums[s.step] = true; });
    v.wakes.forEach(function (w) { stepNums[w.index] = true; });
    Object.keys(stepNums).map(Number).sort(function (a, b) { return a - b; }).forEach(function (n) {
      var w = v.wakeByIndex[n], s = v.stepBy[n];
      if (n === 0 && !w) { return; } // setup is drawn as each provider's seeding
      var idle = w ? w.world_changes === 0 && !w.commitments_changed : false;
      var cls = "step" + (!w ? " outside" : idle ? " idle" : "");
      var label = stepName(v, n);
      var tip = label + (w ? (idle ? ", changed nothing" : ", " + plural(w.world_changes, "change", "changes")) : "") + (w && w.reason ? "\n" + w.reason : "") + (w ? "\nSimulated " + when(w.sim_time) : "") +
        (s ? "\nReal " + clockOf(s.began) + "–" + clockOf(s.ended) + " · " + plural(s.model_calls, "model call", "model calls") : "");
      if (real && s) {
        var a = ms(s.began), b = Math.max(ms(s.ended), a + 1000);
        add({ id: "step:" + n, group: "steps", start: a, end: b, type: "range", content: esc(label), className: cls, title: esc(tip) }, { kind: "step", n: n });
      } else if (w) {
        add({ id: "step:" + n, group: "steps", start: real ? simToReal(v, ms(w.sim_time)) : ms(w.sim_time), type: "box", content: esc(label), className: cls, title: esc(tip) }, { kind: "step", n: n });
      }
    });

    // the model calls, a row per model
    if (v.traffic.calls.length) {
      var nested = v.models.map(function (m, i) { return "model:" + i; });
      group("models", "Model calls", { nestedGroups: nested, showNested: true });
      v.models.forEach(function (m, i) {
        group("model:" + i, '<span class="sw" style="background:var(--' + (i < 3 ? "s" + (i + 1) : "other") + ')"></span>' + esc(m), { treeLevel: 2 });
      });
      if (real) {
        v.traffic.calls.forEach(function (c) {
          var i = v.models.indexOf(c.model || "model not named"), a = ms(c.started), b = Math.max(ms(c.ended), a + 50);
          var wrote = c.wrote.length ? "\nWrote " + plural(c.wrote.length, "message", "messages") : "";
          add({
            id: "call:" + c.span_id, group: "model:" + i, start: a, end: b, type: "range", content: c.wrote.length ? "wrote" : "",
            className: "mc m" + v.slot(c.model) + (c.wrote.length ? " wrote" : ""),
            title: esc((c.model || "model not named") + " · " + stepName(v, c.step) + "\n" + clockOf(c.started) + " real, took " + short(took(c)) +
              "\n" + (c.input_tokens === null ? "?" : c.input_tokens) + " tokens in, " + (c.output_tokens === null ? "?" : c.output_tokens) + " out" + wrote)
          }, { kind: "call", span: c.span_id });
        });
      } else {
        // on the simulated clock a step is an instant: its calls are counted there, one mark per model
        var per = {};
        v.traffic.calls.forEach(function (c) {
          var k = c.step + "|" + v.models.indexOf(c.model || "model not named");
          (per[k] = per[k] || []).push(c);
        });
        Object.keys(per).forEach(function (k) {
          var calls = per[k], n = calls[0].step, i = v.models.indexOf(calls[0].model || "model not named");
          var t = stepSimTime(v, n);
          add({ id: "calls:" + k, group: "model:" + i, start: t, type: "box", content: esc(plural(calls.length, "call", "calls")),
            className: "mc agg", title: esc(stepName(v, n) + ": " + plural(calls.length, "call", "calls") + " to " + (calls[0].model || "a model")) }, { kind: "step", n: n });
        });
      }
    }

    // what the world was waiting on
    if (v.obligations.length) {
      group("waits", "Waiting on people");
      v.obligations.forEach(function (w) {
        var o = w.obligation, open = !o.settled_at;
        var a = ms(o.opened_at), b = open ? ms(v.run.reached) : ms(o.settled_at);
        if (real) { a = simToReal(v, a); b = simToReal(v, b); }
        add({ id: "wait:" + o.key, group: "waits", start: a, end: Math.max(b, a + (real ? 1000 : 60000)), type: "range",
          content: esc(obligationLabel(v, o)), className: "wait" + (open ? " open" : ""), title: esc(obligationLabel(v, o) + (open ? ", still open" : ", settled " + when(o.settled_at))) }, { kind: "wait", key: o.key });
      });
    }

    // each provider: what was seeded at setup (one mark), then every change and read
    v.providers.forEach(function (p) {
      group("p:" + p, esc(title(p)));
      var seeded = v.events.filter(function (e) { return e.entity.provider === p && e.wake === 0 && e.actor === "scenario"; });
      if (seeded.length) {
        var kinds = {};
        seeded.forEach(function (e) { var k = words(e.after ? e.after.kind : e.entity.kind); kinds[k] = (kinds[k] || 0) + 1; });
        add({ id: "seed:" + p, group: "p:" + p, start: Math.min.apply(null, seeded.map(at)), type: "box",
          content: esc("Set up: " + plural(seeded.length, "thing", "things")), className: "ev scenario",
          title: esc("Set up by the scenario: " + Object.keys(kinds).map(function (k) { return kinds[k] + " " + k; }).join(", ")) }, { kind: "seed", provider: p });
      }
      v.events.forEach(function (e) {
        if (e.entity.provider !== p || (e.wake === 0 && e.actor === "scenario")) { return; }
        var d = describe(v, e), read = !isChange(e);
        var kind = e.after ? e.after.kind : e.entity.kind;
        add({
          id: "ev:" + e.seq, group: "p:" + p, start: at(e), type: read ? "point" : "box",
          content: esc(clip(read ? d.short : d.short, read ? 28 : 46)),
          className: "ev " + e.actor + (read ? " read" : "") + (kind === "message" && e.operation === "create" ? " message" : "") + (e.operation === "delete" ? " deleted" : ""),
          title: esc(d.head + (d.body ? "\n“" + clip(d.body, 260) + "”" : "") + "\n" + (e.wake ? stepName(v, e.wake) : "Setup") + " · " + when(e.sim_time) + " simulated")
        }, { kind: "event", seq: e.seq });
      });
    });

    // calls to hosts no fake answers
    if (v.outbound.length) {
      group("outbound", "Outbound calls");
      v.outbound.forEach(function (c, i) {
        var first = v.bySeq[c.first_seq], t = real ? (first ? ms(first.wall_time) : simToReal(v, ms(c.sim_time))) : ms(c.sim_time);
        var e = c.exchange, refused = (e.captured === null && !e.tunnelled) || (e.captured !== null && e.captured.answered_by === "refusal");
        add({ id: "out:" + i, group: "outbound", start: t, type: "box", content: esc(e.method + " " + e.host),
          className: "outbound" + (refused ? " refused" : ""), title: esc(e.method + " " + e.host + e.path.split("?")[0] + "\n" + outboundHow(c)) }, { kind: "outbound", i: i });
      });
    }

    // a fork: the parent's record it shares, and what the parent did after the split
    var fork = v.run.fork;
    if (fork && !real) {
      add({ id: "shared", start: ms(v.run.scenario.starts_at), end: ms(fork.at), type: "background", className: "shared", content: "" }, { kind: "none" });
      if (S.compare && v.parent) {
        group("parent", "The parent, after the split");
        v.parent.wakes.filter(function (w) { return w.index > fork.after_wake; }).forEach(function (w) {
          add({ id: "pw:" + w.index, group: "parent", start: ms(w.sim_time), type: "box", content: esc("Its wake " + w.index), className: "parent",
            title: esc("The parent's wake " + w.index + ": changed " + plural(w.world_changes, "thing", "things")) }, { kind: "none" });
        });
        v.parent.events.filter(function (e) { return e.seq > fork.at_seq && isChange(e); }).forEach(function (e) {
          var d = describe(v, e);
          add({ id: "pe:" + e.seq, group: "parent", start: ms(e.sim_time), type: "box", content: esc(clip(d.short, 40)), className: "parent",
            title: esc("In the parent: " + d.head) }, { kind: "none" });
        });
      }
    }
    return { groups: groups, items: items, meta: meta };
  }

  function stepSimTime(v, n) {
    if (v.wakeByIndex[n]) { return ms(v.wakeByIndex[n].sim_time); }
    var e = v.events.filter(function (x) { return x.wake === n; })[0];
    return e ? ms(e.sim_time) : ms(v.run.scenario.starts_at);
  }

  function timeline(fresh) {
    var v = S.v, built = build(v, S.clock);
    var keep = !fresh && S.timeline ? S.timeline.getWindow() : null;
    if (S.timeline) { S.timeline.destroy(); }
    S.meta = built.meta;
    S.items = new vis.DataSet(built.items);
    S.groups = new vis.DataSet(built.groups.map(function (g) { g.visible = !S.hidden[g.id]; return g; }));
    var narrow = window.innerWidth < 600;
    S.timeline = new vis.Timeline($("timeline"), S.items, S.groups, {
      stack: true,
      orientation: { axis: "top", item: "top" },
      zoomKey: "ctrlKey",
      horizontalScroll: false,
      verticalScroll: true,
      maxHeight: narrow ? 420 : 470,
      minHeight: 200,
      margin: { item: { horizontal: 2, vertical: 2 }, axis: 4 },
      groupOrder: "order",
      multiselect: true,
      showCurrentTime: false,
      zoomMin: 200,
      tooltip: { followMouse: true, overflowMethod: "cap", delay: 120 },
      groupHeightMode: "auto",
      moment: function (date) { return vis.moment(date).utc(); },
      xss: { disabled: false, filterOptions: { whiteList: { span: ["class", "style"] } } }
    });
    var run = v.run;
    if (S.clock === "sim") {
      if (run.scenario.deadline_after) {
        var dl = ms(run.scenario.starts_at) + seconds(run.scenario.deadline_after) * 1000;
        S.timeline.addCustomTime(dl, "deadline"); S.timeline.setCustomTimeMarker("Deadline", "deadline");
        S.timeline.setCustomTimeTitle("The scenario's deadline", "deadline");
      }
      if (run.fork) { S.timeline.addCustomTime(ms(run.fork.at), "split"); S.timeline.setCustomTimeMarker("Split", "split"); }
    }
    var end = S.clock === "sim" ? ms(run.reached) : (run.record ? Math.max.apply(null, v.events.map(function (e) { return ms(e.wall_time); }).concat(v.traffic.calls.map(function (c) { return ms(c.ended); }))) : null);
    if (end) {
      S.timeline.addCustomTime(end, "end"); S.timeline.setCustomTimeMarker(run.finished ? "Ended" : "Now", "end");
    }
    if (keep) { S.timeline.setWindow(keep.start, keep.end, { animation: false }); }
    S.timeline.on("select", function (p) {
      if (!p.items.length) { return; }
      var id = p.items[p.items.length - 1], m = S.meta[id];
      if (!m || m.kind === "none") { return; }
      select(selOf(m), false);
    });
    lanes(built.groups);
    legend();
    $("clocknote").textContent = S.clock === "real"
      ? "Real time (UTC), where the agent's work is spread out: steps span their first to last recorded act, and each model call is drawn from when it was asked to when it answered. What has only a simulated time, a wait, sits where the run reached that moment. Drag to pan; ctrl or pinch to zoom."
      : "Simulated time (UTC): a step is an instant, and the clock jumps between them; model calls are counted at their step. Switch to the real clock to see them spread out. Drag to pan; ctrl or pinch to zoom.";
  }

  function selOf(m) {
    if (m.kind === "step") { return "step:" + m.n; }
    if (m.kind === "call") { return "call:" + m.span; }
    if (m.kind === "event") { return "event:" + m.seq; }
    if (m.kind === "wait") { return "wait:" + m.key; }
    if (m.kind === "seed") { return "seed:" + m.provider; }
    if (m.kind === "outbound") { return "out:" + m.i; }
    return null;
  }

  function lanes(groups) {
    var top = groups.filter(function (g) { return g.id.indexOf("model:") !== 0; });
    $("lanes").innerHTML = top.map(function (g) {
      var text = g.content.replace(/<[^>]*>/g, "");
      return '<label><input type="checkbox" data-lane="' + esc(g.id) + '"' + (S.hidden[g.id] ? "" : " checked") + ">" + esc(text.replace(/&amp;/g, "&")) + "</label>";
    }).join("");
    $("lanes").onchange = function (ev) {
      var id = ev.target.getAttribute("data-lane");
      S.hidden[id] = !ev.target.checked;
      var ids = [id];
      if (id === "models") { S.v.models.forEach(function (m, i) { ids.push("model:" + i); }); }
      S.groups.update(ids.map(function (g) { return { id: g, visible: !S.hidden[id] }; }));
    };
  }

  function legend() {
    var parts = [
      '<span><i style="background:var(--agent)"></i>' + (S.v.run.driven ? "A step" : "A wake") + ' that changed something</span>',
      '<span><i style="border:1px dashed var(--agent)"></i>One that changed nothing</span>'
    ];
    S.v.models.slice(0, 3).forEach(function (m, i) { parts.push('<span><i style="background:var(--s' + (i + 1) + ')"></i>' + esc(m) + "</span>"); });
    if (S.v.models.length > 3) { parts.push('<span><i style="background:var(--other)"></i>Other models</span>'); }
    if (S.v.traffic.calls.length) { parts.push('<span><i style="border:2px solid var(--ink)"></i>A call that wrote a message</span>'); }
    parts.push('<span><i style="border:2px solid var(--agent)"></i>By the agent</span>', '<span><i style="border:2px solid var(--person)"></i>By a person</span>', '<span><i style="border:2px solid var(--scenario)"></i>By the scenario</span>');
    if (S.v.obligations.length) { parts.push('<span><i style="background:var(--wait)"></i>Waiting, asked to settled</span>', '<span><i style="background:var(--fail);height:4px"></i>Overdue</span>'); }
    $("legend").innerHTML = parts.join("");
  }

  /* ---------- selection: one thing at a time, from anywhere ---------- */

  function select(sel, fromPage) {
    S.sel = sel;
    remember();
    showSelection(fromPage);
  }

  // the marks on the timeline that stand for a selection
  function marksOf(sel) {
    if (!sel) { return []; }
    var kind = sel.split(":")[0], rest = sel.slice(kind.length + 1), v = S.v;
    if (kind === "finding") {
      var x = v.findings.findings.filter(function (f) { return String(f.number) === rest; })[0];
      if (!x) { return []; }
      var ids = x.finding.evidence.map(function (s) { return "ev:" + s; });
      if (x.finding.wake !== null) { ids.push("step:" + x.finding.wake); }
      // a cited message's writer is part of the evidence on the real clock
      x.finding.evidence.forEach(function (s) { var l = v.lineBySeq[s]; if (l && l.written_by) { ids.push("call:" + l.written_by.span_id); } });
      return ids.filter(function (id) { return S.items.get(id); });
    }
    var id = { step: "step:", call: "call:", event: "ev:", wait: "wait:", seed: "seed:", out: "out:" }[kind];
    return id && S.items.get(id + rest) ? [id + rest] : [];
  }

  function showSelection(fromPage) {
    var ids = marksOf(S.sel);
    if (S.timeline) {
      S.timeline.setSelection(ids);
      if (fromPage && ids.length) { reveal(ids); }
    }
    document.querySelectorAll(".frow").forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-sel") === S.sel)); });
    document.querySelectorAll("table.calls tbody tr").forEach(function (r) { r.classList.toggle("on", r.getAttribute("data-sel") === S.sel); });
    detail();
    if (S.tab === "calls" && S.onlyStep) { tabs(); }
  }

  // move the window to the selected marks, zooming only as far as they need
  function reveal(ids) {
    var lo = Infinity, hi = -Infinity;
    ids.forEach(function (id) {
      var it = S.items.get(id);
      if (!it) { return; }
      lo = Math.min(lo, +new Date(it.start)); hi = Math.max(hi, +new Date(it.end || it.start));
    });
    if (lo === Infinity) { return; }
    var win = S.timeline.getWindow(), width = win.end - win.start;
    var need = hi - lo, least = S.clock === "real" ? 20000 : 600000;
    if (need * 1.3 > width || width > Math.max(need * 12, least * 6)) { width = Math.max(need * 1.4, least); }
    var mid = (lo + hi) / 2;
    S.timeline.setWindow(mid - width / 2, mid + width / 2, { animation: { duration: 300, easingFunction: "easeInOutQuad" } });
  }

  /* ---------- the detail panel ---------- */

  function link(sel, text) { return '<button type="button" class="link" data-sel="' + esc(sel) + '">' + esc(text) + "</button>"; }

  function detail() {
    var box = $("detail");
    if (!box) { return; }
    var sel = S.sel, kind = sel ? sel.split(":")[0] : null, rest = sel ? sel.slice(kind.length + 1) : null;
    var html;
    if (kind === "step") { html = stepDetail(+rest); }
    else if (kind === "call") { html = callDetail(rest); }
    else if (kind === "event") { html = eventDetail(+rest); }
    else if (kind === "wait") { html = waitDetail(rest); }
    else if (kind === "finding") { html = findingDetail(+rest); }
    else if (kind === "seed") { html = seedDetail(rest); }
    else if (kind === "out") { html = outboundDetail(+rest); }
    else { html = startDetail(); }
    box.innerHTML = html;
    if (OPEN_ALL) { box.querySelectorAll("details").forEach(function (d) { d.open = true; }); }
    if (kind === "step") { waterfall(+rest, null); }
    if (kind === "call" && S.v.callBySpan[rest]) { waterfall(S.v.callBySpan[rest].step, rest); }
  }

  function startDetail() {
    var v = S.v;
    var rows = Object.keys(v.stepBy).map(Number).concat(v.wakes.map(function (w) { return w.index; }))
      .filter(function (n, i, all) { return all.indexOf(n) === i && n !== 0; }).sort(function (a, b) { return a - b; });
    return "<h2>Select anything to read it here</h2>" +
      '<p class="note">A step, a model call, a message or change, a wait, or a finding: its detail opens here, and links follow it to the model call that wrote it, the provider calls around it and the spans of its step.</p>' +
      "<section><h3>" + (v.run.driven ? "Steps" : "Wakes") + '</h3><ul class="steps">' + rows.map(function (n) {
        var w = v.wakeByIndex[n], s = v.stepBy[n];
        var what = w ? (w.world_changes ? plural(w.world_changes, "change", "changes") : "changed nothing") : "";
        var mc = s ? plural(s.model_calls, "model call", "model calls") : "";
        return "<li>" + link("step:" + n, stepName(v, n)) + ' <span class="note">' + esc([w ? when(w.sim_time) : "", what, mc].filter(Boolean).join(" · ")) + "</span>" +
          (w && w.reason ? '<div class="reason">' + esc(w.reason) + "</div>" : "") + "</li>";
      }).join("") + "</ul></section>";
  }

  function crumbs(parts) {
    return '<div class="crumbs">' + parts.map(function (p) { return p[0] ? link(p[0], p[1]) : esc(p[1]); }).join("<span>›</span>") + "</div>";
  }

  function stepDetail(n) {
    var v = S.v, w = v.wakeByIndex[n], s = v.stepBy[n];
    var html = crumbs([[null, "Run"], [null, stepName(v, n)]]) + "<h2>" + esc(stepName(v, n)) + (w && w.reason ? ": " + esc(w.reason) : "") + "</h2><dl>";
    if (w) { html += "<dt>Simulated</dt><dd>" + esc(when(w.sim_time)) + (w.inferred ? ", inferred from the clock moving" : "") + "</dd>"; }
    if (s) { html += "<dt>Real</dt><dd>" + esc(clockOf(s.began) + " to " + clockOf(s.ended) + " (" + short((ms(s.ended) - ms(s.began)) / 1000) + ")") + "</dd>"; }
    if (w) { html += "<dt>World</dt><dd>" + (w.world_changes ? "changed " + plural(w.world_changes, "thing", "things") : "changed nothing") + (w.commitments_changed ? "; its commitments changed" : "") + "</dd>"; }
    var calls = v.traffic.calls.filter(function (c) { return c.step === n; });
    if (calls.length) {
      var tin = calls.reduce(function (a, c) { return a + (c.input_tokens || 0); }, 0), tout = calls.reduce(function (a, c) { return a + (c.output_tokens || 0); }, 0);
      html += "<dt>Model</dt><dd>" + plural(calls.length, "call", "calls") + ", " + count(tin) + " tokens in, " + count(tout) + " out, " + short(calls.reduce(function (a, c) { return a + took(c); }, 0)) + " answering</dd>";
    }
    html += "</dl>";
    var found = v.findings.findings.filter(function (x) {
      return x.finding.wake === n || x.finding.evidence.some(function (seq) { return v.bySeq[seq] && v.bySeq[seq].wake === n; });
    });
    if (found.length) {
      html += '<section><h3>Findings</h3><ul class="rows">' + found.map(function (x) {
        return '<li><span class="chip ' + x.finding.kind + '">' + KIND_LABEL[x.finding.kind] + '</span><span class="x">' + link("finding:" + x.number, findingTitle(x)) + '</span><span></span></li>';
      }).join("") + "</ul></section>";
    }
    var lines = v.messages.filter(function (m) { return m.wake === n; });
    if (lines.length) {
      html += "<section><h3>What was said</h3>" + lines.map(function (m) {
        return '<div class="quote ' + esc(m.actor) + '">' + link("event:" + m.seq, messageWords(m).split(": ")[0]) + "\n" + esc(m.words ? m.text : m.text) +
          (m.written_by ? '\n<span class="note">Written by ' + link("call:" + m.written_by.span_id, (m.written_by.model || "a model call")) + ", " + esc(JOINED[m.written_by.joined_by]) + "</span>" : "") + "</div>";
      }).join("") + "</section>";
    }
    var changes = v.events.filter(function (e) { return e.wake === n && !v.lineBySeq[e.seq]; });
    if (changes.length) {
      html += '<section><h3>Changes and reads</h3><ul class="rows">' + changes.map(function (e) {
        var d = describe(v, e);
        return '<li><span class="w">' + esc(title(e.entity.provider)) + '</span><span class="x">' + link("event:" + e.seq, d.head) + '</span><span class="w">' + esc(clockOf(e.wall_time)) + "</span></li>";
      }).join("") + "</ul></section>";
    }
    var provider = v.calls.filter(function (c) { return c.wake === n && c.provider !== null; });
    if (provider.length) {
      html += "<section><h3>Provider calls</h3>" + provider.map(function (c) { return exchangeBox(c.exchange, title(c.provider)); }).join("") + "</section>";
    }
    if (calls.length) {
      html += '<section><h3>Model calls</h3><ul class="rows">' + calls.map(function (c) {
        return '<li><span class="w">' + esc(clockOf(c.started)) + '</span><span class="x"><span class="sw" style="background:var(--' + (v.slot(c.model) ? "s" + v.slot(c.model) : "other") + ')"></span>' +
          link("call:" + c.span_id, (c.model || "model not named") + (c.wrote.length ? ", wrote a message" : "")) + '</span><span class="w">' + esc(short(took(c)) + " · " + count(tokens(c)) + " tok") + "</span></li>";
      }).join("") + "</ul></section>";
    }
    html += '<section><h3>Spans of this ' + v.stepWord + '</h3><div id="wf"><p class="note">Reading the spans…</p></div></section>';
    return html;
  }

  function messageWords(m) {
    if (m.words) { return (m.actor === "agent" ? "→ " : "") + m.words; }
    var to = m.to.length ? m.to.join(", ") : (m.actor === "agent" ? "nobody in the scenario" : "the agent");
    var who = m.actor === "agent" ? "" : (m.actor === "person" ? "a person" : "the scenario");
    if (m.change === "edited") { return (who ? who + " rewrote" : "rewrote") + " the message to " + to + ": from “" + m.before + "” to “" + m.text + "”"; }
    if (m.change === "deleted") { return (who ? who + " deleted" : "deleted") + " the message to " + to + ": “" + m.text + "”"; }
    return (who ? who + " → " : "→ ") + to + (m.thread ? " (in a thread)" : "") + ": " + m.text;
  }

  function body(text, kept) {
    if (kept && kept.kept === "binary") { return "(" + kept.size + " bytes of " + (kept.content_type || "unknown type") + ", not kept)"; }
    if (text === null || text === undefined || text === "") { return "(empty)"; }
    return pretty(text) + (kept && kept.kept === "truncated" ? "\n… cut at the declared size; " + kept.size + " bytes in all" : "");
  }

  function exchangeBox(e, who, open) {
    var c = e.captured;
    var head = e.method + " " + (e.tunnelled ? e.path : e.host + e.path.split("?")[0]) + " · " + e.status;
    var inner = "";
    if (e.tunnelled) {
      inner = '<p class="note">Relayed on a tunnel and never opened: ' + e.tunnelled.bytes_sent + " bytes sent, " + e.tunnelled.bytes_received + " received.</p>";
    } else {
      inner = "<div>Request</div><pre>" + esc(body(e.request_body, c ? c.request : null)) + "</pre><div>Answer</div><pre>" + esc(body(e.response_body, c ? c.response : null)) + "</pre>";
    }
    return '<details class="box"' + (open ? " open" : "") + "><summary>" + esc((who ? who + ": " : "") + head) + "</summary>" + inner + "</details>";
  }

  function callDetail(spanId) {
    var v = S.v, c = v.callBySpan[spanId];
    if (!c) { return "<h2>A model call</h2><p class=\"note\">This run holds no span of that model call.</p>"; }
    var cached = S.callCache[spanId];
    if (!cached) {
      get(path(API.modelCall, S.run, { span: spanId })).then(function (body) {
        S.callCache[spanId] = body;
        if (S.sel === "call:" + spanId) { detail(); }
      }).catch(function (e) { S.callCache[spanId] = { error: e.message }; if (S.sel === "call:" + spanId) { detail(); } });
    }
    var html = crumbs([[null, "Run"], ["step:" + c.step, stepName(v, c.step)], [null, "Model call"]]) +
      "<h2>" + esc(c.model || "Model not named") + ", " + esc(clockOf(c.started)) + " real</h2><dl>" +
      "<dt>In</dt><dd>" + link("step:" + c.step, stepName(v, c.step)) + "</dd>" +
      "<dt>Took</dt><dd>" + esc(short(took(c))) + "</dd>" +
      "<dt>Tokens</dt><dd>" + esc((c.input_tokens === null ? "?" : c.input_tokens) + " in, " + (c.output_tokens === null ? "?" : c.output_tokens) + " out") + "</dd>";
    var rank = v.traffic.calls.slice().sort(function (a, b) { return tokens(b) - tokens(a); }).indexOf(c) + 1;
    html += "<dt>Size</dt><dd>" + esc("the " + ordinal(rank) + " largest of " + v.traffic.calls.length + " by tokens") + "</dd></dl>";
    if (c.wrote.length) {
      html += "<section><h3>It wrote</h3>" + c.wrote.map(function (seq) {
        var m = v.lineBySeq[seq];
        return m ? '<div class="quote">' + link("event:" + seq, messageWords(m).split(": ")[0]) + "\n" + esc(m.text) + '\n<span class="note">' + esc(JOINED[m.written_by ? m.written_by.joined_by : c.joined_by]) + "</span></div>" : "";
      }).join("") + "</section>";
    }
    if (!cached) { return html + '<p class="note">Reading what it was asked and answered…</p>'; }
    if (cached.error) { return html + '<p class="note">Could not read the call: ' + esc(cached.error) + "</p>"; }
    var call = cached.call;
    html += "<section><h3>What it was asked and answered</h3>" +
      (call.system_instructions ? '<details class="box"><summary>System prompt</summary><pre>' + esc(said(call.system_instructions)) + "</pre></details>" : "") +
      (call.input_messages ? '<details class="box"><summary>Asked</summary><pre>' + esc(said(call.input_messages)) + "</pre></details>" : '<p class="note">The span does not carry what it was asked.</p>') +
      (call.output_messages ? '<details class="box" open><summary>Answered</summary><pre>' + esc(said(call.output_messages)) + "</pre></details>" : '<p class="note">The span does not carry what it answered.</p>') +
      "</section>";
    var provider = v.calls.filter(function (x) { return x.wake === c.step && x.provider !== null; });
    if (provider.length) {
      html += "<section><h3>Provider calls in its " + v.stepWord + "</h3>" + provider.map(function (x) { return exchangeBox(x.exchange, title(x.provider)); }).join("") + "</section>";
    }
    html += '<section><h3>Where it sits among the spans of its ' + v.stepWord + '</h3><div id="wf"><p class="note">Reading the spans…</p></div></section>';
    return html;
  }

  function ordinal(n) { var s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); }

  function eventDetail(seq) {
    var v = S.v, e = v.bySeq[seq];
    if (!e) { return "<h2>A change</h2><p class=\"note\">Not in this run's log.</p>"; }
    var d = describe(v, e), line = v.lineBySeq[seq];
    var html = crumbs([[null, "Run"], e.wake ? ["step:" + e.wake, stepName(v, e.wake)] : [null, "Setup"], [null, title(e.entity.provider)]]) +
      "<h2>" + esc(d.head) + "</h2>";
    if (line) { html += '<div class="quote ' + esc(line.actor) + '">' + esc(line.change === "edited" ? "from “" + line.before + "”\nto “" + line.text + "”" : line.text) + "</div>"; }
    else if (d.body) { html += '<div class="quote ' + esc(e.actor) + '">' + esc(d.body) + "</div>"; }
    html += "<dl><dt>By</dt><dd>" + esc(e.actor === "agent" ? "the agent" : e.actor === "person" ? "a person" : "the scenario") + "</dd>" +
      "<dt>In</dt><dd>" + (e.wake ? link("step:" + e.wake, stepName(v, e.wake)) : "setup") + "</dd>" +
      "<dt>Simulated</dt><dd>" + esc(when(e.sim_time)) + "</dd><dt>Real</dt><dd>" + esc(clockOf(e.wall_time)) + "</dd>" +
      "<dt>What</dt><dd>" + esc(e.operation + " of a " + words(e.after ? e.after.kind : e.entity.kind) + " in " + title(e.entity.provider)) + "</dd></dl>";
    if (line && line.written_by) {
      html += "<section><h3>Written by</h3><p>" + link("call:" + line.written_by.span_id, (line.written_by.model || "a model call") + ": what it was asked and answered") +
        '</p><p class="note">' + esc(JOINED[line.written_by.joined_by]) + "</p></section>";
    } else if (e.actor === "agent" && v.traffic.calls.length && line) {
      html += '<p class="note">No model call of the agent\'s telemetry is joined to it.</p>';
    }
    var found = v.findings.findings.filter(function (x) { return x.finding.evidence.indexOf(seq) >= 0; });
    if (found.length) {
      html += '<section><h3>Findings that cite it</h3><ul class="rows">' + found.map(function (x) {
        return '<li><span class="chip ' + x.finding.kind + '">' + KIND_LABEL[x.finding.kind] + '</span><span class="x">' + link("finding:" + x.number, findingTitle(x)) + "</span><span></span></li>";
      }).join("") + "</ul></section>";
    }
    if (e.exchange) { html += "<section><h3>The call that made it</h3>" + exchangeBox(e.exchange, title(e.entity.provider), true) + "</section>"; }
    return html;
  }

  function waitDetail(key) {
    var v = S.v, w = v.obligations.filter(function (x) { return x.obligation.key === key; })[0];
    if (!w) { return "<h2>A wait</h2>"; }
    var o = w.obligation;
    var html = crumbs([[null, "Run"], [null, "Waiting on people"]]) + "<h2>" + esc(obligationLabel(v, o)) + "</h2><dl>" +
      "<dt>Opened</dt><dd>" + esc(when(o.opened_at)) + " by " + link("event:" + o.opened_by, "this change") + "</dd>" +
      (o.expected_by ? "<dt>Expected by</dt><dd>" + esc(when(o.expected_by)) + "</dd>" : "") +
      "<dt>Settled</dt><dd>" + (o.settled_at ? esc(when(o.settled_at)) : "still open") + "</dd></dl>";
    if (o.agent_touches.length) {
      html += '<section><h3>The agent came back to it</h3><ul class="rows">' + o.agent_touches.map(function (s) {
        var e = v.bySeq[s];
        return '<li><span class="w">' + (e ? esc(when(e.sim_time)) : "") + '</span><span class="x">' + link("event:" + s, e ? describe(v, e).head : "a change") + "</span><span></span></li>";
      }).join("") + "</ul></section>";
    }
    return html;
  }

  function seedDetail(provider) {
    var v = S.v, seeded = v.events.filter(function (e) { return e.entity.provider === provider && e.wake === 0 && e.actor === "scenario"; });
    var kinds = {};
    seeded.forEach(function (e) { var k = words(e.after ? e.after.kind : e.entity.kind); kinds[k] = (kinds[k] || 0) + 1; });
    return crumbs([[null, "Run"], [null, "Setup"]]) + "<h2>" + esc(title(provider)) + ", as the scenario set it up</h2>" +
      '<ul class="rows">' + Object.keys(kinds).map(function (k) { return '<li><span class="w">' + kinds[k] + '</span><span class="x">' + esc(k) + "</span><span></span></li>"; }).join("") + "</ul>" +
      '<section><h3>Named things</h3><ul class="rows">' + seeded.filter(function (e) { return e.after; }).slice(0, 60).map(function (e) {
        return '<li><span class="w"></span><span class="x">' + link("event:" + e.seq, describe(v, e).head + (describe(v, e).body ? ": " + clip(describe(v, e).body, 70) : "")) + "</span><span></span></li>";
      }).join("") + "</ul></section>";
  }

  function outboundHow(call) {
    var c = call.exchange.captured;
    if (c === null) { return "refused: no fake answers it and the agent file does not declare it"; }
    return CAPTURED_AS[c.mode] + ", " + ANSWERED_BY[c.answered_by] + (c.replayed_from ? " (" + c.replayed_from + ")" : "");
  }

  function outboundDetail(i) {
    var v = S.v, call = v.outbound[i];
    if (!call) { return "<h2>An outbound call</h2>"; }
    var e = call.exchange, c = e.captured, replayed = c !== null && c.answered_by === "recording";
    var html = crumbs([[null, "Run"], ["step:" + call.wake, stepName(v, call.wake)], [null, "Outbound call"]]) +
      "<h2>" + esc(e.method + " " + e.host + e.path.split("?")[0]) + "</h2>" +
      '<p class="' + (replayed ? "replayed" : "") + '">' + esc(outboundHow(call)) + "; answered " + esc(String(e.status)) + ".</p>";
    if (c !== null) {
      if (c.note) { html += '<p class="note">' + esc(c.note) + "</p>"; }
      if (c.recipients.length) { html += '<p class="note">To: ' + c.recipients.map(function (r) { return esc(r.address) + (r.person ? "" : " (nobody in the scenario)"); }).join(", ") + "</p>"; }
    }
    return html + exchangeBox(e, "", true);
  }

  function findingDetail(n) {
    var v = S.v, x = v.findings.findings.filter(function (f) { return f.number === n; })[0];
    if (!x) { return "<h2>A finding</h2>"; }
    var f = x.finding, p = x.pattern;
    var html = crumbs([[null, "Run"], [null, "Findings"]]) +
      '<h2><span class="chip ' + f.kind + '">' + KIND_LABEL[f.kind] + "</span> " + esc(findingTitle(x)) + "</h2><p>" + esc(f.message) + "</p>";
    if (p) { html += '<p class="design"><b>What stops it:</b> ' + esc(p.design) + "</p>"; }
    var placed = [];
    if (f.at) { placed.push("<dt>When</dt><dd>" + esc(when(f.at)) + " simulated</dd>"); }
    if (f.wake !== null) { placed.push("<dt>In</dt><dd>" + link("step:" + f.wake, stepName(v, f.wake)) + "</dd>"); }
    if (placed.length) { html += "<dl>" + placed.join("") + "</dl>"; }
    if (f.evidence.length) {
      html += '<section><h3>Evidence</h3><ul class="rows">' + f.evidence.map(function (s) {
        var e = v.bySeq[s];
        return '<li><span class="w">' + (e ? esc(when(e.sim_time)) : "") + '</span><span class="x">' + link("event:" + s, e ? describe(v, e).head : "a change not in this log") + "</span><span></span></li>";
      }).join("") + "</ul></section>";
      var known = v.modelCalls;
      if (known.received === 0) {
        html += '<p class="note">No telemetry was received from the agent in this run, so what its model was asked and answered is not known.</p>';
      } else {
        var traced = known.events.filter(function (t) { return f.evidence.indexOf(t.seq) >= 0 && t.model_call; });
        html += traced.length
          ? "<section><h3>The model call behind it</h3>" + traced.map(function (t) {
            var c = t.model_call;
            return "<p>" + link("call:" + c.span_id, (c.model || "a model call") + ", " + clockOf(c.started) + " real: what it was asked and answered") + '</p><p class="note">' + esc(JOINED[t.joined_by]) + "</p>";
          }).join("") + "</section>"
          : '<p class="note">The agent\'s telemetry was received, and no model call in it led to this evidence.</p>';
      }
    } else if (f.wake === null) {
      html += '<p class="note">It is about the run as a whole, so nothing on the timeline is selected.</p>';
    }
    return html;
  }

  /* ---------- a step's spans as a waterfall (Observable Plot) ---------- */

  function waterfall(step, focus) {
    var have = S.spans[step];
    if (!have) {
      get(path(API.stepSpans, S.run, { step: step })).then(function (b) { S.spans[step] = b.spans; waterfall(step, focus); })
        .catch(function (e) { var box = $("wf"); if (box) { box.innerHTML = '<p class="note">Could not read the spans: ' + esc(e.message) + "</p>"; } });
      return;
    }
    var box = $("wf");
    if (!box) { return; }
    if (!have.length) { box.innerHTML = '<p class="note">No span of the agent\'s telemetry is placed in it.</p>'; return; }
    // the forest, depth first; spans shorter than 1% of the step are left out unless they are model calls or lead
    // to the focused one
    var byId = {}, kids = {}, roots = [];
    have.forEach(function (s) { byId[s.span_id] = s; });
    have.forEach(function (s) {
      if (s.parent_span_id && byId[s.parent_span_id]) { (kids[s.parent_span_id] = kids[s.parent_span_id] || []).push(s); } else { roots.push(s); }
    });
    var t0 = Math.min.apply(null, have.map(function (s) { return ms(s.start); }));
    var t1 = Math.max.apply(null, have.map(function (s) { return ms(s.end); }));
    var least = (t1 - t0) * 0.01, onPath = {};
    if (focus && byId[focus]) { for (var s = byId[focus]; s; s = s.parent_span_id ? byId[s.parent_span_id] : null) { onPath[s.span_id] = true; } }
    var rows = [], hidden = 0, showAll = S.allSpans === step;
    (function walk(list, depth) {
      list.sort(function (a, b) { return ms(a.start) - ms(b.start); }).forEach(function (s) {
        var keep = showAll || s.model_call || onPath[s.span_id] || ms(s.end) - ms(s.start) >= least;
        if (keep) { rows.push({ s: s, depth: depth }); } else { hidden += 1; }
        walk(kids[s.span_id] || [], depth + 1);
      });
    })(roots, 0);
    var data = rows.map(function (r, i) {
      var s = r.s;
      return {
        row: String(i), start: (ms(s.start) - t0) / 1000, end: Math.max((ms(s.end) - t0) / 1000, (ms(s.start) - t0) / 1000 + 0.001),
        label: " ".repeat(Math.min(r.depth, 12) * 2) + clip(s.name, 48), name: s.name, service: s.service, took: (ms(s.end) - ms(s.start)) / 1000,
        kind: s.span_id === focus ? "this call" : s.model_call ? "model call" : s.status === "error" ? "failed" : "span", span: s.span_id, model: s.model_call
      };
    });
    var width = Math.max(box.clientWidth - 2, 300), rowH = 15;
    var chart = Plot.plot({
      width: width, height: data.length * rowH + 36, marginLeft: Math.min(230, width * 0.48), marginRight: 10, marginTop: 24,
      style: { background: "transparent", color: "var(--ink-2)", fontSize: "10.5px" },
      x: { label: "seconds into the " + S.v.stepWord, grid: true, axis: "top" },
      y: { domain: data.map(function (d) { return d.row; }), tickFormat: function (r) { return data[+r].label; }, label: null, tickSize: 0 },
      color: { domain: ["span", "model call", "this call", "failed"], range: ["var(--axis)", "var(--s1)", "var(--ink)", "var(--fail)"] },
      marks: [
        Plot.barX(data, { x1: "start", x2: "end", y: "row", fill: "kind", insetTop: 2, insetBottom: 2, rx: 2,
          title: function (d) { return d.name + (d.service ? " (" + d.service + ")" : "") + "\n" + short(d.took); } })
      ]
    });
    box.innerHTML = '<div class="waterfall"></div>' + (hidden || showAll
      ? '<p class="note">' + (showAll ? "Every span shown. " : hidden + " spans shorter than 1% of the " + S.v.stepWord + " are left out. ") +
        '<button type="button" class="link" id="allspans">' + (showAll ? "Leave the short ones out" : "Show every span") + "</button></p>" : "") +
      '<p class="note">Grey: a span · blue: a model call' + (focus ? " · black: this call" : "") + ". Click a model call to open it.</p>";
    box.firstChild.appendChild(chart);
    var bars = chart.querySelectorAll("rect");
    var barRects = Array.prototype.filter.call(bars, function (r) { return r.parentNode && r.parentNode.getAttribute("aria-label") === "bar"; });
    barRects.forEach(function (r, i) {
      if (data[i] && data[i].model) { r.style.cursor = "pointer"; r.addEventListener("click", function () { select("call:" + data[i].span, true); }); }
    });
    var all = $("allspans");
    if (all) { all.addEventListener("click", function () { S.allSpans = showAll ? null : step; waterfall(step, focus); }); }
    if (focus) {
      var at = data.findIndex(function (d) { return d.span === focus; });
      if (at >= 0) { box.firstChild.scrollTop = Math.max(0, at * rowH - 120); }
    }
  }

  /* ---------- the tabs below the timeline ---------- */

  function tabs() {
    var v = S.v, list = [];
    if (v.traffic.calls.length || v.traffic.hosts.length) { list.push(["calls", "Model calls (" + v.traffic.calls.length + ")"]); }
    list.push(["said", "What was said (" + v.messages.length + ")"]);
    var provider = v.calls.filter(function (c) { return c.provider !== null; });
    list.push(["provider", "Provider calls (" + provider.length + ")"]);
    if (v.outbound.length) { list.push(["outbound", "Outbound calls (" + v.outbound.length + ")"]); }
    if (v.run.fork) { list.push(["fork", "What this fork is"]); }
    if (!list.some(function (t) { return t[0] === S.tab; })) { S.tab = list[0][0]; }
    $("tabs").innerHTML = list.map(function (t) {
      return '<button type="button" role="tab" data-tab="' + t[0] + '" aria-selected="' + (t[0] === S.tab) + '">' + esc(t[1]) + "</button>";
    }).join("");
    var pane = $("pane");
    if (S.tab === "calls") { modelPane(pane); }
    else if (S.tab === "said") { pane.innerHTML = saidPane(); }
    else if (S.tab === "provider") { pane.innerHTML = providerPane(provider); }
    else if (S.tab === "outbound") { pane.innerHTML = outboundPane(); }
    else if (S.tab === "fork") { pane.innerHTML = forkPane(v.run.fork); }
    if (OPEN_ALL) { pane.querySelectorAll("details").forEach(function (d) { d.open = true; }); }
  }

  function modelPane(pane) {
    var v = S.v, t = v.traffic;
    var html = t.hosts.map(function (h) {
      return '<p class="note">' + esc(h.host) + ": " + plural(h.calls, "call", "calls") + " on " + plural(h.connections, "connection", "connections") +
        ", relayed and never opened (" + count(h.bytes_sent) + "B sent, " + count(h.bytes_received) + "B received). What was asked and answered there is not kept; what follows is what the agent's own telemetry sent.</p>";
    }).join("");
    if (!t.calls.length) { pane.innerHTML = html + '<p class="note">No span of a model call was received, so the calls cannot be listed one by one.</p>'; return; }
    html += '<div class="plot-legend">' + v.models.slice(0, 3).map(function (m, i) { return '<span><i style="background:var(--s' + (i + 1) + ')"></i>' + esc(m) + "</span>"; }).join("") +
      (v.models.length > 3 ? '<span><i style="background:var(--other)"></i>other models</span>' : "") + "</div>";
    html += '<div class="charts"><figure><figcaption>Model calls per ' + v.stepWord + '</figcaption><div class="chart" id="c-calls"></div></figure>' +
      '<figure><figcaption>Tokens per ' + v.stepWord + ', in and out</figcaption><div class="chart" id="c-tokens"></div></figure>' +
      '<figure><figcaption>Time spent waiting on the model, per ' + v.stepWord + '</figcaption><div class="chart" id="c-time"></div></figure></div>';
    html += '<figure style="margin:0"><figcaption class="note">Each call: when it was asked (real time) against how long it took; the larger the dot, the more tokens. A ringed dot wrote a message. Click one to open it.</figcaption><div class="chart" id="c-each"></div></figure>';
    var step = S.sel && S.sel.indexOf("step:") === 0 ? +S.sel.slice(5) : null;
    html += '<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><h2>Every call</h2>' +
      (step !== null ? '<label class="note"><input type="checkbox" id="onlystep"' + (S.onlyStep ? " checked" : "") + "> only " + esc(stepName(v, step)) + "</label>" : "") +
      '<span class="note">Click a column to sort; click a row to open the call.</span></div>';
    html += '<div class="tablewrap" id="calltable"></div>';
    pane.innerHTML = html;
    var only = $("onlystep");
    if (only) { only.addEventListener("change", function () { S.onlyStep = only.checked; tabs(); }); }
    table(step !== null && S.onlyStep ? step : null);
    charts();
  }

  var COLUMNS = [
    ["step", "Step", function (c) { return c.step; }, false],
    ["started", "Asked (real)", function (c) { return ms(c.started); }, false],
    ["model", "Model", function (c) { return c.model || ""; }, false],
    ["took", "Took", function (c) { return took(c); }, true],
    ["in", "Tokens in", function (c) { return c.input_tokens || 0; }, true],
    ["out", "Tokens out", function (c) { return c.output_tokens || 0; }, true],
    ["wrote", "Wrote", function (c) { return c.wrote.length; }, false]
  ];

  function table(step) {
    var v = S.v, col = COLUMNS.filter(function (c) { return c[0] === S.sort.key; })[0] || COLUMNS[1];
    var rows = v.traffic.calls.filter(function (c) { return step === null || c.step === step; }).slice().sort(function (a, b) {
      var x = col[2](a), y = col[2](b);
      return (x < y ? -1 : x > y ? 1 : 0) * S.sort.dir || ms(a.started) - ms(b.started);
    });
    var head = "<thead><tr>" + COLUMNS.map(function (c) {
      var sorted = c[0] === S.sort.key ? (S.sort.dir > 0 ? "ascending" : "descending") : "none";
      return '<th scope="col" class="' + (c[3] ? "r" : "") + '" aria-sort="' + sorted + '"><button type="button" data-sort="' + c[0] + '">' + c[1] + "</button></th>";
    }).join("") + "</tr></thead>";
    var bodyRows = rows.map(function (c) {
      var wrote = c.wrote.map(function (seq) { var m = v.lineBySeq[seq]; return m ? "“" + clip(m.text, 90) + "”" : ""; }).join(" ");
      return '<tr tabindex="0" data-sel="call:' + esc(c.span_id) + '"' + (S.sel === "call:" + c.span_id ? ' class="on"' : "") + ">" +
        "<td>" + esc(stepName(v, c.step)) + "</td><td>" + esc(clockOf(c.started)) + '</td><td><span class="sw" style="background:var(--' + (v.slot(c.model) ? "s" + v.slot(c.model) : "other") + ')"></span>' + esc(c.model || "not named") + "</td>" +
        '<td class="r">' + esc(short(took(c))) + '</td><td class="r">' + (c.input_tokens === null ? "?" : c.input_tokens.toLocaleString("en")) + '</td><td class="r">' + (c.output_tokens === null ? "?" : c.output_tokens.toLocaleString("en")) + "</td>" +
        '<td class="txt">' + esc(wrote) + "</td></tr>";
    }).join("");
    $("calltable").innerHTML = '<table class="calls">' + head + "<tbody>" + bodyRows + "</tbody></table>";
    $("calltable").querySelector("thead").addEventListener("click", function (ev) {
      var b = ev.target.closest("[data-sort]");
      if (!b) { return; }
      var key = b.getAttribute("data-sort");
      S.sort = { key: key, dir: S.sort.key === key ? -S.sort.dir : (COLUMNS.filter(function (c) { return c[0] === key; })[0][3] ? -1 : 1) };
      table(step);
    });
  }

  function charts() {
    var v = S.v, calls = v.traffic.calls;
    var steps = [];
    calls.forEach(function (c) { if (steps.indexOf(c.step) < 0) { steps.push(c.step); } });
    steps.sort(function (a, b) { return a - b; });
    var names = steps.map(function (n) { return stepName(v, n); });
    var colour = { domain: v.models.slice(0, 3).concat(v.models.length > 3 ? ["other models"] : []), range: ["var(--s1)", "var(--s2)", "var(--s3)", "var(--other)"] };
    function modelOf(c) { var m = c.model || "model not named"; return v.models.indexOf(m) < 3 ? m : "other models"; }
    var per = {};
    calls.forEach(function (c) {
      var k = c.step + "|" + modelOf(c);
      var p = per[k] = per[k] || { step: stepName(v, c.step), n: c.step, model: modelOf(c), calls: 0, secs: 0 };
      p.calls += 1; p.secs += took(c);
    });
    var rows = Object.keys(per).map(function (k) { return per[k]; });
    var tok = [];
    steps.forEach(function (n) {
      var inStep = calls.filter(function (c) { return c.step === n; });
      tok.push({ step: stepName(v, n), n: n, kind: "in", tokens: inStep.reduce(function (a, c) { return a + (c.input_tokens || 0); }, 0) });
      tok.push({ step: stepName(v, n), n: n, kind: "out", tokens: inStep.reduce(function (a, c) { return a + (c.output_tokens || 0); }, 0) });
    });
    var selStep = S.sel && S.sel.indexOf("step:") === 0 ? stepName(v, +S.sel.slice(5)) : null;
    function base(el) {
      var w = Math.max(240, $(el).clientWidth);
      return { width: w, height: Math.max(110, steps.length * 26 + 44), marginLeft: Math.min(150, Math.max(70, Math.max.apply(null, names.map(function (s) { return s.length; })) * 6.4)), marginRight: 16,
        style: { background: "transparent", color: "var(--ink-2)", fontSize: "11px" },
        y: { domain: names, label: null, tickSize: 0 } };
    }
    function bars(el, opts) {
      var plot = Plot.plot(opts);
      $(el).innerHTML = ""; $(el).appendChild(plot);
      plot.addEventListener("click", function () {
        var d = plot.value;
        if (d && d.n !== undefined) { select("step:" + d.n, true); }
      });
      return plot;
    }
    function faded(d) { return selStep && d.step !== selStep ? 0.3 : 1; }
    var o1 = base("c-calls");
    bars("c-calls", Object.assign(o1, {
      x: { label: "calls", grid: true }, color: colour,
      marks: [Plot.barX(rows, { x: "calls", y: "step", fill: "model", fillOpacity: faded, inset: 1, tip: { format: { n: false } }, title: function (d) { return d.step + ": " + plural(d.calls, "call", "calls") + " to " + d.model; } }),
        Plot.ruleX([0], { stroke: "var(--axis)" })]
    }));
    var o2 = base("c-tokens");
    bars("c-tokens", Object.assign(o2, {
      x: { label: "tokens", grid: true, tickFormat: "s" }, color: { domain: ["in", "out"], range: ["var(--s1)", "var(--s2)"], legend: false },
      marks: [Plot.barX(tok, { x: "tokens", y: "step", fill: "kind", fillOpacity: faded, inset: 1, tip: { format: { n: false } }, title: function (d) { return d.step + ": " + d.tokens.toLocaleString("en") + " tokens " + d.kind; } }),
        Plot.ruleX([0], { stroke: "var(--axis)" })]
    }));
    var o3 = base("c-time");
    bars("c-time", Object.assign(o3, {
      x: { label: "seconds", grid: true }, color: colour,
      marks: [Plot.barX(rows, { x: "secs", y: "step", fill: "model", fillOpacity: faded, inset: 1, tip: { format: { n: false } }, title: function (d) { return d.step + ": " + short(d.secs) + " answering, " + d.model; } }),
        Plot.ruleX([0], { stroke: "var(--axis)" })]
    }));
    // the token figure's own key: in and out are not the models' colours
    $("c-tokens").insertAdjacentHTML("afterbegin", '<div class="plot-legend"><span><i style="background:var(--s1)"></i>in</span><span><i style="background:var(--s2)"></i>out</span></div>');
    var each = calls.map(function (c) { return { at: new Date(c.started), took: took(c), tokens: tokens(c), model: modelOf(c), wrote: c.wrote.length > 0, span: c.span_id, step: stepName(v, c.step), raw: c }; });
    var w = Math.max(300, $("c-each").clientWidth);
    var dots = Plot.plot({
      width: w, height: 230, marginLeft: 44, marginRight: 16, style: { background: "transparent", color: "var(--ink-2)", fontSize: "11px" },
      x: { label: "asked at (real time, UTC)", type: "utc" }, y: { label: "seconds to answer", grid: true },
      r: { range: [2.5, 14] }, color: colour,
      marks: [
        Plot.dot(each.filter(function (d) { return d.wrote; }), { x: "at", y: "took", r: "tokens", stroke: "var(--ink)", strokeWidth: 2.5, fill: "none" }),
        Plot.dot(each, { x: "at", y: "took", r: "tokens", fill: "model", fillOpacity: 0.75, stroke: "var(--surface)", strokeWidth: 1,
          tip: true, channels: { step: "step", tokens: "tokens" }, title: function (d) { return d.model + " · " + d.step + "\n" + short(d.took) + " · " + d.tokens.toLocaleString("en") + " tokens" + (d.wrote ? "\nwrote a message" : ""); } })
      ]
    });
    $("c-each").innerHTML = ""; $("c-each").appendChild(dots);
    dots.addEventListener("click", function () { if (dots.value && dots.value.span) { select("call:" + dots.value.span, true); } });
  }

  function saidPane() {
    var v = S.v;
    if (!v.messages.length) { return '<p class="note">No message was sent, rewritten or deleted.</p>'; }
    return '<p class="note">Every message, and every decision left waiting on a person in the agent’s own product, in the order the record holds them. Click one to read it beside the model call that wrote it.</p><ol class="said">' +
      v.messages.map(function (m) {
        var wrote = m.written_by ? '<span class="note">Written by ' + esc(m.written_by.model || "a model call") + ", " + esc(JOINED[m.written_by.joined_by]) + ".</span>" : "";
        return '<li tabindex="0" data-sel="event:' + m.seq + '"><time>' + esc(when(m.at)) + " · " + esc(stepName(v, m.wake)) + '</time><span class="x ' + esc(m.change) + '">' + esc(messageWords(m)) + "</span>" + wrote + "</li>";
      }).join("") + "</ol>";
  }

  function providerPane(provider) {
    if (!provider.length) { return '<p class="note">The agent called no provider.</p>'; }
    var v = S.v, byStep = {};
    provider.forEach(function (c) { (byStep[c.wake] = byStep[c.wake] || []).push(c); });
    return Object.keys(byStep).map(Number).sort(function (a, b) { return a - b; }).map(function (n) {
      return "<section><h3>" + link("step:" + n, stepName(v, n)) + "</h3>" + byStep[n].map(function (c) { return exchangeBox(c.exchange, title(c.provider)); }).join("") + "</section>";
    }).join("");
  }

  function outboundPane() {
    var v = S.v;
    return '<p class="note">Calls to hosts no fake answers: an email API, a search, a page. Credentials and declared fields are redacted.</p>' +
      v.outbound.map(function (call, i) {
        var e = call.exchange, c = e.captured, replayed = c !== null && c.answered_by === "recording";
        return '<details class="box"><summary>' + esc(e.method + " " + e.host + e.path.split("?")[0] + " · " + e.status + " · " + stepName(v, call.wake) + " · " + when(call.sim_time)) + ' — <span class="' + (replayed ? "replayed" : "") + '">' + esc(outboundHow(call)) + "</span> " + link("out:" + i, "show") + "</summary>" +
          "<div>Request</div><pre>" + esc(body(e.request_body, c ? c.request : null)) + "</pre><div>Answer</div><pre>" + esc(body(e.response_body, c ? c.response : null)) + "</pre></details>";
      }).join("");
  }

  function forkPane(f) {
    var parentRow = S.list.filter(function (r) { return r.run_id === f.parent_run; })[0];
    var parentName = parentRow ? runLabel(parentRow) : "its parent";
    var whereSplit = f.after_wake === 0 ? "at setup, before the first wake"
      : (f.ran_on ? "after wake " + f.after_wake + ", once the clock had run on with nothing due to" : "at the end of wake " + f.after_wake);
    var html = '<div class="fork"><dl>';
    html += '<dt>Split from</dt><dd><a href="#run=' + esc(encodeURIComponent(f.parent_run)) + '">' + esc(parentName) + "</a> " + esc(whereSplit) + ", " + esc(when(f.at)) + " simulated</dd>";
    html += "<dt>What it changed</dt><dd>" + (f.changes.length ? "<ul>" + f.changes.map(function (c) { return "<li>" + esc(c) + "</li>"; }).join("") + "</ul>" : "Nothing: a rerun from the same moment, to see how much the agent varies.") + "</dd>";
    html += "<dt>Agent restored</dt><dd>" + (f.restore
      ? '<span class="chip ' + (f.restore.verified ? "ok" : "review") + '">' + (f.restore.verified ? "Verified" : "Not verified") + "</span> " + esc(f.restore.words.replace(/^(not )?verified: /, ""))
      : "No restore was recorded with this fork.") + "</dd></dl>";
    html += '<p><label class="note"><input type="checkbox" id="compare"' + (S.compare ? " checked" : "") + "> On the simulated clock, show what the parent did after the split in a lane of its own</label></p>";
    var o = f.outcome;
    html += "<h3>Against its parent, from the split on</h3>";
    if (!o) { return html + '<p class="note">Compared once both runs have finished.</p></div>'; }
    html += "<p>Verdict: " + '<span class="chip ' + VERDICT_CLASS[o.parent_verdict.kind] + '">' + VERDICT_WORD[o.parent_verdict.kind] + "</span>" +
      (o.verdict_changed ? ' → <span class="chip ' + VERDICT_CLASS[o.fork_verdict.kind] + '">' + VERDICT_WORD[o.fork_verdict.kind] + "</span>" : " in both") + "</p>";
    html += o.scorecard.length
      ? "<table><thead><tr><th>Scorecard</th><th>Parent</th><th>This fork</th></tr></thead><tbody>" + o.scorecard.map(function (d) { return "<tr><td>" + esc(d.label) + "</td><td>" + esc(d.parent) + "</td><td>" + esc(d.fork) + "</td></tr>"; }).join("") + "</tbody></table>"
      : '<p class="note">Every line of the scorecard is the same.</p>';
    var found = [];
    o.findings_gained.forEach(function (x) { found.push(["gained", "Gained", words(x.check) + ": " + x.message]); });
    o.findings_lost.forEach(function (x) { found.push(["lost", "Gone", words(x.check) + ": " + x.message]); });
    o.findings_changed.forEach(function (x) { found.push(["changed", "Changed", words(x.fork.check) + ": " + x.parent.message + " → " + x.fork.message]); });
    html += found.length
      ? '<ul class="found">' + found.map(function (x) { return '<li><span class="chip ' + x[0] + '">' + x[1] + "</span><span>" + esc(x[2]) + "</span></li>"; }).join("") + "</ul>"
      : '<p class="note">The same findings in both.</p>';
    var split = o.first_divergence, kinds = { change: "change in the world", call: "call", report: "report of the agent's", model_call: "model call" };
    if (!split) {
      html += '<p class="note">From the split on, both records made the same changes and calls at the same moments, the agent reported alike and its models were asked and answered alike.</p>';
    } else {
      html += '<div class="part"><b>First difference</b><span>' + (split.shared ? "a " + kinds[split.kind] + ", after " + plural(split.shared, "thing", "things") + " both did alike" : "the very first " + kinds[split.kind] + " after the split") + ": " + esc(split.differs) + "</span>" +
        "<b>The parent</b><span>" + esc(split.parent ? split.parent.words : "had nothing more of it") + "</span>" +
        "<b>This fork</b><span>" + esc(split.fork ? split.fork.words : "had nothing more of it") + "</span></div>";
    }
    return html + "</div>";
  }

  document.addEventListener("change", function (ev) {
    if (ev.target && ev.target.id === "compare") { S.compare = ev.target.checked; timeline(false); showSelection(false); }
  });
  window.addEventListener("hashchange", function () {
    var at = where();
    if (at.run && at.run !== S.run) { open(at.run, at); return; }
    if (at.sel !== S.sel) { S.sel = at.sel; showSelection(true); }
  });
  var resizing = null, lastWidth = window.innerWidth;
  window.addEventListener("resize", function () {
    clearTimeout(resizing);
    resizing = setTimeout(function () {
      if (!S.v || Math.abs(window.innerWidth - lastWidth) < 40) { return; }
      lastWidth = window.innerWidth;
      tabs(); detail();
    }, 200);
  });

  // a selection named in the location is revealed once the timeline has drawn
  var firstReveal = true;
  var drawn = draw;
  draw = function (fresh) {
    drawn(fresh);
    if (firstReveal && S.sel) { firstReveal = false; var ids = marksOf(S.sel); if (ids.length) { S.timeline.once("changed", function () { reveal(ids); }); } }
  };

  loadList();
  setInterval(loadList, LIST_REFRESH);
})();
