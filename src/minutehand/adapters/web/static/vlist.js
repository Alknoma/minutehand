/* A virtualised list: only the rows in view (and a few either side) are in the document, so a table of a
   hundred thousand calls scrolls as one of ten. Rows are one height; the header sorts by a column. */

import { esc } from "./util.js";

const HEAD = 28, OVERSCAN = 8;

export class VList {
  /**
   * columns: [{ label, width, align, html(row), sort(row) }] for a table; none for a list drawn by `rowHtml`.
   * rowHeight, key(row), onPick(row), rowHtml(row, selected) for a list.
   */
  constructor(host, options) {
    this.o = Object.assign({ rowHeight: 30, columns: null }, options);
    this.rows = []; this.shown = []; this.selected = null; this.sort = null;
    host.innerHTML = '<div class="vt" tabindex="0">' + (this.o.columns ? '<div class="vt-head"></div>' : "") + '<div class="vt-spacer"></div><div class="vt-rows"></div></div>';
    this.el = host.querySelector(".vt");
    this.head = host.querySelector(".vt-head");
    this.spacer = host.querySelector(".vt-spacer");
    this.box = host.querySelector(".vt-rows");
    this.offset = this.o.columns ? HEAD : 0;
    if (this.o.columns) {
      this.template = this.o.columns.map((c) => c.width || "1fr").join(" ");
      this.head.style.gridTemplateColumns = this.template;
      this.renderHead();
      this.head.addEventListener("click", (ev) => {
        const t = ev.target.closest("[data-col]");
        if (!t) { return; }
        const i = +t.getAttribute("data-col");
        this.sort = this.sort && this.sort.col === i ? { col: i, dir: -this.sort.dir } : { col: i, dir: 1 };
        this.renderHead();
        this.apply();
      });
    }
    let frame = 0;
    this.el.addEventListener("scroll", () => { if (!frame) { frame = requestAnimationFrame(() => { frame = 0; this.paint(); }); } });
    this.el.addEventListener("click", (ev) => {
      const t = ev.target.closest("[data-row]");
      if (t && this.o.onPick) { this.o.onPick(this.shown[+t.getAttribute("data-row")]); }
    });
    new ResizeObserver(() => this.paint()).observe(this.el);
  }

  renderHead() {
    this.head.innerHTML = this.o.columns.map((c, i) =>
      '<span data-col="' + i + '"' + (this.sort && this.sort.col === i ? ' data-dir="' + this.sort.dir + '"' : "") + (c.align === "r" ? ' style="text-align:right"' : "") + ">" + esc(c.label) + "</span>").join("");
  }

  setRows(rows) { this.rows = rows; this.apply(); }

  apply() {
    let shown = this.rows;
    if (this.sort) {
      const c = this.o.columns[this.sort.col], dir = this.sort.dir, by = c.sort || ((r) => c.html(r));
      shown = shown.slice().sort((a, b) => { const x = by(a), y = by(b); return (x < y ? -1 : x > y ? 1 : 0) * dir; });
    }
    this.shown = shown;
    this.spacer.style.height = (this.offset + shown.length * this.o.rowHeight) + "px";
    this.paint();
  }

  paint() {
    const rh = this.o.rowHeight, top = Math.max(0, this.el.scrollTop - this.offset), height = this.el.clientHeight;
    const first = Math.max(0, Math.floor(top / rh) - OVERSCAN), last = Math.min(this.shown.length, Math.ceil((top + height) / rh) + OVERSCAN);
    let html = "";
    for (let i = first; i < last; i++) {
      const row = this.shown[i], on = this.selected !== null && this.o.key(row) === this.selected;
      const y = this.offset + i * rh;
      if (this.o.columns) {
        html += '<div class="vt-row' + (on ? " on" : "") + '" data-row="' + i + '" style="top:' + y + "px;height:" + rh + "px;grid-template-columns:" + this.template + '">' +
          this.o.columns.map((c) => '<span class="' + (c.align === "r" ? "r" : "") + '">' + c.html(row) + "</span>").join("") + "</div>";
      } else {
        html += this.o.rowHtml(row, on, i, y, rh);
      }
    }
    this.box.innerHTML = html;
  }

  select(key, scroll) {
    this.selected = key;
    if (scroll) {
      const i = this.shown.findIndex((r) => this.o.key(r) === key);
      if (i >= 0) {
        const y = this.offset + i * this.o.rowHeight;
        if (y < this.el.scrollTop + this.offset || y > this.el.scrollTop + this.el.clientHeight - this.o.rowHeight) {
          this.el.scrollTop = Math.max(0, y - this.el.clientHeight / 3);
        }
      }
    }
    this.paint();
  }
}
