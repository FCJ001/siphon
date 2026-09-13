/* app-3-accounts.js —— 账号管理(配置中心): 列表 + 增删改 + 详情抽屉 + 全局策略 */
"use strict";
(() => {
  let list = [], policy = {};

  Pages.on("accounts", render);

  async function render() {
    const r = await API.get("/api/accounts");
    if (r.code !== 0) return;
    list = r.data.items || [];
    policy = r.data.policy || {};
    renderTable();
    renderPolicy();
  }

  function statusBadge(a) {
    if (!a.enabled) return '<span class="badge off">已禁用</span>';
    if (a.breaker?.account_open_until > 0)
      return `<span class="badge danger">熔断中 ${Fmt.countdown(a.breaker.account_open_until_iso)}</span>`;
    if (a.used_pct >= 85) return '<span class="badge danger">接近上限</span>';
    if (a.used_pct >= 70) return '<span class="badge warn">降权</span>';
    return '<span class="badge ok">正常</span>';
  }

  function winBar(pct) {
    const p = Math.min(100, Math.max(0, pct || 0));
    const cls = p >= 85 ? "danger" : p >= 70 ? "warn" : "";
    return `<div class="bar"><i class="${cls}" style="width:${p}%"></i></div>`;
  }

  /* 迷你水位管(表格内联): 三窗 = 5h/周/月 */
  function miniTube(label, pct) {
    const p = Math.min(100, Math.max(0, pct || 0));
    const fill = p >= 85 ? "var(--brand)" : p >= 70 ? "var(--brass)" : "var(--flow)";
    return `<div class="gauge-tube mini" title="${label} 已用 ${p.toFixed(0)}%">
      <div class="gauge-warnline"></div>
      <div class="gauge-fill" style="--fill:${fill};height:${p}%"></div></div>`;
  }

  function miniGauges(a) {
    const w = a.windows || {};
    const pct = k => (w[k]?.percent ?? a.used_pct ?? 0);
    return `<div class="gauge-mini" style="display:flex;gap:5px;align-items:flex-end">
      ${miniTube("5h", pct("rolling"))}${miniTube("周", pct("weekly"))}${miniTube("月", pct("monthly"))}
    </div>`;
  }

  function renderTable() {
    const el = document.getElementById("acct-tbody");
    if (!list.length) {
      el.innerHTML = '<tr><td colspan="8"><div class="empty">尚无账号 — 点「添加账号」填入 OpenCode Go key</div></td></tr>';
      return;
    }
    el.innerHTML = list.map(a => `
      <tr>
        <td><b>${Fmt.esc(a.alias || "账号" + a.id)}</b> <span class="tag">…${Fmt.esc(a.key_tail)}</span></td>
        <td>${statusBadge(a)}</td>
        <td>${a.primary ? "✓" : ""}</td>
        <td class="num">${a.weight}</td>
        <td class="num">${a.inflight}/${a.inflight_cap}</td>
        <td><div style="display:flex;gap:10px;align-items:center">
          ${miniGauges(a)}<span class="num tag">${a.used_pct.toFixed(0)}%</span></div></td>
        <td>${(a.breaker?.models || []).length ? `<span class="badge warn">${a.breaker.models.length} 个模型熔断</span>` : "—"}</td>
        <td style="white-space:nowrap">
          <button class="btn sm" data-act="detail" data-id="${a.id}">详情</button>
          <button class="btn sm" data-act="edit" data-id="${a.id}">编辑</button>
          <button class="btn sm" data-act="toggle" data-id="${a.id}">${a.enabled ? "禁用" : "启用"}</button>
          <button class="btn sm" data-act="del" data-id="${a.id}">删除</button>
        </td>
      </tr>`).join("");
  }

  function renderPolicy() {
    document.getElementById("pol-overflow").value = policy.overflow || "strict";
    document.getElementById("pol-ttl").value = policy.affinity_ttl ?? 3600;
    document.getElementById("pol-cap").value = policy.primary_inflight_cap ?? 24;
    document.getElementById("pol-free").value = policy.free_routing ? "true" : "false";
  }

  /* ---- 表单(新增/编辑) ---- */
  let editing = null, editingDetail = null;
  function openForm(a) {
    editing = a ? a.id : null;
    document.getElementById("mf-title").textContent = a ? `编辑账号 …${a.key_tail}` : "添加账号";
    document.getElementById("mf-key").value = "";
    document.getElementById("mf-key").placeholder = a ? "留空 = 不修改" : "sk-...";
    document.getElementById("mf-alias").value = a?.alias || "";
    document.getElementById("mf-note").value = a?.note || "";
    document.getElementById("mf-weight").value = a?.weight ?? 100;
    document.getElementById("mf-cap").value = a?.inflight_cap ?? 24;
    document.getElementById("mf-allowlist").value = (a?.allowlist || []).join(", ");
    document.getElementById("mf-primary").checked = !!a?.primary;
    document.getElementById("mf-force").checked = false;
    document.getElementById("mf-check").textContent = "";
    UI.open("modal-form");
  }

  async function submitForm() {
    const body = {
      alias: document.getElementById("mf-alias").value.trim(),
      note: document.getElementById("mf-note").value.trim(),
      weight: parseInt(document.getElementById("mf-weight").value || "100", 10),
      inflight_cap: parseInt(document.getElementById("mf-cap").value || "24", 10),
      primary: document.getElementById("mf-primary").checked,
      allowlist: document.getElementById("mf-allowlist").value
        .split(/[,，]/).map(s => s.trim()).filter(Boolean),
    };
    const key = document.getElementById("mf-key").value.trim();
    const check = document.getElementById("mf-check");
    if (editing) {
      if (key) body.key = key;
      const r = await API.patch("/api/accounts/" + editing, body);
      if (r.code !== 0) { check.textContent = r.message; return; }
      UI.closeAll(); UI.toast("已保存并热加载"); render(); return;
    }
    if (!key) { check.textContent = "key 不能为空"; return; }
    body.key = key;
    const r = await API.post("/api/accounts" + (document.getElementById("mf-force").checked ? "?force=true" : ""), body);
    if (r.code !== 0) {
      check.textContent = r.message + (r.message.includes("401") ? " — 勾选「跳过校验」可强制保存" : "");
      document.getElementById("mf-force-row").style.display = "block";
      return;
    }
    UI.closeAll();
    UI.toast(r.data?.validated ? "已添加(key 校验通过)" : "已添加(未校验)");
    render();
  }

  /* ---- 详情抽屉 ---- */
  async function openDetail(id) {
    editingDetail = id;
    const r = await API.get("/api/accounts/" + id + "/detail");
    if (r.code !== 0) return;
    const d = r.data, a = d.account;
    document.getElementById("d-title").innerHTML =
      `${Fmt.esc(a.alias || "账号" + a.id)} <span class="tag">…${Fmt.esc(a.key_tail)}</span> ${statusBadge(a)}`;

    document.getElementById("d-wins").innerHTML = ["rolling", "weekly", "monthly"].map(k => {
      const pct = a.used_pct || 0;   // P0: /usage 只回最紧窗口, 逐窗口拆分在 P3
      const label = { rolling: "5小时", weekly: "周", monthly: "月" }[k];
      const reset = (a.resets || {})[k];
      return `<tr><td>${label}</td><td class="num">${pct.toFixed(0)}%</td>
        <td>${winBar(pct)}</td><td class="tag">${Fmt.esc(Fmt.countdown(reset))}</td></tr>`;
    }).join("");

    document.getElementById("d-models").innerHTML = (d.model_usage || []).map(m => `
      <tr><td>${Fmt.esc(m.model)}</td><td class="num">${Fmt.tok(m.prompt_tokens + m.completion_tokens)}</td>
      <td class="num">${Fmt.usd(m.cost_usd)}</td>
      <td>${m.cap_pct != null ? winBar(m.cap_pct) + `<span class="tag num"> ${m.cap_pct}%</span>` : '<span class="tag">免费</span>'}</td></tr>`
    ).join("") || '<tr><td colspan="4" class="tag">本月暂无消耗</td></tr>';

    document.getElementById("d-reqs").innerHTML = (d.recent_requests || []).map(q => `
      <div><span class="t">${Fmt.time(q.ts)}</span> ${Fmt.esc(q.model)}
       in ${Fmt.tok(q.prompt_tokens)}(缓存${Fmt.tok(q.cached_tokens)})
       out ${Fmt.tok(q.completion_tokens)} ${q.status === "ok" ? "" : `<span class="badge danger">${Fmt.esc(q.status)}</span>`}</div>`
    ).join("") || '<div class="tag">暂无请求</div>';

    document.getElementById("d-breaker").innerHTML = a.breaker?.account_open_until
      ? `<span class="badge danger">账号级熔断, ${Fmt.countdown(a.breaker.account_open_until_iso)}</span>`
      : (a.breaker?.models || []).length
        ? a.breaker.models.map(m => `<span class="badge warn">${Fmt.esc(m.model)} 熔断</span>`).join(" ")
        : '<span class="tag">无</span>';

    UI.open("modal-detail");
  }

  /* ---- 绑定 ---- */
  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("acct-add").onclick = () => openForm(null);
    document.getElementById("acct-export").onclick = async () => {
      const r = await API.get("/api/accounts/export?with_secrets=false");
      if (r.code !== 0) return UI.toast(r.message, true);
      const blob = new Blob([JSON.stringify(r.data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `siphon-accounts-${new Date().toISOString().slice(0, 10)}.json`;
      a.click(); URL.revokeObjectURL(a.href);
      UI.toast("已导出(不含 key 明文)");
    };
    document.getElementById("acct-import").onclick = () => {
      const inp = document.createElement("input");
      inp.type = "file"; inp.accept = ".json";
      inp.onchange = async () => {
        try {
          const data = JSON.parse(await inp.files[0].text());
          const r = await API.post("/api/accounts/import",
            { accounts: data.accounts || [], mode: "merge" });
          if (r.code === 0) { UI.toast(`已导入 ${r.data.imported} 个账号`); render(); }
          else UI.toast(r.message, true);
        } catch (e) { UI.toast("文件不是合法 JSON", true); }
      };
      inp.click();
    };
    document.getElementById("acct-tbody").addEventListener("click", async e => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const id = +btn.dataset.id, act = btn.dataset.act, a = list.find(x => x.id === id);
      if (act === "detail") return openDetail(id);
      if (act === "edit") return openForm(a);
      if (act === "toggle") {
        await API.patch("/api/accounts/" + id, { enabled: !a.enabled });
        UI.toast(a.enabled ? "已禁用" : "已启用"); render(); return;
      }
      if (act === "del") {
        if (!confirm(`确认删除账号「${a.alias || a.key_tail}」? 该操作不可撤销。`)) return;
        await API.del("/api/accounts/" + id);
        UI.toast("已删除"); render();
      }
    });

    document.getElementById("mf-save").onclick = submitForm;
    document.getElementById("mf-cancel").onclick = UI.closeAll;
    document.getElementById("d-close").onclick = UI.closeAll;
    document.getElementById("d-reset-breaker").onclick = async () => {
      await API.post("/api/accounts/" + editingDetail + "/reset-breaker");
      UI.toast("熔断已重置"); UI.closeAll(); render();
    };
    document.getElementById("d-reset-session").onclick = async () => {
      if (!confirm("重置 session 后, 该账号的已有前缀缓存仍有效(实测缓存不按 session 隔离), 确认继续?")) return;
      await API.post("/api/accounts/" + editingDetail + "/reset-session");
      UI.toast("session 已重置"); UI.closeAll();
    };

    document.getElementById("pol-save").onclick = async () => {
      const r = await API.put("/api/accounts/policy", {
        overflow: document.getElementById("pol-overflow").value,
        affinity_ttl: parseInt(document.getElementById("pol-ttl").value || "3600", 10),
        primary_inflight_cap: parseInt(document.getElementById("pol-cap").value || "24", 10),
        free_routing: document.getElementById("pol-free").value === "true",
      });
      if (r.code === 0) UI.toast("策略已保存并热加载");
      else UI.toast(r.message, true);
    };

    SSE.on("quota", () => { if (Pages.current === "accounts") render(); });
  });
})();
