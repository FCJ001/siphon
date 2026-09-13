/* app-1-core.js —— API 封装 / 主题 / 页面路由 / SSE / 格式化 / 弹层 */
"use strict";

const API = {
  get token() { return localStorage.getItem("siphon_token") || ""; },
  login(pw) { return this._raw("POST", "/api/login", { password: pw }); },
  logout() { localStorage.removeItem("siphon_token"); location.href = "/login"; },
  async _raw(method, path, body) {
    const opt = { method, headers: {} };
    if (this.token) opt.headers.Authorization = "Bearer " + this.token;
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    const j = await r.json().catch(() => ({ code: r.status, message: r.statusText }));
    if (r.status === 401 && !path.startsWith("/api/login")) { location.href = "/login"; }
    return j;
  },
  get(path) { return this._raw("GET", path); },
  post(path, body) { return this._raw("POST", path, body); },
  patch(path, body) { return this._raw("PATCH", path, body); },
  del(path) { return this._raw("DELETE", path); },
};

const Fmt = {
  usd(n, d = 2) { return "$" + (Number(n) || 0).toFixed(d); },
  tok(n) {
    n = Number(n) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  },
  pct(n) { return (Number(n) || 0).toFixed(1) + "%"; },
  time(ts) {
    if (!ts) return "—";
    const d = new Date(ts > 1e11 ? ts : ts * 1000);
    const p = x => String(x).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  },
  clock(ts) { return this.time(ts).slice(9); },
  dur(ms) { return ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms"; },
  countdown(iso) {
    if (!iso) return "";
    const ms = new Date(iso).getTime() - Date.now();
    if (ms <= 0) return "已重置";
    const h = Math.floor(ms / 3600000), m = Math.floor(ms % 3600000 / 60000);
    return h ? `${h}小时${m}分后` : `${m}分后`;
  },
  esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); },
};

/* 主题(与投研终端同机制) */
(function () {
  try {
    let t = localStorage.getItem("theme");
    if (!t) t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* 静默 */ }
})();
const Theme = {
  toggle() {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
    if (window.Pages) Pages.rerender();
  },
};

/* 页面路由 */
const Pages = {
  current: "overview",
  handlers: {},
  on(name, fn) { this.handlers[name] = fn; },
  show(name) {
    this.current = name;
    document.querySelectorAll(".page-switch button").forEach(b =>
      b.classList.toggle("active", b.dataset.page === name));
    document.querySelectorAll("main .page").forEach(p =>
      p.classList.toggle("active", p.id === "page-" + name));
    this.rerender();
  },
  rerender() { const fn = this.handlers[this.current]; if (fn) fn(); },
};

/* SSE(断线降级提示) */
const SSE = {
  src: null, handlers: {}, live: false,
  on(topic, fn) { this.handlers[topic] = fn; },
  connect() {
    if (this.src) this.src.close();
    const url = "/api/events/stream?token=" + encodeURIComponent(API.token);
    this.src = new EventSource(url);
    this.src.onopen = () => { this.live = true; this._dot(); };
    this.src.onerror = () => {
      if (this.live) { this.live = false; this._dot(); }
    };
    this.src.onmessage = ev => {
      try {
        const item = JSON.parse(ev.data);
        const fn = this.handlers[item.topic];
        if (fn) fn(item.payload);
      } catch (e) { /* 忽略坏帧 */ }
    };
  },
  _dot() {
    const d = document.getElementById("sse-dot");
    if (!d) return;
    d.className = "dot " + (this.live ? "live" : "off");
    d.title = this.live ? "实时已连接" : "实时断开(自动重连)";
  },
};

/* 弹层 / 轻提示 */
const UI = {
  open(id) { document.getElementById("mask").classList.add("open");
             document.getElementById(id).classList.add("open"); },
  closeAll() { document.querySelectorAll(".modal.open").forEach(m => m.classList.remove("open"));
               document.getElementById("mask").classList.remove("open"); },
  toast(text, bad = false) {
    let t = document.getElementById("toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
    t.textContent = text;
    Object.assign(t.style, { position: "fixed", zIndex: 70, bottom: "26px", left: "50%",
      transform: "translateX(-50%)", background: bad ? "var(--danger)" : "var(--text)",
      color: bad ? "#fff" : "var(--bg)", padding: "8px 16px", borderRadius: "8px",
      fontSize: "13px", boxShadow: "var(--shadow)" });
    clearTimeout(t._h); t.style.display = "block";
    t._h = setTimeout(() => { t.style.display = "none"; }, 2600);
  },
};
window.addEventListener("keydown", e => { if (e.key === "Escape") UI.closeAll(); });
