# Siphon

OpenCode Go 订阅账号池中转站 —— 统一 OpenAI 兼容入口、自动切号、省 token。

设计文档：[docs/design.md](docs/design.md)（后端与优化器口径）· [docs/frontend.md](docs/frontend.md)（前端与图表规格）

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # 改 SIPHON_MASTER_KEY, 填 OPENCODE_KEYS
.venv/bin/uvicorn siphon.main:app --host 127.0.0.1 --port 4100
```

- 管理台：http://127.0.0.1:4100/ （密码 = `SIPHON_MASTER_KEY`）
- 客户端接入：`base_url = http://127.0.0.1:4100/v1`，`api_key = SIPHON_MASTER_KEY`

接入 a-share-research-os 只改一行：

```bash
# .env
OPENCODE_BASE_URL=http://127.0.0.1:4100/v1
```

## 它做什么

| 层 | 能力 |
|---|---|
| 账号池 | 多把 Go key，主力优先 + 溢出，`/usage` 轮询回填剩余额度 |
| 会话粘性 | 同一会话钉同一账号 —— 上游前缀缓存**按账号隔离**（实测），打散即全价冷启动 |
| 两级熔断 | 模型级（该模型月度上限 429）与账号级（第二模型也 429 → 升级）分开标记 |
| 优化器 | 分轮次 effort 控制 / 工具结果(日K)降采样 / 精确响应回放 |
| 记账 | prompt / **cached_tokens** / completion 三口径落库，节省四段拆分（可对账） |
| 中转台 | 总览（节省瀑布 + 配额环 + 实时流）、账号配置中心、日志 |

## 省 token 的账（为什么是这些优化器）

实测（2026-09-13）：上游前缀缓存自动生效，同前缀二次请求 `cached_tokens=3840/3983`（96%），
且**按账号隔离**、不按 session 隔离。缓存把输入打到 0.1x 后，**输出（含 thinking）占成本 64%** ——
所以优化器主攻输出侧与"新内容"，不做历史折叠（缓存价部分压了只省 0.1x，还损害质量）。

| 优化器 | 增量收益 |
|---|---|
| 分轮次 effort 控制（首轮 low / 中间 medium / 尾声不动，只降不升） | -26% |
| 工具结果规范化（`get_day_kline` 降采样到 20 根） | -3% |
| 精确响应回放（重试 / 盘中重复请求不出站） | -3% |
| 工具 schema 瘦身（客户端一次性改动） | -1% |

瀑布图（总览页）按 `基线 − 缓存 − 免费档 − 规范化 − 回放 = 实际` 拆分，
`accounting.py` 保证各段可加且能对上总账。

## 目录

```
siphon/
  config.py       环境变量
  db.py           SQLite(WAL) + 审计/事件
  accounts.py     账号池 + 两级熔断 + 会话粘性
  router.py       选号(粘性 > 主力 > 在途最少)
  quota.py        /usage 轮询
  optimizers.py   三个优化器
  accounting.py   记账 + 节省四段拆分 + 聚合查询
  upstream.py     上游 HTTP(必需头 x-opencode-session)
  proxy.py        /v1/chat/completions(SSE 透传)
  admin.py        /api/*(总览/账号/日志/策略/事件)
  main.py         FastAPI 入口
web/static/       零构建前端(app-0-chart 图表基元 + 4 个页面模块)
```

## 当前边界（P0）

- 单实例内存态（熔断/粘性/在途）；多实例需把 `accounts.py` 的内存 dict 换 Redis
- Anthropic 协议入口（服务 Claude Code）在 P4
- effort 优化器的"节省量"依赖反事实（不知满档会输出多少），暂不计入瀑布图，
  其余三段与 `baseline − actual` 严格可加
- 管理台鉴权 = 单密码（`SIPHON_MASTER_KEY`）；多租户/子令牌在 P3
