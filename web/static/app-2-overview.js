/* app-2-overview.js —— 总览: 读数条 + 节省瀑布 + 三窗水位计 + 模型分布 + 实时流 */
"use strict";
(() => {
  let range = "today";
  let lastOverview = null;

  Pages.on("overview", render);

  async function render() {
    const r = await API.get("/api/overview?range=" + range);
    if (r.code !== 0) return;
    lastOverview = r.data;
    renderReadout(r.data.totals);
    renderWaterfall();
    renderGauges(r.data.accounts || []);
    renderModels(r.data.by_model || []);
    const hours = range === "today" ? 24 : range === "7d" ? 168 : 720;
    const gran = range === "30d" ? "day" : "hour";
    const ts = await API.get(`/api/analytics/timeseries?metric=tokens&granularity=${gran}&hours=${hours}`);
    renderTrend(ts.data?.items || []);
  }

  function renderReadout(t) {
    const el = document.getElementById("readout");
    const saved = t.cost_baseline - t.cost_actual;
    const note = document.getElementById("baseline-note");
    if (note) note.textContent = `基线口径 = ${t.baseline_model || "counterfactual"} 全价`;
    el.innerHTML = `
      <div class="readout-cell">
        <span class="eyebrow">Actual · <span class="zh">实际成本</span></span>
        <b>${Fmt.usd(t.cost_actual)}</b>
        <span class="sub">${rangeName()} · 等效 ${Fmt.tok(t.prompt_tokens + t.completion_tokens)} tok</span>
      </div>
      <div class="readout-cell is-flow">
        <span class="eyebrow">Saved · <span class="zh">已节省</span></span>
        <b>${Fmt.usd(saved)}<span class="unit">${t.savings_pct}%</span></b>
        <span class="sub">基线 ${Fmt.usd(t.cost_baseline)}</span>
      </div>
      <div class="readout-cell">
        <span class="eyebrow">Cache hit · <span class="zh">缓存命中</span></span>
        <b>${Fmt.pct(t.cache_hit_rate)}</b>
        <span class="sub">命中 ${Fmt.tok(t.cached_tokens)} / 输入 ${Fmt.tok(t.prompt_tokens)}</span>
      </div>
      <div class="readout-cell">
        <span class="eyebrow">Requests · <span class="zh">请求数</span></span>
        <b>${Fmt.tok(t.requests)}</b>
        <span class="sub">失败率 ${t.failure_rate}% · 回放 ${t.replays} 次</span>
      </div>
      <div class="readout-cell">
        <span class="eyebrow">Output share · <span class="zh">输出占比</span></span>
        <b>${Fmt.pct(t.output_cost_pct ?? 0)}</b>
        <span class="sub">输出 = 全价成本重心</span>
      </div>`;
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

  /* ---- 三窗水位计(签名元素): 每账号三支管, 液面 = 窗口已用 % ---- */
  const WL = [["rolling", "5H"], ["weekly", "周"], ["monthly", "月"]];

  function gaugeTube(win, pct, resetIso) {
    const cls = pct >= 85 ? "danger" : pct >= 70 ? "warn" : "";
    const fill = pct >= 85 ? "var(--brand)" : pct >= 70 ? "var(--brass)" : "var(--flow)";
    const reset = resetIso ? Fmt.countdown(resetIso) : "";
    return `<div class="gauge-row" title="${Fmt.esc(win)}${reset ? " · " + Fmt.esc(reset) + "回灌" : ""}">
      <span class="wl">${win}</span>
      <div class="gauge-tube">
        <div class="gauge-ticks"></div>
        <div class="gauge-warnline"></div>
        <div class="gauge-fill ${cls}" style="--fill:${fill};height:${Math.min(100, pct)}%"></div>
      </div>
      <span class="wv">${pct.toFixed(0)}%</span>
    </div>`;
  }

  function renderGauges(list) {
    const el = document.getElementById("gauges");
    if (!list.length) {
      el.innerHTML = '<div class="empty">尚无账号 — 到「账号」页添加 OpenCode Go key, 管道先通起来</div>';
      return;
    }
    el.innerHTML = list.map(a => {
      const wins = a.windows || {};
      const st = a.breaker?.account_open_until > 0
        ? `<span class="badge danger">熔断中</span>`
        : (a.enabled ? (a.primary ? '<span class="badge ok">主力</span>' : "")
                     : '<span class="badge off">已禁用</span>');
      const tubes = WL.map(([k, label]) => {
        const w = wins[k] || {};
        return gaugeTube(label, w.percent ?? a.used_pct ?? 0, w.resetsAt);
      }).join("");
      return `<div class="gauge-card">
        <div class="gauge-name">
          <b>${Fmt.esc(a.alias || "账号" + a.id)}</b>
          <span class="tag">…${Fmt.esc(a.key_tail)}</span> ${st}
        </div>
        <div class="gauge-rows">${tubes}</div>
        <div class="gauge-foot">在途 ${a.inflight}/${a.inflight_cap} · 权重 ${a.weight} · 5h 回灌 ${Fmt.esc(Fmt.countdown((a.resets || {}).rolling))}</div>
      </div>`;
    }).join("");
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
    // 语义配色: 红 = 输出全价(成本重心), 青 = 缓存命中(便宜), 蓝灰 = 未命中输入
    Chart.line(document.getElementById("trend-chart"), {
      labels,
      stacked: true,
      series: [
        { name: "输入(未命中)", values: pick("input_uncached"), color: "var(--c2)" },
        { name: "输入(命中缓存)", values: pick("input_cached"), color: "var(--c1)" },
        { name: "输出(全价)", values: pick("output"), color: "var(--c4)" },
      ],
      yFmt: v => Fmt.tok(v),
      tipFmt: v => Fmt.tok(v),
    });
  }

  /* ---- 实时流 ---- */
  function pushFeed(p) {
    Flow.pulse();
    const el = document.getElementById("feed");
    const div = document.createElement("div");
    const effort = p.effort && p.effort[0] !== p.effort[1]
      ? `<span class="hi num">effort ${Fmt.esc(p.effort[0] || "—")}→${Fmt.esc(p.effort[1] || "—")}</span>` : "";
    const replay = p.replay_hit ? '<span class="hi">↩replay</span>' : "";
    div.innerHTML = `<span class="t">${Fmt.clock(Date.now() / 1000)}</span>` +
      `<b>${Fmt.esc(p.model || "?")}</b> <span class="tag">${Fmt.esc(p.account || "—")}</span>` +
      ` in ${Fmt.tok(p.prompt_tokens)}<span class="tag">(缓存${Fmt.tok(p.cached_tokens)})</span>` +
      ` out <span class="hi-red">${Fmt.tok(p.completion_tokens)}</span> ` +
      `${effort}${replay} ${Fmt.dur(p.latency_ms || 0)}` +
      (p.status === "ok" ? "" : ` <span class="badge danger">${Fmt.esc(p.status)}</span>`);
    el.prepend(div);
    while (el.children.length > 40) el.lastChild.remove();
  }

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
      if (lastOverview) renderModels(lastOverview.by_model || []);
    };
    SSE.on("request", pushFeed);
    SSE.on("quota", () => { if (Pages.current === "overview") render(); });
  }
  document.addEventListener("DOMContentLoaded", bind);
})();
