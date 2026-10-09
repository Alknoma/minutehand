/* Small things every part of the page uses: escaping, building elements, and saying times, spans and counts the
   way the rest of Minutehand says them (UTC, the simulated clock unless named otherwise). */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function esc(s) {
  return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export function $(id) { return document.getElementById(id); }

export function words(name) { return String(name).replace(/_/g, " "); }
export function title(name) { return words(name).replace(/^\w/, (c) => c.toUpperCase()); }

export function pad(n) { return String(n).padStart(2, "0"); }

export function ms(iso) { return typeof iso === "number" ? iso : Date.parse(iso); }

/** "24 Aug 2026 09:00" in UTC; `seconds` adds them; the year is left out when `year` is false. */
export function when(t, { seconds = false, year = true } = {}) {
  const d = new Date(ms(t));
  return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + (year ? " " + d.getUTCFullYear() : "") + " " +
    pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + (seconds ? ":" + pad(d.getUTCSeconds()) : "");
}

export function clock(t) {
  const d = new Date(ms(t));
  return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()) + "." + String(d.getUTCMilliseconds()).padStart(3, "0");
}

/** A stretch of time in the largest units that say it: "3 d 4 h", "12 min", "850 ms". */
export function span(secs) {
  if (secs === null || secs === undefined || isNaN(secs)) { return "—"; }
  const neg = secs < 0; secs = Math.abs(secs);
  let out;
  if (secs < 1) { out = Math.round(secs * 1000) + " ms"; }
  else if (secs < 60) { out = (secs < 10 ? secs.toFixed(1) : Math.round(secs)) + " s"; }
  else if (secs < 3600) { out = Math.floor(secs / 60) + " min" + (secs % 60 >= 1 ? " " + Math.round(secs % 60) + " s" : ""); }
  else if (secs < 86400) { out = Math.floor(secs / 3600) + " h" + (secs % 3600 >= 60 ? " " + Math.round((secs % 3600) / 60) + " min" : ""); }
  else if (secs < 86400 * 60) { out = Math.floor(secs / 86400) + " d" + (secs % 86400 >= 3600 ? " " + Math.round((secs % 86400) / 3600) + " h" : ""); }
  else if (secs < 86400 * 730) { out = (secs / (86400 * 30.44)).toFixed(1) + " months"; }
  else { out = (secs / (86400 * 365.25)).toFixed(1) + " years"; }
  return (neg ? "−" : "") + out;
}

/** An ISO 8601 duration as pydantic writes a timedelta ("P1Y35D", "P1DT2H", "PT36H", "PT0.5S") in seconds: its year
    is 365 days, as it counts them. */
export function seconds(duration) {
  const m = /^(-)?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$/.exec(duration || "");
  if (!m) { return null; }
  const days = (+(m[2] || 0)) * 365 + (+(m[3] || 0)) * 30 + (+(m[4] || 0)) * 7 + (+(m[5] || 0));
  const s = days * 86400 + (+(m[6] || 0)) * 3600 + (+(m[7] || 0)) * 60 + (+(m[8] || 0));
  return m[1] ? -s : s;
}

export function count(n) {
  if (n === null || n === undefined) { return "—"; }
  if (n >= 1e6) { return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M"; }
  if (n >= 1e4) { return Math.round(n / 1e3) + "k"; }
  if (n >= 1e3) { return (n / 1e3).toFixed(1) + "k"; }
  return String(n);
}

export function plural(n, one, many) { return n + " " + (n === 1 ? one : many || one + "s"); }

export function clip(s, n) { s = String(s); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

export function median(values) {
  if (!values.length) { return null; }
  const v = values.slice().sort((a, b) => a - b), m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

/** JSON text laid out and marked up for reading; anything that is not JSON comes back escaped as it was. */
export function prettyJson(text) {
  if (text === null || text === undefined) { return ""; }
  let s = String(text);
  try { s = JSON.stringify(JSON.parse(s), null, 2); } catch (e) { return esc(s); }
  return esc(s).replace(/(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (all, str, colon, lit, num) => str ? (colon ? '<span class="k">' + str + "</span>" + colon : '<span class="s">' + str + "</span>")
      : lit ? '<span class="b">' + lit + "</span>" : '<span class="n">' + num + "</span>");
}

export function isJson(text) {
  if (typeof text !== "string") { return false; }
  const t = text.trim();
  if (t[0] !== "{" && t[0] !== "[") { return false; }
  try { JSON.parse(t); return true; } catch (e) { return false; }
}

/** Two texts as a line diff (longest common subsequence), each line kept, added or deleted. */
export function diffLines(a, b) {
  const x = a.split("\n"), y = b.split("\n");
  if (x.length * y.length > 4e6) { return null; }
  const n = x.length, m = y.length, t = new Array(n + 1);
  for (let i = 0; i <= n; i++) { t[i] = new Uint32Array(m + 1); }
  for (let i = n - 1; i >= 0; i--) { for (let j = m - 1; j >= 0; j--) { t[i][j] = x[i] === y[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1]); } }
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push(["same", x[i]]); i++; j++; }
    else if (t[i + 1][j] >= t[i][j + 1]) { out.push(["del", x[i++]]); }
    else { out.push(["add", y[j++]]); }
  }
  while (i < n) { out.push(["del", x[i++]]); }
  while (j < m) { out.push(["add", y[j++]]); }
  return out;
}

export function layout(text) {
  if (text === null || text === undefined) { return ""; }
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (e) { return String(text); }
}

/** Read a per-viewer preference; storage may be missing or refuse. */
export function remembered(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; }
}
export function remember(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* a private window: kept for this page only */ }
}
