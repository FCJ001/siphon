/* app-0-chart.js —— 手写 SVG 图表基元(frontend.md §7.2)
 * line / hbar / donut / waterfall / spark + axis 复用
 * 颜色一律走 CSS 变量; tooltip 单例; update() 原地重画 */
"use strict";
const Chart = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const CSS_VARS = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6"];
  const color = (i) => `var(${CSS_VARS[i % CSS_VARS.length]})`;

  function svgEl(tag, attrs = {}) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
  }
  // 全局单例 tooltip
  let tip = null;
  function showTip(html, x, y) {
    if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip"; document.body.appendChild(tip); }
    tip.innerHTML = html; tip.style.display = "block";
    tip.style.left = Math.min(x + 12, innerWidth - 190) + "px";
    tip.style.top = (y + 14) + "px";
  }
  function hideTip() { if (tip) tip.style.display = "none"; }

  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
    return 10 * p;
  }

  function prepare(el, height) {
    el.classList.add("chart-box");
    el.innerHTML = "";
    const W = Math.max(240, el.clientWidth || el.parentElement?.clientWidth || 600);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${height}`, height });
    el.appendChild(svg);
    return { svg, W, H: height };
  }

  /* ---------- 多序列折线 / 堆叠面积 ---------- */
  function line(el, opt) {
    const H = opt.height || 190;
    const { svg, W } = prepare(el, H);
    const padL = 46, padR = 10, padT = 10, padB = 22;
    const iw = W - padL - padR, ih = H - padT - padB;
    const labels = opt.labels || [];
    const n = labels.length;
    if (!n) { el.innerHTML = '<div class="empty">暂无数据</div>'; return; }
    const series = opt.series.filter(s => s.values.some(v => v > 0));
    if (!series.length) series.push({ name: "(空)", values: new Array(n).fill(0) });

    let maxV = 0;
    const stack = opt.stacked;
    if (stack) {
      for (let i = 0; i < n; i++) {
        let s = 0; series.forEach(sr => s += (sr.values[i] || 0));
        maxV = Math.max(maxV, s);
      }
    } else series.forEach(sr => sr.values.forEach(v => maxV = Math.max(maxV, v || 0)));
    const yMax = niceMax(maxV || 1);
    const x = i => padL + (n === 1 ? iw / 2 : iw * i / (n - 1));
    const y = v => padT + ih * (1 - v / yMax);

    // 网格 + y 轴
    for (let g = 0; g <= 3; g++) {
      const gv = yMax * g / 3, gy = y(gv);
      svg.appendChild(svgEl("line", { x1: padL, x2: W - padR, y1: gy, y2: gy,
        stroke: "var(--border)", "stroke-width": 1 }));
      const t = svgEl("text", { x: padL - 6, y: gy + 3, "text-anchor": "end",
        "font-size": 9, fill: "var(--faint)" });
      t.textContent = (opt.yFmt || (v => v))(gv);
      svg.appendChild(t);
    }
    // x 轴标签(抽样)
    const step = Math.ceil(n / Math.max(3, Math.floor(iw / 64)));
    labels.forEach((lb, i) => {
      if (i % step && i !== n - 1) return;
      const t = svgEl("text", { x: x(i), y: H - 6, "text-anchor": "middle",
        "font-size": 9, fill: "var(--faint)" });
      t.textContent = lb; svg.appendChild(t);
    });

    // 目标线
    if (opt.target != null) {
      const ty = y(opt.target);
      svg.appendChild(svgEl("line", { x1: padL, x2: W - padR, y1: ty, y2: ty,
        stroke: "var(--warn)", "stroke-dasharray": "4 3", "stroke-width": 1 }));
      const t = svgEl("text", { x: W - padR, y: ty - 3, "text-anchor": "end",
        "font-size": 9, fill: "var(--warn)" });
      t.textContent = `目标 ${opt.target}${opt.targetUnit || ""}`;
      svg.appendChild(t);
    }

    // 事件标记
    (opt.events || []).forEach(ev => {
      const i = labels.indexOf(ev.label);
      if (i < 0) return;
      svg.appendChild(svgEl("line", { x1: x(i), x2: x(i), y1: padT, y2: padT + ih,
        stroke: "var(--danger)", "stroke-dasharray": "2 3", "stroke-width": 1, opacity: .7 }));
    });

    const cum = new Array(n).fill(0);
    series.forEach((sr, si) => {
      const c = sr.color || color(si);           // 允许按语义指定颜色
      let d = "", dArea = "";
      sr.values.forEach((v, i) => {
        const base = stack ? cum[i] : 0;
        const px = x(i), py = y((v || 0) + base);
        d += (i ? "L" : "M") + px.toFixed(1) + " " + py.toFixed(1);
        if (stack) cum[i] += (v || 0);
      });
      if (stack) {
        dArea = d + `L${x(n - 1).toFixed(1)} ${y(stack ? cum[series === sr ? 0 : 0] : 0)}`;
        // 面积沿堆叠底边闭合
        let dClose = "";
        for (let i = n - 1; i >= 0; i--) {
          const below = cum[i] - (sr.values[i] || 0);
          dClose += `L${x(i).toFixed(1)} ${y(below).toFixed(1)}`;
        }
        svg.appendChild(svgEl("path", { d: d + dClose + "Z", fill: c, opacity: .16 }));
      }
      svg.appendChild(svgEl("path", { d, fill: "none", stroke: c,
        "stroke-width": stack ? 1.2 : 1.8 }));
    });

    // 图例(与图形同色: 优先语义色)
    if (opt.legend !== false && series.length) {
      const lg = document.createElement("div"); lg.className = "legend";
      series.forEach((sr, si) => {
        const c = sr.color || color(si);
        const s = document.createElement("span");
        s.innerHTML = `<i style="background:${c}"></i>${sr.name}`;
        lg.appendChild(s);
      });
      el.appendChild(lg);
    }
    // tooltip(列命中)
    svg.addEventListener("mousemove", ev => {
      const rect = svg.getBoundingClientRect();
      const px = (ev.clientX - rect.left) * (W / rect.width);
      const i = Math.max(0, Math.min(n - 1,
        Math.round((px - padL) / (iw / Math.max(1, n - 1)))));
      let rows = `<b>${labels[i]}</b>`;
      [...series].reverse().forEach(sr => {
        rows += `<br>${sr.name}: ${(opt.tipFmt || (v => v))(sr.values[i])}`;
      });
      showTip(rows, ev.clientX, ev.clientY);
    });
    svg.addEventListener("mouseleave", hideTip);
    svg._update = (next) => line(el, { ...opt, ...next });
  }

  /* ---------- 横向条形(双口径) ---------- */
  function hbar(el, opt) {
    const H = opt.height || (opt.items.length * 26 + 8);
    const { svg, W } = prepare(el, H);
    const labelW = opt.labelW || 120, valW = 64;
    const iw = W - labelW - valW;
    const maxV = Math.max(...opt.items.map(i => i.value), 0.000001);
    if (!opt.items.length) { el.innerHTML = '<div class="empty">暂无数据</div>'; return; }
    opt.items.forEach((it, i) => {
      const yy = i * 26 + 4;
      const t = svgEl("text", { x: labelW - 8, y: yy + 12, "text-anchor": "end",
        "font-size": 11, fill: "var(--text)" });
      t.textContent = it.label.length > 14 ? it.label.slice(0, 13) + "…" : it.label;
      svg.appendChild(t);
      svg.appendChild(svgEl("rect", { x: labelW, y: yy, width: iw, height: 16,
        fill: "var(--panel-2)", rx: 3 }));
      const w = Math.max(1, iw * (it.value / maxV));
      svg.appendChild(svgEl("rect", { x: labelW, y: yy, width: w, height: 16,
        fill: color(i), rx: 3, opacity: .85 }));
      const v = svgEl("text", { x: labelW + iw + 6, y: yy + 12, "font-size": 10.5,
        fill: "var(--muted)", "font-family": "var(--mono)" });
      v.textContent = (opt.fmt || (x => x))(it.value);
      svg.appendChild(v);
    });
    svg.addEventListener("mousemove", ev => {
      const rect = svg.getBoundingClientRect();
      const i = Math.min(opt.items.length - 1,
        Math.max(0, Math.floor((ev.clientY - rect.top) * (H / rect.height) / 26)));
      const it = opt.items[i];
      showTip(`<b>${it.label}</b>` + (it.tip ? `<br>${it.tip}` : ""), ev.clientX, ev.clientY);
    });
    svg.addEventListener("mouseleave", hideTip);
  }

  /* ---------- 环形进度 ---------- */
  function donut(el, opt) {
    const size = opt.size || 86, sw = opt.stroke || 8;
    el.classList.add("chart-box");
    const r = (size - sw) / 2, cx = size / 2, cy = size / 2;
    const circ = 2 * Math.PI * r;
    const pct = Math.max(0, Math.min(100, opt.pct || 0));
    const col = pct >= 85 ? "var(--danger)" : pct >= 70 ? "var(--warn)" : (opt.color || "var(--c4)");
    const svg = svgEl("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size });
    svg.appendChild(svgEl("circle", { cx, cy, r, fill: "none", stroke: "var(--panel-2)", "stroke-width": sw }));
    svg.appendChild(svgEl("circle", { cx, cy, r, fill: "none", stroke: col, "stroke-width": sw,
      "stroke-linecap": "round", "stroke-dasharray": `${circ * pct / 100} ${circ}`,
      transform: `rotate(-90 ${cx} ${cy})` }));
    const t = svgEl("text", { x: cx, y: cy + 4, "text-anchor": "middle",
      "font-size": size / 4.6, fill: "var(--text)", "font-family": "var(--mono)" });
    t.textContent = `${Math.round(pct)}%`;
    svg.appendChild(t);
    el.innerHTML = ""; el.appendChild(svg);
  }

  /* ---------- 节省归因瀑布(斜纹填充: 省下的段 = 工程图剖面线) ---------- */
  function waterfall(el, opt) {
    const H = opt.height || 200;
    const { svg, W } = prepare(el, H);
    const defs = svgEl("defs");
    defs.innerHTML =
      `<pattern id="wf-hatch" width="6" height="6" patternTransform="rotate(45)" ` +
      `patternUnits="userSpaceOnUse">` +
      `<rect width="6" height="6" fill="var(--flow-dim)"/>` +
      `<line x1="0" y1="0" x2="0" y2="6" stroke="var(--flow)" stroke-width="1.4"/></pattern>`;
    svg.appendChild(defs);
    const { baseline, actual, parts } = opt;
    const padL = 10, padR = 74, padT = 14, padB = 20;
    const iw = W - padL - padR, ih = H - padT - padB;
    const bars = [{ label: "基线(全价)", from: 0, to: baseline, fill: "var(--c2)", solid: true }];
    let cur = baseline;
    parts.forEach(p => {
      if (!p.amount) return;
      bars.push({ label: p.label, from: cur - p.amount, to: cur, fill: "url(#wf-hatch)", neg: true, amt: p.amount });
      cur -= p.amount;
    });
    bars.push({ label: "实际成本", from: 0, to: actual, fill: "var(--c4)", solid: true });
    const maxV = baseline || 1;
    const bw = Math.min(64, iw / bars.length * 0.62);
    const gap = iw / bars.length;
    bars.forEach((b, i) => {
      const cx = padL + gap * i + (gap - bw) / 2;
      const y1 = padT + ih * (1 - b.to / maxV), y2 = padT + ih * (1 - b.from / maxV);
      const rect = svgEl("rect", { x: cx, y: y1, width: bw, height: Math.max(2, y2 - y1),
        fill: b.fill, rx: b.neg ? 0 : 3, stroke: b.neg ? "var(--flow)" : "none", "stroke-width": b.neg ? 1 : 0 });
      svg.appendChild(rect);
      const t = svgEl("text", { x: cx + bw / 2, y: y1 - 5, "text-anchor": "middle",
        "font-size": 9.5, fill: "var(--muted)", "font-family": "var(--mono)" });
      // 小额段保留 4 位小数, 否则两段都显示 $0.00
      t.textContent = b.neg
        ? `−$${b.amt < 0.01 ? b.amt.toFixed(4) : b.amt.toFixed(2)}`
        : `$${b.to.toFixed(2)}`;
      svg.appendChild(t);
      const lb = svgEl("text", { x: cx + bw / 2, y: H - 6, "text-anchor": "middle",
        "font-size": 9, fill: "var(--muted)" });
      lb.textContent = b.label.length > 7 ? b.label.slice(0, 6) + "…" : b.label;
      svg.appendChild(lb);
    });
    if (opt.savings_pct != null) {
      const t = svgEl("text", { x: W - padR + 4, y: padT + 8, "font-size": 11,
        fill: "var(--flow)", "font-weight": 600, "font-family": "var(--mono)" });
      t.textContent = `省 ${opt.savings_pct}%`; svg.appendChild(t);
    }
  }

  /* ---------- sparkline ---------- */
  function spark(el, values, colorVar = "--c2") {
    const H = 30, W = Math.max(60, el.clientWidth || 90);
    const maxV = Math.max(...values, 0.000001);
    const x = i => values.length === 1 ? W / 2 : W * i / (values.length - 1);
    const y = v => H - 3 - (H - 6) * (v / maxV);
    let d = ""; values.forEach((v, i) => d += (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1));
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H });
    svg.appendChild(svgEl("path", { d, fill: "none", stroke: `var(${colorVar})`, "stroke-width": 1.5 }));
    el.innerHTML = ""; el.appendChild(svg);
  }

  return { line, hbar, donut, waterfall, spark, color, hideTip };
})();
