/* The swimlane timeline, drawn on a canvas so a run of a year and tens of thousands of marks pans and zooms at
   frame rate.

   Each lane keeps its marks' times sorted (one array per clock), so what a window holds is two binary searches.
   A lane that holds more marks in the window than it has room for is drawn binned by the calendar unit that fits
   (year, month, week, day, hour, ten minutes, minute, ten seconds, second): a heat strip whose shade is the count,
   the count written in each bin wide enough, and a red stripe for the failed marks in it. Zoomed in far enough, every
   mark is drawn on its own. The findings sit in a row of their own at the top, always one by one; a minimap under
   the lanes shows the whole run and the window, which can be dragged.

   Interaction: drag to pan, wheel or pinch to zoom around the pointer, shift+wheel to pan; a click on a mark selects
   it (again on the same spot: the next mark at that instant), a click on a bin zooms into it. */

import { esc, when, clock as realClock, plural, words, count } from "./util.js";

const KINDS = ["wake", "sent", "said", "edited", "wait", "call", "call_failed", "change", "read", "due", "due_fault",
  "model_call", "memory_write", "memory_read", "stored", "span"];
const FAILED = new Set(["call_failed", "due_fault"]);
const KIND_WORD = {
  wake: "wake", sent: "from the agent", said: "from the person", edited: "rewritten or deleted", wait: "wait",
  call: "call", call_failed: "failed call", change: "change", read: "read", due: "due entry", due_fault: "faulted entry",
  model_call: "model call", memory_write: "memory write", memory_read: "memory read", stored: "stored item", span: "span"
};
const AXIS = 26, ROW = 22, MIN_SIM = 1000, MIN_REAL = 20, DENSE = 4;
const UNITS = [
  ["second", () => d3.utcSecond, 1e3], ["10 seconds", () => d3.utcSecond.every(10), 1e4],
  ["minute", () => d3.utcMinute, 6e4], ["10 minutes", () => d3.utcMinute.every(10), 6e5],
  ["hour", () => d3.utcHour, 36e5], ["6 hours", () => d3.utcHour.every(6), 216e5],
  ["day", () => d3.utcDay, 864e5], ["week", () => d3.utcMonday, 6048e5],
  ["month", () => d3.utcMonth, 2.63e9], ["year", () => d3.utcYear, 3.156e10]
];
const PRESETS = [["all", "All"], ["year", "Year"], ["month", "Month"], ["week", "Week"], ["day", "Day"], ["hour", "Hour"]];
const PRESET_MS = { year: 3.156e10, month: 2.63e9, week: 6048e5, day: 864e5, hour: 36e5 };

function lowerBound(a, x) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < x) { lo = m + 1; } else { hi = m; } } return lo; }
function upperBound(a, x) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] <= x) { lo = m + 1; } else { hi = m; } } return lo; }
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function tickFormat(d) {
  const f = d3.utcSecond(d) < d ? ".%L" : d3.utcMinute(d) < d ? ":%S" : d3.utcHour(d) < d ? "%H:%M" : d3.utcDay(d) < d ? "%H:%M"
    : d3.utcMonth(d) < d ? (d3.utcWeek(d) < d ? "%a %d" : "%b %d") : d3.utcYear(d) < d ? "%B" : "%Y";
  return d3.utcFormat(f)(d);
}
function periodWords(unit, t0) {
  const d = new Date(t0);
  if (unit === "year") { return d3.utcFormat("%Y")(d); }
  if (unit === "month") { return d3.utcFormat("%B %Y")(d); }
  if (unit === "week") { return "Week of " + d3.utcFormat("%-d %b %Y")(d); }
  if (unit === "day") { return d3.utcFormat("%a %-d %b %Y")(d); }
  return when(t0, { seconds: unit.indexOf("second") >= 0 });
}

export class Timeline {
  constructor(root, hooks) {
    this.root = root;
    this.hooks = hooks;
    this.clock = "sim";
    this.hidden = new Set();
    this.selected = new Set();
    this.focus = null;
    this.filter = "";
    this.data = null;
    this.win = [0, 1];
    this.dirty = false;
    this.layoutState = [];
    this.cycle = null;
    root.innerHTML =
      '<div class="tl-tools">' +
      '<span class="seg" role="group" aria-label="Range">' + PRESETS.map((p) => '<button type="button" data-preset="' + p[0] + '">' + p[1] + "</button>").join("") + "</span>" +
      '<span class="seg" role="group" aria-label="Zoom"><button type="button" data-zoom="out" aria-label="Zoom out">−</button><button type="button" data-zoom="in" aria-label="Zoom in">+</button></span>' +
      '<span class="seg" role="group" aria-label="Clock"><button type="button" data-clock="sim" aria-pressed="true">Simulated</button><button type="button" data-clock="real" aria-pressed="false">Real</button></span>' +
      '<span class="tl-range" data-role="range"></span><span class="tl-note" data-role="note"></span>' +
      '<input class="tl-filter" type="search" data-role="filter" placeholder="Filter marks  /" aria-label="Filter the timeline">' +
      "</div>" +
      '<div class="tl-main" data-role="main"><div class="tl-labels" data-role="labels"></div><div class="tl-canvas-box" data-role="box"><canvas data-role="canvas"></canvas></div></div>' +
      '<div class="tl-mini"><canvas data-role="mini" aria-label="The whole run: drag to move the window"></canvas></div>';
    const q = (r) => root.querySelector('[data-role="' + r + '"]');
    this.canvas = q("canvas"); this.mini = q("mini"); this.box = q("box"); this.labels = q("labels");
    this.rangeEl = q("range"); this.noteEl = q("note"); this.filterEl = q("filter");
    this.tooltip = document.getElementById("tooltip");
    this.bind();
    new ResizeObserver(() => this.request()).observe(this.box);
  }

  /* ---------- data ---------- */

  setData(tl, explained) {
    const n = tl.marks.sim.length;
    const kind = new Uint8Array(n), lane = new Uint16Array(n);
    const sim = Float64Array.from(tl.marks.sim), real = Float64Array.from(tl.marks.real);
    const simEnd = new Float64Array(n), realEnd = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      kind[i] = KINDS.indexOf(tl.marks.kind[i]);
      lane[i] = tl.marks.lane[i];
      simEnd[i] = tl.marks.sim_end[i] === null ? NaN : tl.marks.sim_end[i];
      realEnd[i] = tl.marks.real_end[i] === null ? NaN : tl.marks.real_end[i];
    }
    const byRef = new Map();
    for (let i = 0; i < n; i++) { const r = tl.marks.ref[i]; const l = byRef.get(r); if (l) { l.push(i); } else { byRef.set(r, [i]); } }
    const keep = this.data && this.data.run === tl.run;
    this.data = { tl, n, kind, lane, sim, real, simEnd, realEnd, ref: tl.marks.ref, label: tl.marks.label, byRef, explained,
      lanes: tl.lanes, findings: tl.findings };
    this.index();
    if (!keep) { this.selected = new Set(); }
    this.renderLabels();
    this.request();
  }

  /** Per lane and clock: the marks' indices sorted by time, their times, and the failed ones apart. */
  index() {
    const d = this.data, lanes = d.lanes.length, match = this.matcher();
    d.order = {};
    for (const c of ["sim", "real"]) {
      const t = c === "sim" ? d.sim : d.real;
      const per = Array.from({ length: lanes }, () => []);
      for (let i = 0; i < d.n; i++) { if (!match || match(i)) { per[d.lane[i]].push(i); } }
      d.order[c] = per.map((list) => {
        if (c === "real") { list.sort((a, b) => t[a] - t[b]); }
        const idx = Int32Array.from(list), times = new Float64Array(idx.length);
        const failed = [];
        for (let k = 0; k < idx.length; k++) { times[k] = t[idx[k]]; if (FAILED.has(KINDS[d.kind[idx[k]]])) { failed.push(t[idx[k]]); } }
        return { idx, times, failed: Float64Array.from(failed) };
      });
      const all = [];
      for (let i = 0; i < d.n; i++) { all.push(i); }
      all.sort((a, b) => t[a] - t[b] || a - b);
      d.order[c].all = Int32Array.from(all);
    }
    this.extent();
  }

  matcher() {
    const f = this.filter.trim().toLowerCase();
    if (!f) { return null; }
    const d = this.data;
    return (i) => d.label[i].toLowerCase().indexOf(f) >= 0 || d.ref[i] === f;
  }

  extent() {
    const d = this.data, t = this.clock === "sim" ? d.sim : d.real;
    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < d.n; i++) { if (t[i] < lo) { lo = t[i]; } if (t[i] > hi) { hi = t[i]; } }
    if (this.clock === "sim") {
      lo = Math.min(lo, d.tl.starts); hi = Math.max(hi, d.tl.reached);
    }
    if (!isFinite(lo)) { lo = d.tl.starts; hi = d.tl.reached; }
    if (hi - lo < (this.clock === "sim" ? 60000 : 1000)) { hi = lo + (this.clock === "sim" ? 60000 : 1000); }
    const pad = (hi - lo) * 0.02;
    d.extent = [lo - pad, hi + pad];
  }

  /* ---------- window ---------- */

  setClock(c) {
    if (c === this.clock || !this.data) { return; }
    const mid = this.toClock((this.win[0] + this.win[1]) / 2, c);
    this.clock = c;
    this.root.querySelectorAll("[data-clock]").forEach((b) => b.setAttribute("aria-pressed", String(b.getAttribute("data-clock") === c)));
    this.extent();
    this.renderLabels();
    this.fit();
    if (this.focus !== null) { this.revealIndex([this.focus]); } else if (isFinite(mid)) { this.win = this.clamp(this.win); }
    this.request();
  }

  /** A moment on the current clock carried to the other, through the nearest mark at or before it. */
  toClock(t, target) {
    const d = this.data, from = this.clock === "sim" ? d.sim : d.real, to = target === "sim" ? d.sim : d.real;
    const all = d.order[this.clock].all;
    let lo = 0, hi = all.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (from[all[m]] <= t) { lo = m + 1; } else { hi = m; } }
    const at = Math.max(0, lo - 1);
    return all.length ? to[all[at]] : t;
  }

  clamp([a, b]) {
    const [lo, hi] = this.data.extent, least = this.clock === "sim" ? MIN_SIM : MIN_REAL;
    let w = Math.max(b - a, least);
    const most = (hi - lo) * 1.5;
    if (w > most) { w = most; }
    let mid = (a + b) / 2;
    mid = Math.min(Math.max(mid, lo), hi);
    return [mid - w / 2, mid + w / 2];
  }

  setWindow(a, b, animate) {
    const target = this.clamp([a, b]);
    if (!animate) { this.win = target; this.request(); this.announce(); return; }
    const from = this.win.slice(), t0 = performance.now(), dur = 260;
    const step = (now) => {
      const k = Math.min(1, (now - t0) / dur), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      this.win = [from[0] + (target[0] - from[0]) * e, from[1] + (target[1] - from[1]) * e];
      this.draw();
      if (k < 1) { requestAnimationFrame(step); } else { this.announce(); }
    };
    requestAnimationFrame(step);
  }

  announce() { if (this.hooks.onWindow) { this.hooks.onWindow(this.win, this.clock); } }

  fit() { if (this.data) { this.setWindow(this.data.extent[0], this.data.extent[1], false); } }

  preset(name) {
    if (name === "all") { this.fit(); return; }
    const mid = this.focus !== null ? this.timeOf(this.focus) : (this.win[0] + this.win[1]) / 2;
    const w = PRESET_MS[name];
    this.setWindow(mid - w / 2, mid + w / 2, true);
  }

  zoom(factor, around) {
    const [a, b] = this.win, c = around === undefined ? (a + b) / 2 : around;
    this.setWindow(c - (c - a) * factor, c + (b - c) * factor, false);
  }

  pan(fraction) { const w = this.win[1] - this.win[0]; this.setWindow(this.win[0] + w * fraction, this.win[1] + w * fraction, false); }

  timeOf(i) { return this.clock === "sim" ? this.data.sim[i] : this.data.real[i]; }

  /* ---------- selection, filter, lanes ---------- */

  select(refs, focusRef) {
    this.selected = new Set(refs);
    const d = this.data;
    this.focus = focusRef && d && d.byRef.has(focusRef) ? d.byRef.get(focusRef)[0] : null;
    this.request();
  }

  indicesOf(refs) {
    const out = [];
    for (const r of refs) { const l = this.data && this.data.byRef.get(r); if (l) { out.push(...l); } }
    return out;
  }

  /** Bring marks into view, zooming in only as far as they need and out when they do not fit. */
  revealIndex(indices, also) {
    if (!indices.length && !(also && also.length)) { return; }
    let lo = Infinity, hi = -Infinity;
    for (const t of also || []) { lo = Math.min(lo, t); hi = Math.max(hi, t); }
    for (const i of indices) {
      const t = this.timeOf(i), e = this.clock === "sim" ? this.data.simEnd[i] : this.data.realEnd[i];
      lo = Math.min(lo, t); hi = Math.max(hi, isNaN(e) ? t : e);
    }
    const w = this.win[1] - this.win[0], need = hi - lo;
    const least = this.clock === "sim" ? 6 * 36e5 : 10000;
    let width = w;
    if (need * 1.2 > w || w > Math.max(need * 40, least * 40)) { width = Math.max(need * 1.5, least); }
    const inside = lo >= this.win[0] + w * 0.05 && hi <= this.win[1] - w * 0.05;
    if (inside && width === w) { this.request(); return; }
    const mid = (lo + hi) / 2;
    this.setWindow(mid - width / 2, mid + width / 2, true);
  }

  /** Bring the marks of `refs` into view, and the moments `also` (simulated) with them. */
  reveal(refs, also) { this.revealIndex(this.indicesOf(refs), (also || []).map((t) => (this.clock === "sim" ? t : this.toClock(t, "real")))); }

  setFilter(text) {
    this.filter = text;
    if (this.filterEl.value !== text) { this.filterEl.value = text; }
    if (this.data) { this.index(); this.renderLabels(); this.request(); }
  }

  /** The next (dir 1) or previous (-1) mark after the focused one, in time, on the lanes shown. */
  step(dir) {
    const d = this.data;
    if (!d || !d.n) { return null; }
    const all = d.order[this.clock].all, match = this.matcher();
    let at = this.focus === null ? -1 : all.indexOf(this.focus);
    if (at < 0) {
      const mid = (this.win[0] + this.win[1]) / 2, t = this.clock === "sim" ? d.sim : d.real;
      at = dir > 0 ? -1 : all.length;
      for (let k = 0; k < all.length; k++) { if (t[all[k]] >= mid) { at = dir > 0 ? k - 1 : k; break; } }
    }
    for (let k = at + dir; k >= 0 && k < all.length; k += dir) {
      const i = all[k];
      if (this.hidden.has(d.lanes[d.lane[i]].key) || (match && !match(i))) { continue; }
      return d.ref[i];
    }
    return null;
  }

  toggleLane(key) {
    if (this.hidden.has(key)) { this.hidden.delete(key); } else { this.hidden.add(key); }
    this.renderLabels();
    this.request();
  }

  renderLabels() {
    const d = this.data;
    const rows = ['<div class="axis-cell" style="height:' + AXIS + 'px">' + (this.clock === "sim" ? "UTC, simulated" : "UTC, real") + "</div>",
      '<div style="height:' + ROW + 'px"><span>Findings</span><span class="c">' + d.findings.length + "</span></div>"];
    const counts = d.order[this.clock].map((o) => o.idx.length);
    d.lanes.forEach((l, i) => {
      const off = this.hidden.has(l.key);
      rows.push('<div style="height:' + ROW + "px" + (off ? ";opacity:.4" : "") + '" data-lane="' + esc(l.key) + '" title="' + esc(l.label + ": " + plural(l.marks, "mark") + ". Click to " + (off ? "show" : "hide")) + '">' +
        "<span>" + esc(l.label) + '</span><span class="c">' + count(this.filter ? counts[i] : l.marks) + "</span></div>");
    });
    this.labels.innerHTML = rows.join("");
    this.canvas.style.height = (AXIS + ROW * (d.lanes.length + 1)) + "px";
  }

  /* ---------- drawing ---------- */

  request() {
    if (this.dirty) { return; }
    this.dirty = true;
    requestAnimationFrame(() => { this.dirty = false; this.draw(); });
  }

  unitFor(pxPerMs) {
    for (const u of UNITS) { if (u[2] * pxPerMs >= 5) { return u; } }
    return UNITS[UNITS.length - 1];
  }

  draw() {
    const d = this.data;
    if (!d) { return; }
    const t0 = performance.now();
    const dpr = window.devicePixelRatio || 1, w = this.box.clientWidth, h = AXIS + ROW * (d.lanes.length + 1);
    if (w <= 0) { return; }
    const cv = this.canvas;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); cv.style.width = w + "px"; cv.style.height = h + "px";
    }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const [a, b] = this.win, k = w / (b - a), x = (t) => (t - a) * k;
    const C = { ink: css("--ink"), mark: css("--mark"), soft: css("--mark-soft"), faint: css("--faint"), line: css("--line"), band: css("--band"),
      heat: css("--heat"), fail: css("--fail"), review: css("--review"), info: css("--info"), select: css("--select"), paper: css("--paper"), muted: css("--muted") };
    g.font = "11px " + css("--sans");

    // axis and grid
    const scale = d3.scaleUtc().domain([new Date(a), new Date(b)]).range([0, w]);
    const ticks = scale.ticks(Math.max(2, Math.floor(w / 120)));
    g.fillStyle = C.muted; g.strokeStyle = C.line; g.lineWidth = 1;
    g.textBaseline = "middle";
    for (const tk of ticks) {
      const px = Math.round(scale(tk)) + 0.5;
      g.beginPath(); g.moveTo(px, AXIS - 6); g.lineTo(px, h); g.stroke();
      g.fillText(tickFormat(tk), px + 3, AXIS / 2 + 2);
    }
    g.strokeStyle = css("--line-strong"); g.beginPath(); g.moveTo(0, AXIS - 0.5); g.lineTo(w, AXIS - 0.5); g.stroke();
    // lane separators
    g.strokeStyle = C.line;
    for (let r = 1; r <= d.lanes.length + 1; r++) { const y = AXIS + r * ROW - 0.5; g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }

    // the run's own moments: deadline, split, end
    const moments = [];
    if (this.clock === "sim") {
      if (d.tl.deadline !== null) { moments.push([d.tl.deadline, "deadline"]); }
      if (d.tl.split !== null) { moments.push([d.tl.split, "split"]); }
      moments.push([d.tl.reached, "end"]);
    }
    g.setLineDash([4, 3]); g.strokeStyle = C.faint; g.fillStyle = C.faint;
    for (const [t, name] of moments) {
      const px = Math.round(x(t)) + 0.5;
      if (px < 0 || px > w) { continue; }
      g.beginPath(); g.moveTo(px, AXIS - 8); g.lineTo(px, h); g.stroke();
      g.fillText(name, px + 3, 7);
    }
    g.setLineDash([]);

    // the lanes
    const unit = this.unitFor(k);
    let binned = false, drawn = 0;
    this.layoutState = [];
    const match = this.matcher();
    d.lanes.forEach((lane, li) => {
      const y0 = AXIS + ROW * (li + 1), mid = y0 + ROW / 2;
      if (this.hidden.has(lane.key)) { this.layoutState[li] = { mode: "hidden" }; return; }
      const o = d.order[this.clock][li];
      // a range that began before the window still shows: look back by the longest range of the lane
      const lo = lowerBound(o.times, a - (this.clock === "sim" ? 0 : 0)), hi = upperBound(o.times, b);
      const visible = hi - lo;
      if (visible > w / DENSE) {
        binned = true;
        this.layoutState[li] = this.drawBins(g, o, unit, a, b, x, y0, C);
        drawn += this.layoutState[li].bins.length;
      } else {
        this.layoutState[li] = { mode: "marks", lo, hi, o };
        this.drawMarks(g, o, lo, hi, x, mid, y0, w, C, match);
        drawn += visible;
      }
    });
    // waits and other ranges that began before the window and run into it
    this.drawOpenRanges(g, x, w, C);

    // findings row: one by one when there is room, else one marker per bin in the worst colour it holds
    const fy = AXIS + ROW / 2;
    this.findingX = [];
    const shownF = [];
    for (const f of d.findings) {
      const t = this.clock === "sim" ? f.sim : this.toClock(f.sim, "real");
      if (t >= a - (b - a) * 0.01 && t <= b + (b - a) * 0.01) { shownF.push([t, f]); }
    }
    const triangle = (px, color, on) => {
      g.fillStyle = color;
      g.beginPath(); g.moveTo(px, fy + 6); g.lineTo(px - 5, fy - 5); g.lineTo(px + 5, fy - 5); g.closePath(); g.fill();
      if (on) { g.strokeStyle = C.select; g.lineWidth = 2; g.stroke(); g.lineWidth = 1; }
    };
    const colorOf = (kind) => (kind === "fail" ? C.fail : kind === "review" ? C.review : C.faint);
    if (shownF.length > w / 12) {
      const interval = unit[1](), groups = new Map();
      for (const [t, f] of shownF) { const key = +interval.floor(new Date(t)); if (!groups.has(key)) { groups.set(key, []); } groups.get(key).push(f); }
      for (const [key, fs] of groups) {
        const worst = fs.some((f) => f.kind === "fail") ? "fail" : fs.some((f) => f.kind === "review") ? "review" : "informational";
        const x0 = x(key), x1 = x(+interval.offset(new Date(key), 1)), px = (Math.max(x0, 0) + Math.min(x1, w)) / 2;
        triangle(px, colorOf(worst), fs.some((f) => this.selected.has("finding:" + f.number)));
        if (x1 - x0 > 30) { g.fillStyle = C.muted; g.fillText(String(fs.length), px + 7, fy); }
        this.findingX.push([px, fs[0], fs.length, key, +interval.offset(new Date(key), 1)]);
      }
    } else {
      for (const [t, f] of shownF) {
        const px = x(t);
        triangle(px, colorOf(f.kind), this.selected.has("finding:" + f.number));
        this.findingX.push([px, f, 1]);
      }
    }

    // the selection: a guide at the focused mark, and a ring on every selected mark
    const sel = this.indicesOf(this.selected);
    g.strokeStyle = C.select; g.fillStyle = C.select; g.lineWidth = 2;
    for (const i of sel) {
      const li = d.lane[i];
      if (this.hidden.has(d.lanes[li].key)) { continue; }
      const px = x(this.timeOf(i)), y = AXIS + ROW * (li + 1) + ROW / 2;
      if (px < -8 || px > w + 8) { continue; }
      g.beginPath(); g.arc(px, y, 6, 0, Math.PI * 2); g.stroke();
    }
    if (this.focus !== null) {
      const px = Math.round(x(this.timeOf(this.focus))) + 0.5;
      g.globalAlpha = 0.5; g.lineWidth = 1;
      g.beginPath(); g.moveTo(px, AXIS); g.lineTo(px, h); g.stroke();
      g.globalAlpha = 1;
    }
    g.lineWidth = 1;

    this.rangeEl.textContent = when(a) + " – " + when(b) + " · " + this.widthWords(b - a);
    this.noteEl.textContent = binned ? "dense lanes binned per " + unit[0] + "; zoom in for each mark" : "";
    this.drawMini(C);
    this.lastDraw = { ms: performance.now() - t0, drawn };
    if (this.hooks.onDraw) { this.hooks.onDraw(this.lastDraw); }
  }

  widthWords(msWide) {
    const s = msWide / 1000;
    if (s < 120) { return Math.round(s) + " s"; }
    if (s < 7200) { return Math.round(s / 60) + " min"; }
    if (s < 172800) { return Math.round(s / 3600) + " h"; }
    if (s < 86400 * 120) { return Math.round(s / 86400) + " days"; }
    return (s / (86400 * 30.44)).toFixed(1) + " months";
  }

  drawBins(g, o, unit, a, b, x, y0, C) {
    const interval = unit[1]();
    const edges = interval.range(interval.floor(new Date(a)), new Date(b + 1)).map(Number);
    edges.unshift(+interval.floor(new Date(a)));
    const bins = [];
    let most = 0;
    for (let i = 0; i < edges.length; i++) {
      const s = edges[i], e = i + 1 < edges.length ? edges[i + 1] : +interval.offset(new Date(s), 1);
      if (e <= s || (i > 0 && s === edges[i - 1])) { continue; }
      const c = lowerBound(o.times, e) - lowerBound(o.times, s);
      const f = o.failed.length ? lowerBound(o.failed, e) - lowerBound(o.failed, s) : 0;
      if (c) { bins.push({ s, e, c, f }); most = Math.max(most, c); }
    }
    const top = y0 + 3, hh = ROW - 6;
    for (const bn of bins) {
      const x0 = Math.max(x(bn.s), -1), x1 = Math.min(x(bn.e), x(b) + 1), wd = Math.max(1, x1 - x0 - 1);
      const shade = 0.1 + 0.55 * Math.log(1 + bn.c) / Math.log(1 + most);
      g.fillStyle = "rgba(" + C.heat + "," + shade.toFixed(3) + ")";
      g.fillRect(x0, top, wd, hh);
      if (bn.f) { g.fillStyle = C.fail; g.fillRect(x0, top + hh - 3, wd, 3); }
      if (wd >= 26) {
        g.fillStyle = shade > 0.45 ? C.paper : C.ink;
        g.fillText(count(bn.c), x0 + 3, top + hh / 2 + 1);
      }
      bn.x0 = x0; bn.x1 = x0 + wd;
    }
    return { mode: "bins", bins, unit: unit[0] };
  }

  drawMarks(g, o, lo, hi, x, mid, y0, w, C, match) {
    const d = this.data, endOf = this.clock === "sim" ? d.simEnd : d.realEnd;
    let lastX = -1e9, stack = 0;
    for (let k = lo; k < hi; k++) {
      const i = o.idx[k], kind = KINDS[d.kind[i]], px = x(o.times[k]);
      const e = endOf[i];
      // marks at one instant (a wake's work on the simulated clock) step down the lane a little
      stack = Math.abs(px - lastX) < 1 ? (stack + 1) % 4 : 0;
      lastX = px;
      const y = mid + (stack - 1.5) * 2;
      this.drawMark(g, kind, px, isNaN(e) ? null : x(e), y, y0, C, d.label[i]);
    }
  }

  drawMark(g, kind, px, pxEnd, y, y0, C, label) {
    const fail = FAILED.has(kind);
    if (pxEnd !== null && pxEnd - px > 2) {
      const tall = kind === "wait" ? 10 : kind === "span" ? 4 : 8;
      g.fillStyle = kind === "wait" ? C.band : fail ? C.fail : kind === "span" ? C.soft : C.mark;
      g.fillRect(px, y0 + (ROW - tall) / 2, pxEnd - px, tall);
      if (kind === "wait" && pxEnd - px > 60) {
        g.fillStyle = C.muted; g.save(); g.beginPath(); g.rect(px, y0, pxEnd - px, ROW); g.clip();
        g.fillText(label, px + 4, y0 + ROW / 2 + 1); g.restore();
      }
      return;
    }
    g.fillStyle = fail ? C.fail : C.mark; g.strokeStyle = fail ? C.fail : C.mark; g.lineWidth = 1.5;
    switch (kind) {
      case "wake": g.fillRect(px - 1.5, y0 + 4, 3, ROW - 8); break;
      case "sent": g.beginPath(); g.arc(px, y, 3.5, 0, Math.PI * 2); g.fill(); break;
      case "said": g.beginPath(); g.arc(px, y, 3.2, 0, Math.PI * 2); g.stroke(); break;
      case "edited": g.beginPath(); g.moveTo(px, y - 4.5); g.lineTo(px + 4.5, y); g.lineTo(px, y + 4.5); g.lineTo(px - 4.5, y); g.closePath(); g.fill(); break;
      case "wait": g.fillStyle = C.band; g.fillRect(px - 1, y0 + 6, 3, ROW - 12); break;
      case "call": case "call_failed": g.fillRect(px - 1, y0 + 6, 2, ROW - 12); break;
      case "change": g.strokeRect(px - 3, y - 3, 6, 6); break;
      case "read": g.fillStyle = C.soft; g.fillRect(px - 1, y - 1, 2, 2); break;
      case "due": case "due_fault": g.beginPath(); g.moveTo(px, y - 5); g.lineTo(px + 4, y + 3); g.lineTo(px - 4, y + 3); g.closePath(); g.fill(); break;
      case "model_call": g.fillRect(px - 3, y - 3, 6, 6); break;
      case "memory_write": g.fillRect(px - 1, y - 4, 2.5, 8); break;
      case "memory_read": g.fillStyle = C.soft; g.fillRect(px - 1, y - 1, 2, 2); break;
      case "stored": g.strokeRect(px - 3, y - 3, 6, 6); g.fillRect(px - 1, y - 1, 2, 2); break;
      default: g.fillStyle = C.soft; g.fillRect(px - 1, y - 3, 2, 6);
    }
    g.lineWidth = 1;
  }

  /** A range that began before the window (a wait, a wake on the real clock) is drawn where it shows. */
  drawOpenRanges(g, x, w, C) {
    const d = this.data, t = this.clock === "sim" ? d.sim : d.real, e = this.clock === "sim" ? d.simEnd : d.realEnd;
    const [a] = this.win;
    d.lanes.forEach((lane, li) => {
      const st = this.layoutState[li];
      if (!st || st.mode !== "marks") { return; }
      const o = st.o;
      for (let k = 0; k < st.lo; k++) {
        const i = o.idx[k];
        if (!isNaN(e[i]) && e[i] > a) { this.drawMark(g, KINDS[d.kind[i]], x(t[i]), x(e[i]), AXIS + ROW * (li + 1) + ROW / 2, AXIS + ROW * (li + 1), C, d.label[i]); }
      }
    });
  }

  drawMini(C) {
    const d = this.data, cv = this.mini, dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if (w <= 0) { return; }
    if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const [lo, hi] = d.extent, k = w / (hi - lo);
    if (!this.miniBins || this.miniBins.key !== this.clock + w + this.filter + d.n) {
      const n = Math.max(20, Math.floor(w / 3)), bins = new Uint32Array(n), t = this.clock === "sim" ? d.sim : d.real;
      const match = this.matcher();
      for (let i = 0; i < d.n; i++) { if (match && !match(i)) { continue; } const b = Math.floor((t[i] - lo) / (hi - lo) * n); if (b >= 0 && b < n) { bins[b]++; } }
      this.miniBins = { key: this.clock + w + this.filter + d.n, bins, n, most: Math.max(1, ...bins) };
    }
    const mb = this.miniBins, bw = w / mb.n;
    g.fillStyle = C.soft;
    for (let i = 0; i < mb.n; i++) {
      if (!mb.bins[i]) { continue; }
      const bh = Math.max(1, Math.sqrt(mb.bins[i] / mb.most) * (h - 8));
      g.fillRect(i * bw, h - bh, Math.max(1, bw - 0.5), bh);
    }
    for (const f of d.findings) {
      if (f.kind === "informational") { continue; }
      const t = this.clock === "sim" ? f.sim : this.toClock(f.sim, "real");
      g.fillStyle = f.kind === "fail" ? C.fail : C.review;
      g.fillRect((t - lo) * k - 1, 0, 2, 6);
    }
    const x0 = (this.win[0] - lo) * k, x1 = (this.win[1] - lo) * k;
    g.fillStyle = css("--select-soft"); g.fillRect(x0, 0, Math.max(2, x1 - x0), h);
    g.strokeStyle = C.select; g.lineWidth = 1.5; g.strokeRect(x0 + 0.5, 0.5, Math.max(2, x1 - x0) - 1, h - 1);
    this.miniGeom = { lo, hi, k, x0, x1 };
  }

  /* ---------- pointer ---------- */

  /** What lies under a point of the canvas: a finding, a bin, or the marks within a few pixels. */
  hit(px, py) {
    const d = this.data;
    if (!d || py < AXIS) { return null; }
    const row = Math.floor((py - AXIS) / ROW);
    if (row === 0) {
      let best = null;
      for (const item of this.findingX || []) { if (Math.abs(item[0] - px) <= 7 && (!best || Math.abs(item[0] - px) < Math.abs(best[0] - px))) { best = item; } }
      if (!best) { return null; }
      return best[2] > 1 ? { findings: best[2], from: best[3], to: best[4], finding: best[1] } : { finding: best[1] };
    }
    const li = row - 1, st = this.layoutState[li];
    if (!st || st.mode === "hidden") { return null; }
    if (st.mode === "bins") {
      const bn = st.bins.find((b) => px >= b.x0 - 1 && px <= b.x1 + 1);
      return bn ? { lane: li, bin: bn, unit: st.unit } : null;
    }
    const [a, b] = this.win, k = this.box.clientWidth / (b - a), t = a + px / k, r = 6 / k;
    const o = st.o, lo = lowerBound(o.times, t - r), hi = upperBound(o.times, t + r);
    const near = [];
    for (let q = lo; q < hi; q++) { near.push(o.idx[q]); }
    // a range under the pointer counts too
    const e = this.clock === "sim" ? d.simEnd : d.realEnd;
    if (!near.length) {
      for (let q = 0; q < lowerBound(o.times, t); q++) { const i = o.idx[q]; if (!isNaN(e[i]) && e[i] >= t) { near.push(i); } }
    }
    near.sort((p, q2) => Math.abs(this.timeOf(p) - t) - Math.abs(this.timeOf(q2) - t));
    return near.length ? { lane: li, marks: near } : null;
  }

  tooltipFor(h) {
    const d = this.data;
    if (h.findings) {
      return '<div class="tt-head">' + plural(h.findings, "finding") + "</div><div class=\"tt-sub\">" + esc(when(h.from) + " – " + when(h.to)) + "</div><div class=\"tt-sub\">Click to zoom in</div>";
    }
    if (h.finding) {
      const x = d.explained.find((e) => e.number === h.finding.number);
      return '<div class="tt-head">' + esc((x && x.pattern ? x.pattern.title : words(x ? x.finding.check : "finding"))) + "</div><div>" + esc(x ? x.finding.message : "") + '</div><div class="tt-sub">' + esc(h.finding.kind) + " · " + when(h.finding.sim) + "</div>";
    }
    if (h.bin) {
      const o = d.order[this.clock][h.lane], lo = lowerBound(o.times, h.bin.s), hi = lowerBound(o.times, h.bin.e);
      const kinds = {};
      for (let q = lo; q < Math.min(hi, lo + 20000); q++) { const kk = KINDS[d.kind[o.idx[q]]]; kinds[kk] = (kinds[kk] || 0) + 1; }
      return '<div class="tt-head">' + esc(periodWords(h.unit, h.bin.s)) + " · " + esc(d.lanes[h.lane].label) + "</div>" +
        "<div>" + Object.keys(kinds).map((kk) => esc(plural(kinds[kk], KIND_WORD[kk]))).join(", ") + "</div>" +
        (h.bin.f ? '<div>' + plural(h.bin.f, "failed") + "</div>" : "") + '<div class="tt-sub">Click to zoom into it</div>';
    }
    const shown = h.marks.slice(0, 6).map((i) =>
      "<div>" + esc(d.label[i]) + '</div><div class="tt-sub">' + esc(KIND_WORD[KINDS[d.kind[i]]]) + " · " + when(d.sim[i], { seconds: true }) +
      (this.clock === "real" ? " · real " + realClock(d.real[i]) : "") + "</div>").join("");
    return shown + (h.marks.length > 6 ? '<div class="tt-sub">and ' + (h.marks.length - 6) + " more here; click again for the next</div>" : h.marks.length > 1 ? '<div class="tt-sub">' + h.marks.length + " at this moment; click again for the next</div>" : "");
  }

  showTip(html, cx, cy) {
    const tt = this.tooltip;
    tt.innerHTML = html; tt.hidden = false;
    const r = tt.getBoundingClientRect();
    let left = cx + 14, top = cy + 14;
    if (left + r.width > window.innerWidth - 8) { left = cx - r.width - 14; }
    if (top + r.height > window.innerHeight - 8) { top = cy - r.height - 14; }
    tt.style.left = left + "px"; tt.style.top = top + "px";
  }
  hideTip() { this.tooltip.hidden = true; }

  bind() {
    const cv = this.canvas;
    let drag = null;
    const local = (ev) => { const r = cv.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; };
    cv.addEventListener("mousedown", (ev) => {
      if (ev.button !== 0 || !this.data) { return; }
      drag = { x: ev.clientX, win: this.win.slice(), moved: false };
    });
    window.addEventListener("mousemove", (ev) => {
      if (drag) {
        const dx = ev.clientX - drag.x;
        if (Math.abs(dx) > 3) { drag.moved = true; cv.classList.add("dragging"); this.hideTip(); }
        if (drag.moved) {
          const msPerPx = (drag.win[1] - drag.win[0]) / this.box.clientWidth;
          this.setWindow(drag.win[0] - dx * msPerPx, drag.win[1] - dx * msPerPx, false);
        }
        return;
      }
      if (ev.target !== cv || !this.data) { return; }
      const [px, py] = local(ev), h = this.hit(px, py);
      if (h) { this.showTip(this.tooltipFor(h), ev.clientX, ev.clientY); cv.style.cursor = "pointer"; } else { this.hideTip(); cv.style.cursor = ""; }
    });
    window.addEventListener("mouseup", (ev) => {
      if (!drag) { return; }
      const was = drag;
      drag = null; cv.classList.remove("dragging");
      if (was.moved || ev.target !== cv) { return; }
      const [px, py] = local(ev), h = this.hit(px, py);
      if (!h) { return; }
      if (h.findings) { this.setWindow(h.from, h.to, true); return; }
      if (h.finding) { this.hooks.onSelect("finding:" + h.finding.number); return; }
      if (h.bin) { const pad = (h.bin.e - h.bin.s) * 0.05; this.setWindow(h.bin.s - pad, h.bin.e + pad, true); return; }
      const key = h.lane + ":" + Math.round(px);
      const at = this.cycle && this.cycle.key === key ? (this.cycle.at + 1) % h.marks.length : 0;
      this.cycle = { key, at };
      this.hooks.onSelect(this.data.ref[h.marks[at]]);
    });
    cv.addEventListener("mouseleave", () => this.hideTip());
    cv.addEventListener("wheel", (ev) => {
      if (!this.data) { return; }
      const horizontal = ev.shiftKey || Math.abs(ev.deltaX) > Math.abs(ev.deltaY);
      if (!horizontal && !ev.ctrlKey && !ev.metaKey && !ev.altKey && this.isVerticalScroll(ev)) { return; }
      ev.preventDefault();
      const [px] = local(ev), w = this.box.clientWidth;
      if (horizontal) { this.pan((ev.shiftKey ? ev.deltaY : ev.deltaX) / w); return; }
      const around = this.win[0] + (px / w) * (this.win[1] - this.win[0]);
      this.zoom(Math.exp(ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0018)), around);
    }, { passive: false });
    this.labels.addEventListener("click", (ev) => {
      const t = ev.target.closest("[data-lane]");
      if (t) { this.toggleLane(t.getAttribute("data-lane")); }
    });
    this.root.querySelector(".tl-tools").addEventListener("click", (ev) => {
      const p = ev.target.closest("[data-preset]"), z = ev.target.closest("[data-zoom]"), c = ev.target.closest("[data-clock]");
      if (p) { this.preset(p.getAttribute("data-preset")); }
      if (z) { this.zoom(z.getAttribute("data-zoom") === "in" ? 0.5 : 2); }
      if (c) { this.setClock(c.getAttribute("data-clock")); this.announce(); }
    });
    let typing = null;
    this.filterEl.addEventListener("input", () => {
      clearTimeout(typing);
      typing = setTimeout(() => { this.setFilter(this.filterEl.value); if (this.hooks.onFilter) { this.hooks.onFilter(this.filter); } }, 120);
    });
    // the minimap: drag the window, or drag out a new one
    let mdrag = null;
    this.mini.addEventListener("mousedown", (ev) => {
      if (!this.miniGeom) { return; }
      const r = this.mini.getBoundingClientRect(), px = ev.clientX - r.left, gm = this.miniGeom;
      const inside = px >= gm.x0 - 2 && px <= gm.x1 + 2;
      mdrag = inside ? { mode: "move", x: px, win: this.win.slice() } : { mode: "draw", x: px };
      ev.preventDefault();
    });
    window.addEventListener("mousemove", (ev) => {
      if (!mdrag) { return; }
      const r = this.mini.getBoundingClientRect(), px = ev.clientX - r.left, gm = this.miniGeom;
      if (mdrag.mode === "move") {
        const dt = (px - mdrag.x) / gm.k;
        this.setWindow(mdrag.win[0] + dt, mdrag.win[1] + dt, false);
      } else if (Math.abs(px - mdrag.x) > 3) {
        const a = gm.lo + Math.min(px, mdrag.x) / gm.k, b = gm.lo + Math.max(px, mdrag.x) / gm.k;
        this.setWindow(a, b, false);
      }
    });
    window.addEventListener("mouseup", (ev) => {
      if (!mdrag) { return; }
      const r = this.mini.getBoundingClientRect(), px = ev.clientX - r.left, gm = this.miniGeom;
      if (mdrag.mode === "draw" && Math.abs(px - mdrag.x) <= 3) {
        const c = gm.lo + px / gm.k, wd = this.win[1] - this.win[0];
        this.setWindow(c - wd / 2, c + wd / 2, true);
      }
      mdrag = null;
    });
  }

  /** A plain vertical wheel over a timeline whose lanes overflow scrolls the lanes; everywhere else it zooms. */
  isVerticalScroll() {
    const main = this.root.querySelector('[data-role="main"]');
    return main.scrollHeight > main.clientHeight + 2;
  }
}
