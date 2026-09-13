/* app-2-overview.js —— 总览: KPI + 节省瀑布 + 配额环 + 模型分布 + 实时流 */
"use strict";
(() => {
  let range = "today";
  let lastOverview = null;

  Pages.on("overview", render);

  async function render() {
    const r = await API.get("/api/overview?range=" + range);
    if (r.code !== 0) return;
    lastOverview = r.data;
    renderKpis(r.data.totals);
    renderWaterfall();
    renderRings(r.data.accounts || []);
    renderModels(r.data.totals.by_model || []);
    const [ts] = await Promise.all([API.get("/api/analytics/timeseries?metric=tokens&granularity=hour&hours=" + (range === "today" ? 24 : range === "7d" ? 168 : 720))]);
    renderTrend(ts.data?.items || []);
  }

  function renderKpis(t) {
    const el = document.getElementById("kpis");
    el.innerHTML = `
      <div class="card kpi"><span>实际成本(${rangeName()})</span>
        <b>${Fmt.usd(t.cost_actual)}</b>
        <span class="sub">基线 ${Fmt.usd(t.cost_baseline)} · ${Fmt.tok(t.prompt_tokens + t.completion_tokens)} tok</span></div>
      <div class="card kpi"><span>已节省</span>
        <b class="up">${Fmt.usd(t.cost_baseline - t.cost_actual)}
          <span class="sub">(${t.savings_pct}%)</span></b>
        <span class="sub">缓存 ${Fmt.usd(t.save_cache)} · 回放 ${Fmt.usd(t.save_replay)}</span></div>
      <div class="card kpi"><span>缓存命中率</span>
        <b>${Fmt.pct(t.cache_hit_rate)}</b>
        <span class="sub">命中 ${Fmt.tok(t.cached_tokens)} / 输入 ${Fmt.tok(t.prompt_tokens)}</span></div>
      <div class="card kpi"><span>请求数</span>
        <b>${Fmt.tok(t.requests)}</b>
        <span class="sub">失败率 ${t.failure_rate}% · 回放 ${t.replays} 次</span></div>`;
  }

  function rangeName() { return { today: "今日", "24h": "24h", "7d": "7日", "30d": "30日" }[range] || range; }

  async function renderWaterfall() {
    const r = await API.get("/api/analytics/savings-attribution?range=" + range);
    if (r.code !== 0) return;
    const d = r.data;
    Chart.waterfall(document.getElementById("wf-chart"), {
      baseline: d.baseline, actual: d.actual, parts: d.parts,
      savings_pct: d.savings_pct,
    });
    document.getElementById("wf-note").textContent = d.methodology || "";
  }

  function renderRings(list) {
    const el = document.getElementById("rings");
    if (!list.length) { el.innerHTML = '<div class="empty">尚无账号 — 到「账号」页添加 OpenCode Go key</div>'; return; }
    el.innerHTML = list.map(a => {
      const st = a.breaker?.account_open_until > 0
        ? '<span class="badge danger">熔断中</span>'
        : (a.enabled ? "" : '<span class="badge off">已禁用</span>');
      const w = a.resets || {};
      const tip = Object.entries(w).map(([k, v]) => `${k}: ${Fmt.countdown(v)}`).join(" · ");
      return `<div class="card" style="display:flex;gap:14px;align-items:center" title="${Fmt.esc(tip)}">
        <div id="ring-${a.id}"></div>
        <div style="min-width:0">
          <b>${Fmt.esc(a.alias || "账号" + a.id)} <span class="tag">…${Fmt.esc(a.key_tail)}</span></b> ${st}
          <div class="tag" style="font-size:11px">在途 ${a.inflight}/${a.inflight_cap} · 权重 ${a.weight}</div>
          <div class="tag" style="font-size:11px">已用 ${Fmt.pct(a.used_pct)} ${a.primary ? "· 主力" : ""}</div>
        </div></div>`;
    }).join("");
    list.forEach(a => Chart.donut(document.getElementById("ring-" + a.id),
      { pct: a.used_pct, size: 78 }));
  }

  let modelMetric = "cost";
  function renderModels(byModel) {
    const costSorted = [...byModel].sort((a, b) =>
      (modelMetric === "cost" ? b.cost_actual - a.cost_actual : b.requests - a.requests));
    Chart.hbar(document.getElementById("models-chart"), {
      items: costSorted.map(m => ({
        label: m.model || "(未知)",
        value: modelMetric === "cost" ? Number(m.cost_actual) : Number(m.requests),
        tip: `请求 ${m.requests} · 输入 ${Fmt.tok(m.prompt_tokens)}(缓存 ${Fmt.tok(m.cached_tokens)}) · 输出 ${Fmt.tok(m.completion_tokens)} · ${Fmt.usd(m.cost_actual)}`,
      })),
      fmt: modelMetric === "cost" ? (v => Fmt.usd(v)) : (v => v + " 次"),
    });
  }

  function renderTrend(items) {
    const labels = items.map(r => (r.bucket || "").slice(5, 13));
    const pick = k => items.map(r => Number(r[k]) || 0);
    Chart.line(document.getElementById("trend-chart"), {
      labels,
      stacked: true,
      series: [
        { name: "输入(未命中)", values: pick("input_uncached") },
        { name: "输入(命中缓存)", values: pick("input_cached") },
        { name: "输出", values: pick("output") },
      ],
      yFmt: v => Fmt.tok(v),
      tipFmt: v => Fmt.tok(v),
    });
  }

  /* ---- 实时流 ---- */
  function pushFeed(p) {
    const el = document.getElementById("feed");
    const div = document.createElement("div");
    const effort = p.effort && p.effort[0] !== p.effort[1]
      ? `<span class="hi">effort ${Fmt.esc(p.effort[0] || "—")}→${Fmt.esc(p.effort[1] || "—")}</span>` : "";
    const replay = p.replay_hit ? '<span class="hi">↩replay</span>' : "";
    div.innerHTML = `<span class="t">${Fmt.clock(Date.now() / 1000)}</span>` +
      `<b>${Fmt.esc(p.model || "?")}</b> <span class="tag">${Fmt.esc(p.account || "—")}</span>` +
      ` in ${Fmt.tok(p.prompt_tokens)}(缓存${Fmt.tok(p.cached_tokens)}) out ${Fmt.tok(p.completion_tokens)} ` +
      `${effort}${replay} ${Fmt.dur(p.latency_ms || 0)}` +
      (p.status === "ok" ? "" : ` <span class="badge danger">${Fmt.esc(p.status)}</span>`);
    el.prepend(div);
    while (el.children.length > 40) el.lastChild.remove();
  }

  /* ---- 事件绑定(一次) ---- */
  let bound = false;
  function bind() {
    if (bound) return;
    bound = true;
    document.querySelectorAll("#range-seg button").forEach(b =>
      b.onclick = () => {
        range = b.dataset.range;
        document.querySelectorAll("#range-seg button").forEach(x =>
          x.classList.toggle("active", x === b));
        render();
      });
    document.getElementById("model-metric").onchange = e => {
      modelMetric = e.target.value;
      if (lastOverview) renderModels(lastOverview.totals.by_model || []);
    };
    SSE.on("request", pushFeed);
    SSE.on("quota", () => render());
  }
  document.addEventListener("DOMContentLoaded", bind);
})();
