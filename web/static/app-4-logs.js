/* app-4-logs.js —— 日志页: 筛选 + 表格 + 分页 */
"use strict";
(() => {
  let page = 1, total = 0, pageSize = 50;

  Pages.on("logs", () => load(1));

  async function load(p) {
    page = p || 1;
    const q = new URLSearchParams();
    q.set("page", page); q.set("page_size", pageSize);
    const acct = document.getElementById("lf-account").value;
    const model = document.getElementById("lf-model").value.trim();
    const status = document.getElementById("lf-status").value;
    const cached = document.getElementById("lf-cached").value;
    const hours = document.getElementById("lf-hours").value;
    if (acct) q.set("account_id", acct);
    if (model) q.set("model", model);
    if (status) q.set("status", status);
    if (cached) q.set("cached", cached);
    q.set("hours", hours);

    const [r, la] = await Promise.all([
      API.get("/api/logs?" + q),
      API.get("/api/accounts"),
    ]);
    if (r.code !== 0) return;
    total = r.data.total;
    const names = {};
    (la.data?.items || []).forEach(a => names[a.id] = a.alias || "账号" + a.id);
    render(r.data.items || [], names);
    document.getElementById("lg-page").textContent = `第 ${page} 页 / 共 ${Math.max(1, Math.ceil(total / pageSize))} 页(${total} 条)`;
    document.getElementById("lg-prev").disabled = page <= 1;
    document.getElementById("lg-next").disabled = page * pageSize >= total;
  }

  function render(items, names) {
    const el = document.getElementById("lg-tbody");
    if (!items.length) { el.innerHTML = '<tr><td colspan="10"><div class="empty">无匹配请求</div></td></tr>'; return; }
    el.innerHTML = items.map(q => `
      <tr>
        <td class="num">${Fmt.time(q.ts)}</td>
        <td>${Fmt.esc(names[q.account_id] || (q.account_id ? "账号" + q.account_id : "—"))}</td>
        <td>${Fmt.esc(q.model)}</td>
        <td>${q.effort_in !== q.effort_out && q.effort_out
          ? `<span class="hi num">${Fmt.esc(q.effort_in || "—")}→${Fmt.esc(q.effort_out)}</span>`
          : `<span class="tag num">${Fmt.esc(q.effort_out || q.effort_in || "—")}</span>`}</td>
        <td class="num">${Fmt.tok(q.prompt_tokens)}<span class="tag">(${Fmt.tok(q.cached_tokens)})</span></td>
        <td class="num"><b>${Fmt.tok(q.completion_tokens)}</b></td>
        <td class="num">${q.saved_tokens ? `<span class="hi">${Fmt.tok(q.saved_tokens)}</span>` : "—"}</td>
        <td class="num">${q.tool_rounds || 0}</td>
        <td class="num">${Fmt.dur(q.latency_ms)}</td>
        <td>${q.replay_hit ? '<span class="hi">↩replay</span>' : (q.status === "ok"
          ? '<span class="tag">ok</span>' : `<span class="badge danger" title="${Fmt.esc(q.error)}">${Fmt.esc(q.status)}</span>`)}</td>
      </tr>`).join("");
  }

  document.addEventListener("DOMContentLoaded", () => {
    ["lf-account", "lf-model", "lf-status", "lf-cached", "lf-hours"].forEach(id =>
      document.getElementById(id).addEventListener("change", () => load(1)));
    document.getElementById("lf-refresh").onclick = () => load(page);
    document.getElementById("lg-prev").onclick = () => load(page - 1);
    document.getElementById("lg-next").onclick = () => load(page + 1);
    SSE.on("request", () => { if (Pages.current === "logs" && page === 1) load(1); });
  });
})();
