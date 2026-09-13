# OpenCode Go 中转站 设计方案

上游：OpenCode Go 套餐 ×N 账号（单一上游）
日期：2026-09-13

---

## 0. 结论先行

**能省多少**：以 a-share-research-os 为参考负载（~330 agent 运行/交易日、~200M token/月）：

| 阶段 | 等效全价 token/月 | 月成本 | 归因 |
|---|---|---|---|
| 无缓存无优化 | 200M | $32 | 基线 |
| 前缀缓存生效 | ~45M | $12.5 | **上游自动，-61%** |
| 加中转层优化 | ~30M | $8.5 | **中转层 -33%** |

等效全价 token **200M/月 → 30M/月（-85%）**。归因要清楚：**61% 是上游缓存白送的，中转层的增量是 33%。**

中转层的 33% 拆开（复利，非简单相加）：

| 优化器 | 增量收益 |
|---|---|
| 分轮次 reasoning effort 控制 | **-26%** |
| 工具结果规范化 | -3% |
| 精确响应回放 | -3% |
| 工具 schema 瘦身 | -1% |

单一上游下，中转站的三个角色：**① 账号池调度（配额与并发） ② 输出侧优化（成本重心所在） ③ 缓存保护（防止负优化）**。

---

## 1. 上游机制（2026-09-13 实网实测，非文档推断）

| 项 | 值 |
|---|---|
| 端点 | `https://opencode.ai/zen/go/v1`（OpenAI 兼容） |
| 认证 | `Authorization: Bearer <key>` |
| **必需头** | `x-opencode-session` —— 缺失直接 400 `MissingSessionID` |
| 模型 | deepseek-v4.1-flash / deepseek-v4-flash / deepseek-v4-flash-vision-exp / mimo-v2.5（免费档）等 |
| 配额 | 三窗口百分比：rolling 5h = 20% · 周 = 50% · 月 = 100%，另叠加**每模型月度美元上限**（$15/$30/$60 档） |
| 超限 | HTTP **429**，错误体含 `FreeUsageLimitError` / `usage limit` |
| 配额查询 | ✅ `GET /zen/go/v1/usage` → `{rolling\|weekly\|monthly: {status, percent, resetsAt}}` |
| 前缀缓存 | ✅ **自动生效**，同前缀二次请求实测 `cached_tokens = 3840 / 3983`（96%） |
| 缓存作用域 | **按账号隔离**（换 key 同一前缀 `cached_tokens = null`，全冷） |
| 缓存与 session 的关系 | **无关**。全新 `x-opencode-session` 同一前缀仍 95% 命中 → 换 session 无害 |
| 缓存 TTL | ≥ 15–20 分钟（跨多轮探测仍命中） |
| ReAct 循环命中率 | 逐轮爬升：第 3/4/5 轮实测 56% / 68% / 74%（当轮新内容尚未入缓存） |

**三条硬约束，直接决定架构：**

1. **缓存按账号隔离** → 会话必须粘账号，否则每次漂移都是全价冷启动
2. **每模型月度上限是独立于窗口百分比的** → 429 可能是模型级而非账号级，熔断要分两级
3. **`x-opencode-session` 是硬要求** → 中转层必须按账号合成并持久化

---

## 2. 成本结构：为什么优化对象是输出

单次 reasoner 运行（8 次工具调用 → ~10 轮 LLM 调用）：

```
prompt 累计          ≈ 100k tok
  ├─ 命中缓存  92k   × $0.014/M = $0.00129   ← 缓存命中约 1/10 价
  └─ 未命中     8k   × $0.14/M  = $0.00112
output(含 thinking)  15k  × $0.28/M = $0.00420
                                       ────────
                              单次运行 ≈ $0.0066   → 输出占 64%
```

**输入侧 92k 的缓存 token 加起来还不如输出的一半。**

原因：前缀缓存把输入单价打到 0.1x，输入被"折叠"了；而所有档位都挂着 `@max`
（`LLM_MODEL_STRONG=deepseek-v4.1-flash@max`、`LLM_MODEL_CHEAP=mimo-v2.5@max`），
reasoner 跑 10 轮、每轮都在以满档生成 thinking，**thinking token 不可缓存、全价计费**。

推论：**压缩输入前缀的收益上限是 0.1x，压输出是 1x。** 优化方向由此确定。

---

## 3. 架构

```
客户端
  a-share-research-os ──┐
  Claude Code ──────────┤  POST /v1/chat/completions   (OpenAI 协议)
  Cline / 自有脚本 ──────┤  POST /v1/messages          (Anthropic 协议)
                        ▼
┌──────────────────────────────────────────────────────────┐
│  中转站                                                   │
│                                                           │
│  ① 协议层   Anthropic ↔ OpenAI 双向转换 + SSE 事件流互转     │
│  ② 账号池   Go key ×N，每账号独立 session / 独立熔断         │
│  ③ 配额账本  轮询 /usage(账号级) + 本地按模型累加(模型级)     │
│  ④ 路由     会话粘性 × 剩余配额 × 模型上限 × 并发闸门         │
│  ⑤ 优化器   分轮 effort · 工具结果规范化 · 回放 · 前缀不变式   │
│  ⑥ 记账     tokens / cached_tokens / 等效成本 / 账号消耗速率 │
└──────────────────────────────────────────────────────────┘
                        ▼
              opencode.ai/zen/go/v1
```

**技术栈**：FastAPI + httpx（必须支持 SSE 流式透传，客户端走的是流式）+ Redis（配额/回放/并发闸门）+ Postgres（请求日志）。

**不要用 new-api / one-api 二次开发** —— 它们是为"转售 API key"设计的（渠道管理、发子令牌、计费），**不做请求体改写**，而 §5 的优化器全部要改写请求体。

---

## 4. 账号池与配额调度

### 4.1 账号级：轮询 `/usage`

每账号每 5 分钟一次 `GET /zen/go/v1/usage`，拿到三窗口 `percent` + `resetsAt`：

- `percent ≥ 阈值` → 降权（不是直接停用，因为百分比是预估口径）
- `percent ≥ 100` 或收到 429 → 冷却到 `resetsAt`

### 4.2 模型级：本地累加 + 实测校准

`/usage` 只有账号级百分比，而**每模型月度美元上限会独立触发 429**。所以还要按模型本地累加：

```
该账号该模型本月消耗 += prompt_tokens × 输入单价 + completion_tokens × 输出单价
（缓存命中的 prompt 部分按 1/10 计）
对比 LLM_MODEL_MONTHLY_CAP_USD 里的上限 → 超过安全线(90%)则该 (账号,模型) 降权
```

**校准**：首次收到某 (账号,模型) 的 429 时，用"累计消耗 ÷ 该模型上限"反推真实单价系数，修正后落库。
上游价目表会变，靠实测校准比硬编码可靠。

### 4.3 熔断分两级

| 级别 | 触发 | 影响范围 | 恢复 |
|---|---|---|---|
| 模型级 | 同账号单模型 429 | 该账号该模型 | TTL 15 分钟（若是月度上限，TTL 内不会恢复，靠路由绕开） |
| 账号级 | 同账号**第二个**模型也 429 | 该账号全部模型 | 冷却到 `resetsAt` |

判据：模型级配额只影响单个模型；账号级（5h/周/总额度）会让同账号所有模型一起失败 —— 后者在第二个模型报错时即被识别。

### 4.4 会话粘性（缓存保护的核心）

缓存按账号隔离，所以**同一会话必须钉在同一账号**：

```python
affinity_key = req.headers.get("x-opencode-session") or hash(session_id_from_body or client_key)
account = pick_sticky(affinity_key, candidates, ttl=3600)
```

客户端已经会发 `x-opencode-session`（a-share-research-os 按账号生成），中转层直接复用它作为亲和键。
粘性优先级**高于**负载均衡 —— 宁可某个账号偏载，也不要打散缓存。

### 4.5 并发闸门

每账号独立在途上限（Go 套餐的并发上限不透明，按实测调）。
超出则排队或溢出到其他账号 —— 但**溢出会打断会话粘性**，所以只在粘性账号熔断时才溢出。

---

## 5. 优化器（按收益排序）

### 5.1 分轮次 reasoning effort 控制 —— 最大单项，-26%

tool-calling 循环里前几轮只是"决定下一个调什么工具"，属于低风险决策；**只有最后一轮出结论才需要满档**。

**客户端做不到这件事**：`_cached_llm` 按 `(账号, 模型, 温度, effort)` 缓存 LLM 实例，effort 在构造时写死，
而 LangChain 的 `create_agent` 全程绑同一个实例，无法按轮次变化。**这正是中转层不可替代的价值。**

```python
_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}

def rewrite_effort(body: dict) -> dict:
    """按已完成的工具轮数降 effort；只降不升。"""
    rounds = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
    if "tools" not in body:                 # 无工具 = 单发调用(ainvoke_json 等)，不动
        return body
    want = "low" if rounds == 0 else ("medium" if rounds < 4 else None)
    if not want:                            # 尾声轮保留调用方档位(出结论，不降)
        return body
    now = body.get("reasoning_effort")
    if now and _RANK.get(want, 9) < _RANK.get(now, -1):
        body["reasoning_effort"] = want
    return body
```

`只降不升` 是硬规则：差档杂活的 `@max` 不该被提到高成本档。

**护栏指标**：工具调用轮数中位数、`agent_static_fallback` 触发次数、数据抢救次数。
这三个一旦上升，说明降过头 —— 把阈值从 4 调到 6，或只降 `rounds == 0` 那一轮。

### 5.2 前缀不变式（防亏损，不是创造收益）

这是最容易被"优化"毁掉的地方。中转层必须**主动保证**：

- ❌ **绝不做按轮次动态增删工具** —— 会让每轮前缀都不同，彻底摧毁缓存，负收益
- ❌ **绝不重排 system / tools 顺序** —— 前缀必须逐字节稳定
- ✅ **会话粘账号**（见 4.4）
- ⚠️ **降级链要谨慎**：换模型 = 另一份全新缓存，从零开始。降级只应在配额真耗尽时发生

值得做的一条：**把工具 schema 描述一次性瘦身 40–50%**（客户端 docstring 压一压）。
收益只有 1–2%，但零风险，且能压低首轮延迟。参考：`get_futures_inst_watch` 592 字符、
`get_chip_structure` 499、`get_technical_indicators` 428，这三个合计 ≈ 950 tok，可压到 ~400 tok。

### 5.3 工具结果规范化 —— -3%

`get_day_kline` 是 24 个工具里**唯一未截断**的（其余都截到 400–1000 字符），
返回 30–40 根 K 线 ≈ 2000+ 字符。中转层降采样：

```python
def downsample_kline(body: dict) -> dict:
    """把 tool 消息里的日K数组压到最近 20 根 + 只留 OHLCV。"""
    for m in body.get("messages", []):
        if m.get("role") != "tool":
            continue
        try:
            data = json.loads(m["content"])
        except (ValueError, TypeError):
            continue
        if isinstance(data, list) and data and "close" in data[0]:
            m["content"] = json.dumps(
                [{k: r.get(k) for k in ("date","open","high","low","close","volume")}
                 for r in data[-20:]], ensure_ascii=False)
    return body
```

这部分是"新生成内容"，**全价计费**，所以收益是实打实的（不像压缩缓存部分那样打 0.1 折）。

⚠️ **不要做历史折叠**（把 N 轮前的工具结果换成摘要）：那部分已命中缓存，压缩它只省 0.1x，
却直接损害推理质量（结论严重依赖累积的工具数据）。**性价比为负。**

### 5.4 精确响应回放 —— -3%

`(model, messages, temperature, effort)` 哈希 → Redis 回放。适用场景：

- **重试**：日均 56 次，每次重发完整 prompt
- **盘中重复刷新**：每 30 分钟刷一次建议，同股票同数据快照下的请求可能逐字节相同

只对 `temperature ≤ 0.1` 启用，TTL ≤ 盘中刷新周期（避免把过期数据喂回决策）。

### 5.5 免费档路由（结构性节省）

`mimo-v2.5` 是免费档。把能降级的杂活全部导向它 → 那部分成本归零。
中转层按 model alias 兜底：客户端没指定模型时，按请求特征（无工具 / 短 prompt / 低温度）路由到免费档。

注意：免费档有独立的速率与可用性限制，且质量档位明显更低 —— 只用于杂活，并设独立熔断。

---

## 6. 协议层：Anthropic ↔ OpenAI

Go 是 OpenAI 兼容端点。如果中转站要服务 Claude Code 这类 Anthropic 原生客户端，需要双向转换：

| 方向 | 转换点 |
|---|---|
| 入站 Anthropic → 内部 | `system` 数组 → `messages[0]`；`tools[].input_schema` → `functions[].parameters`；`max_tokens` 必填 → 保留 |
| 出站 内部 → OpenAI | 标准 OpenAI body；注入 `x-opencode-session` |
| SSE 事件流 | `message_start` / `content_block_delta` / `message_stop` ↔ `data: {choices:[{delta}]}` / `[DONE]` |
| 缓存 | Anthropic 的 `cache_control` 断点在 Go 上无对应物（DeepSeek 自动缓存）→ **剥离即可，不要伪造** |

这块是工作量最大也最容易出 bug 的部分（流式事件的状态机），建议 P0 只做 OpenAI 入站，
Anthropic 入站在 P4 单独做。

---

## 7. 记账与验证

每请求必须落这些字段，否则无法验证优化是否生效：

```
ts · account · model · reasoning_effort(改写前后)
prompt_tokens · cached_tokens · completion_tokens
tool_rounds · latency · retried · replay_hit
等效成本 · 是否命中免费档
```

**核心看板**：缓存命中率、每运行等效成本、输出 token 占比、各账号消耗速率。

⚠️ **前提**:a-share-research-os 侧的 `client.py:714` `_track_cost` 只读 `prompt_tokens`，
把 `prompt_tokens_details.cached_tokens` 丢掉了，账上按全价算 → 成本看板**高估约 2–3 倍**。
不修这个，中转层的效果无法度量。

---

## 8. 分阶段实施

| 阶段 | 内容 | 产出 |
|---|---|---|
| **P0** | 反代骨架：OpenAI 入站 + SSE 透传 + 记账（含 cached_tokens） | 能跑通，账本准确 |
| **P1** | 账号池 + `/usage` 轮询 + 两级熔断 + **会话粘性** | 多账号自动调度，缓存不打散 |
| **P2** | 优化器：分轮 effort → 工具结果规范化 → 响应回放 | 成本下降可量化 |
| **P3** | 模型级配额账本 + 免费档路由 + 打分路由 | 配额利用率最大化 |
| **P4** | Anthropic 入站（服务 Claude Code 类客户端） | 中转站产品化 |

**P0/P1 是地基，优化器放 P2** —— 优化器的效果必须靠 P0 的账本度量，顺序反了就是凭感觉优化。

### 验收标准

| 阶段 | 通过条件 |
|---|---|
| P0 | 记账的 `cached_tokens` 与上游返回值一致；成本看板不再高估 |
| P1 | 同一会话在整段生命周期内落在同一账号；429 自动切换且不中断会话 |
| P2 | 每运行等效成本对比 P1 下降 ≥ 20%；工具调用轮数中位数不上升 |
| P3 | 免费档承接 ≥ 15% 的调用量；无 (账号,模型) 撞月度上限 |

---

## 9. 一个前提

OpenCode Go 的服务条款对账号共享/多用户接入有约束（同 GLM 一类问题）。
这决定架构选型：**自用 + 自有项目**是最稳的定位；若要对外提供服务，
建议只做 Go 线路并把风险敞口控制在小范围，或改用按量 API 计价。
