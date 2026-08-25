# Claude Code vs Telegram 交易员:架构与使用指南

> 整理自 2026-08-25 的问答,学习用。描述的是本项目(hermes-swing-trader)的实际部署。

## 1. 两个对话入口是两套完全不同的系统

|            | **Telegram(交易员)**                                                     | **Claude Code(终端)**                     |
| ---------- | ------------------------------------------------------------------------ | ----------------------------------------- |
| 背后程序   | Hermes Agent 网关,launchd 服务 `ai.hermes.gateway-trader`,7×24 常驻      | Claude Code CLI,打开终端才存在            |
| 模型       | **kimi-k3**(Moonshot 国际端点),限流/5xx 自动切 **deepseek-v4-pro**       | Claude(Anthropic)                         |
| 系统提示词 | `~/.hermes/profiles/trader/SOUL.md`(= 仓库 `trader_profile.md`)          | Claude Code 内置 + CLAUDE.md + 记忆索引   |
| 可用工具   | 仅 ~19 个:13 个交易 MCP 工具 + vision/todo/memory/clarify/cronjob/kanban | 几乎无限:shell、文件读写、git、测试、联网 |
| 能改代码吗 | **不能**(terminal/file/code_execution 已禁用,物理上没有手段)             | 能,这是本职                               |
| 能下单吗   | 能,每单都被 `trading_server.py` 硬风控拦截检查                           | 不主动碰交易                              |
| 记忆       | `state.db`(对话历史)+ Hermes memory 工具(长期记忆)                       | `~/.claude/projects/.../memory/*.md`      |
| 计费       | Moonshot / DeepSeek API 费                                               | Claude 订阅/API 费                        |

两套记忆**互不相通**。

## 2. 两条典型链路

### 链路 A:Telegram 里发"NVDA 30% 直接下单"

```
Telegram 服务器
  → 本机网关(白名单 TELEGRAM_ALLOWED_USERS 过滤)
  → 取 state.db 会话历史 + SOUL.md,调 Moonshot kimi-k3 API
  → kimi-k3 决定调 place_direct_bracket_buy
  → MCP 子进程(trading_server.py):查 Finnhub 现价 → 取当日低点做止损
    → 按账户 30% 算股数 → 跑硬风控 → Alpaca 提交 OTO 单(入场+止损原子提交)
  → 结果回 kimi-k3 → 组织"已成交"回复 → 发回 Telegram
  → 整轮写入 state.db
```

全程没有 Claude 参与;终端关着它也照常工作(收盘日报也是它自己跑的)。

### 链路 B:Claude Code 里说"止损距离上限改成 8%"

```
Claude 改 risk_config.json / 代码 → 写/改测试 → uv run pytest 验证
  → cp trader_profile.md ~/.hermes/profiles/trader/SOUL.md(若动了规则文本)
  → trader gateway restart(网关和 MCP 子进程加载新代码)
  → git commit + push → 值得跨会话记的写进 Claude 记忆
```

从这一刻起 Telegram 里的交易员受新规则约束,它自己对此毫无发言权。

## 3. 指令文件 vs 记忆系统(SOUL.md ≈ CLAUDE.md)

两边都有"指令文件"和"记忆"两套东西,不要混淆:

**指令文件(人写的,每轮对话无条件全量注入,权威):**

| Claude Code | Hermes                                    |
| ----------- | ----------------------------------------- |
| `CLAUDE.md` | `SOUL.md`(源头是仓库 `trader_profile.md`) |

**记忆系统(agent 自己写的,按相关性检索,可能漏):**

| Claude Code                                                   | Hermes                   |
| ------------------------------------------------------------- | ------------------------ |
| `~/.claude/projects/.../memory/*.md`(索引 MEMORY.md 每次加载) | Hermes memory 工具的存储 |

注意:SOUL.md **不会自动同步**——改了 `trader_profile.md` 必须
`cp trader_profile.md ~/.hermes/profiles/trader/SOUL.md` 并 `trader gateway restart`。

## 4. 为什么"在 Telegram 里改规则"不可靠

在 Telegram 说"以后止损都用 3%",这句话只有两个去处:

1. **对话历史**(state.db)——网关重启、新会话、历史截断/压缩后就没了
2. **memory 工具**(如果模型想到要存)——下次靠检索命中才进上下文,可能漏

就算记住了也未必执行:**LLM 的所有指令都只是"建议"**,每轮回复是一次全新
的概率推理。SOUL.md 写的"止损=当日低点"每轮都在场,聊天里说的"3%"时在
时不在,两条冲突时行为不稳定;fallback 切到 deepseek 后取舍可能又不同。

而硬风控层(代码)根本不知道这回事:它只校验"距离 ≤5%",直接下单的自动
止损写死了取当日低点——没有任何一层替你把关"必须 3%"。

**对比**:同一句话在 Claude Code 里说,会变成确定性代码 + 配置 + 测试,
每一单物理上只能符合规则,不随重启、换模型、上下文截断而漂移。

> 一句话:Telegram 里的话影响模型的"想法",Claude 里改的是模型的"世界"。
> 这正是"风控在工具层硬编码,不依赖模型自觉"的设计原因。

## 5. 怎么分工使用效果最大

**Telegram = 盘中执行**(它离行情和下单最近、7×24、手机可用):

- 快速执行:"NVDA 30% 直接下单"、"突破昨高就买"
- 盯盘问询:"看下持仓"、"NVDA 什么情况"
- 仓位管理:"止损上移到 205"
- 看图分析:发行情截图(vision 工具)
- 收盘日报自动推送

**Claude Code = 盘后进化**(能动代码、有硬手段、记忆可靠):

- 改规则与风控(代码级生效,交易员违反不了)
- 加功能(做空、直接下单、日报都是这么来的)
- 复盘审计:拉订单历史、算胜率盈亏比(要跑脚本,交易员干不了)
- 排障:查日志、修网关、修数据源
- 换模型/调基建:`trader model`、fallback 链、cron

**三个习惯:**

1. **规则沉淀**:Telegram 交易中发现"希望它这样做"→ 回头在 Claude 里说一句,软偏好变硬规则
2. **定期复盘**:每周让 Claude 拉成交记录做统计,用数据决定调哪条规则
3. **信任边界**:交易员=手快但只按规则办事的操盘手;`require_confirmation`
   和急停别轻易关;任何"让它更自主"的想法先在 Claude 里过一遍

## 6. 常用命令速查

```bash
trader                    # = hermes -p trader,以交易员 profile 跑 hermes
trader model              # 交互式换模型(改 config.yaml 的 model 块)
trader fallback list      # 查看/管理备用模型链
trader gateway status     # 网关状态 / restart / stop
trader cron list          # 定时任务(收盘日报 daily-close-report)
trader -z "..."           # 单次执行一条指令(测试用)
./kill_switch.sh on|off   # 急停,不经过任何 LLM 直接封锁交易
```

## 7. 当前部署快照(2026-08-25)

- 模型:kimi-k3 主力(kimi-coding / api.moonshot.ai),deepseek-v4-pro fallback
- 行情:Finnhub 实时价 + Alpaca 全市场 feed(delayed_sip/sip)
- 风控:仓位 ≤50%、止损距离 ≤5%、日限 6 笔、allow_short=true、
  allow_direct_order=true、require_confirmation=true
- 止损规则:做多=当日最低价(只上移),做空=当日最高价(只下移)
- 收盘日报:cron `10 5 * * 2-6`(北京时间,覆盖美股夏/冬令时),投递 Telegram
- 测试:`uv run pytest`,63 个
