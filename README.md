# Siphon

OpenCode Go 订阅账号池中转站 —— 独立项目，对任何客户端一视同仁：统一 OpenAI 兼容入口、
多账号自动调度、在**不降低输出质量**的前提下省 token。

设计文档：[docs/design.md](docs/design.md)（后端与优化器口径）· [docs/frontend.md](docs/frontend.md)（前端与图表规格）

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # 改 SIPHON_MASTER_KEY, 填 OPENCODE_KEYS
./scripts/start.sh            # 或 .venv/bin/uvicorn siphon.main:app --port 4100
```

- 管理台：http://127.0.0.1:4100/ （密码 = `SIPHON_MASTER_KEY`）
- 代理端点：`http://127.0.0.1:4100/v1`，API key = `SIPHON_MASTER_KEY`

任何 OpenAI 兼容客户端（Claude Code / Cline / OpenCode / 自研 agent）接入都只改两行：
`base_url` 指向中转站、`api_key` 换成中转站的 key。客户端代码零改动。

## 它做什么

| 层 | 能力 |
|---|---|
| 账号池 | 多把 Go key，主力优先 + 溢出，`/usage` 轮询回填剩余额度，重启后从快照恢复 |
| 会话粘性 | 同一会话钉同一账号 —— 上游前缀缓存**按账号隔离**（实测），打散即全价冷启动 |
| 两级熔断 | 模型级（该模型月度上限 429）与账号级（第二模型也 429 → 升级）分开标记 |
| 优化器 | 超大工具结果压缩 / 精确响应回放 / 分轮次 effort 控制（默认关，见下） |
| 记账 | prompt / **cached_tokens** / completion 三口径落库，节省四段拆分（可对账） |
| 中转台 | 总览（节省瀑布 + 三窗水位计 + 实时流）、账号配置中心、日志 |

## 质量优先的省 token 立场

原则：**先保输出质量，再谈省 token**。因此：

| 优化器 | 默认 | 质量边界 |
|---|---|---|
| 分轮次 effort 控制 | **关** | 降 reasoning 深度有质量风险。全局 `SIPHON_OPT_EFFORT=1` 开启，或客户端带 `x-siphon-effort-policy: optimize` 按请求开启；开启后也"只降不升"，出结论的尾声轮不动 |
| 超大工具结果压缩 | 开 | 只碰超过 3000 字符的消息，常规结果一字不动；JSON 数组只裁头部保留最近 30 条（时序数据最近的最有用）；不重写、不删字段、**不做摘要** —— 摘要会改变模型看到的事实 |
| 精确响应回放 | 开 | 仅 temperature ≤ 0.1 的逐字节相同请求，回放的是当时真实响应（数据就在消息里，同请求=同数据） |
| 历史折叠 | **不做** | 已命中缓存的部分压缩只省 0.1x，却改变模型看到的历史 —— 负收益 |

上游前缀缓存自动生效（实测命中 96%，按账号隔离），把输入打到约 0.1x；
此后成本重心在**输出侧**，上表前两行就是冲着它去的。

## 目录

```
siphon/
  config.py       环境变量
  db.py           SQLite(WAL) + 审计/事件
  accounts.py     账号池 + 两级熔断 + 会话粘性(内部 monotonic 时钟)
  router.py       选号(粘性 > 主力 > 在途最少, 重试必跳过失败账号)
  quota.py        /usage 轮询
  optimizers.py   工具结果压缩 / 回放 / effort 控制(可选)
  accounting.py   记账 + 节省四段拆分 + 聚合查询
  upstream.py     上游 HTTP(必需头 x-opencode-session)
  proxy.py        /v1/chat/completions(SSE 透传)
  admin.py        /api/*(总览/账号/日志/策略/事件)
  main.py         FastAPI 入口
web/static/       零构建前端(app-0-chart 图表基元 + 4 个页面模块)
scripts/          start.sh / smoke.py(端到端自测) / seed_demo.py(演示数据)
```

## 当前边界（P0）

- 单实例内存态（熔断/粘性/在途，配额水位已可从快照恢复）；多实例需把 `accounts.py` 的内存 dict 换 Redis
- Anthropic 协议入口（服务 Claude Code 原生协议）在 P4
- effort 优化器的"节省量"依赖反事实（不知满档会输出多少），暂不计入瀑布图；
  其余三段与 `baseline − actual` 严格可加
- 管理台鉴权 = 单密码（`SIPHON_MASTER_KEY`）；多租户/子令牌在 P3
