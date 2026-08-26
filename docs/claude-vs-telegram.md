# Claude Code vs Telegram 交易员:架构与使用指南

> 整理自 2026-08-25 的问答(§8–§9 补于 2026-08-26),学习用。描述的是本项目(hermes-swing-trader)的实际部署。

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

> **旁注:还有一个 `hermes`(默认 profile)。** 终端里直接跑 `hermes`(不带
> `-p trader`)是同一个 Hermes 引擎的默认 profile:有 terminal/file 等全套工具、
> 能改代码,用自己的 `~/.hermes/state.db` 和记忆,与 trader 完全隔离(见 §8)。
> 所以交易员"物理上不能改代码"不是引擎能力差异,而是 trader profile 的配置
> 刻意锁死(禁工具、换 SOUL.md、独立存储)。默认 hermes 和 Claude Code 角色
> 重叠(都能改代码),但本项目的进化角色由 Claude Code 承担,默认 hermes
> 不参与任何交易链路。

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

## 8. Hermes 的记忆管理(实测 2026-08-26)

交易员的所有存储在 `~/.hermes/profiles/trader/` 下,和主 Hermes(`~/.hermes/state.db`)
**完全独立**——你用默认 profile 聊的内容,交易员看不到,反之亦然。

记忆分三层,容量和寿命完全不同:

### 第 1 层:工作记忆(每轮真正进模型的内容)

每次你在 Telegram 发消息,网关组装的上下文 =
**SOUL.md(全量)+ MEMORY.md(全量)+ 当前会话所有活跃消息**。
这一层受 kimi-k3 的上下文窗口限制,是真正意义上的"记忆容量"。

### 第 2 层:会话历史 `state.db`(SQLite)

- 位置:`~/.hermes/profiles/trader/state.db`。实测 752 KB,6 个会话、130 条消息
  (Telegram DM 会话 99 条,累计输入约 12.3 万 token;其余是日报 cron 和 `trader -z` 测试)。
- **每一轮完整落库**:你的消息、模型回复、每次工具调用及其返回,每条带 token 计数。
  磁盘上没有上限,只增不减。
- **Telegram 会话是一个永不结束的长会话**(配置 `session_reset: mode: none`),
  不像 Claude Code 每次打开终端是新会话。
- **自动压缩**:`messages` 表有 `compacted`/`active` 标志位。会话长到快撑爆模型窗口时,
  Hermes 把老消息压缩成摘要、标记不活跃,之后只送摘要进模型(原文仍在 db 里)。
  目前 130 条全部 `active=1, compacted=0`,还没触发过——但迟早会,届时早期对话
  细节就只剩摘要。
- db 上建了全文索引(FTS),配 `session_search` 工具可搜历史会话——但 trader 的
  telegram 工具集没开这个工具,交易员用不上。

**`/new` 之后,历史去哪了?** 分两个层面:

- **磁盘层面:还在**。`/new` 只是给当前会话盖上 `ended_at` 标记、开一个新会话;
  旧会话全部消息原封不动留在 `messages` 表里,这个库只增不减。
- **模型层面:等于没了**。每轮只组装**当前会话**的消息进上下文,新会话从零开始;
  且交易员没有 `session_search` 工具,**物理上没有手段**翻旧会话——对它来说
  `/new` 之前的对话就像从没发生过,还"记得"的只有 SOUL.md 和 MEMORY.md。

实操含义:想查旧对话,在 Claude Code 里用 `sqlite3` 读
`~/.hermes/profiles/trader/state.db`,原文都在;有值得留的结论,`/new` 前先说
"把要点记入记忆";交易状态(持仓/订单)在 Alpaca 服务端,重置不受影响。
会话历史对交易员只是短期工作记忆,对你则是一份永久留底的日志。

### 第 3 层:长期记忆 MEMORY.md(memory 工具)

- 位置:`~/.hermes/profiles/trader/memories/MEMORY.md`,**硬上限 2200 字符**(约 2 KB)。
- 模型自己决定写什么(每 10 轮 nudge 一次提醒它考虑),全量注入每轮对话——
  不靠检索、不会漏,但写满了就得自己取舍覆盖。
- 实测目前 636 字节、两条事实(止损价取整分;8-25 规则更新确认)。

### 和 Claude Code 的对比

|              | Hermes(交易员)                            | Claude Code                                            |
| ------------ | ----------------------------------------- | ------------------------------------------------------ |
| 会话生命周期 | Telegram 一个会话无限续,直到手动 `/reset` | 每次启动新会话,转录存文件可 resume                     |
| 上下文溢出   | 自动压缩老消息成摘要(原文留在 db)         | 同样自动 compact(原文留在转录文件)                     |
| 长期记忆结构 | 单个 MEMORY.md,≤2200 字符,全量注入        | 一个事实一个 `.md` 文件 + 索引;索引全量加载,正文按需读 |
| 长期记忆容量 | 约 2 KB 硬顶                              | 实际不封顶(按需读文件)                                 |
| 历史可检索性 | SQLite FTS(trader 未开放给模型)           | 可直接 grep 转录/记忆文件                              |

两边分层哲学一致:**指令文件全量注入 → 会话历史受窗口限制、溢出压缩 → 长期记忆跨会话**。
真正的差别在长期记忆形态:Hermes 是一块 2 KB 的小黑板,写满要擦;Claude Code 是
索引 + 文件库。这也是"规则沉淀放 Claude Code"的又一个理由——那块小黑板只够记
"止损要取整分"量级的操作事实,规则必须走代码和 `trader_profile.md`。

## 9. 使用教程:把 Telegram 交易员用顺手

### 9.1 下指令的技巧(让它可靠执行)

1. **一条消息一件事,带全参数**。好指令自带标的、比例/股数、方式:
   - ✅ "NVDA 30% 直接下单"
   - ✅ "TSLA 突破昨高就买,20%"
   - ❌ "看看有什么机会搞一下"(开放式指令 = 把决策权交给概率推理)
2. **用系统里已有的动词**。"直接下单"(place_direct)、"止损上移到 X"(move_stop_up)、
   "看下持仓/账户"——指令词和工具名对得上,模型不用猜。
3. **确认环节别省**。`require_confirmation=true` 时它会复述订单再执行;
   看到复述与你意图不符,直接说"取消"。这是最后一道人肉风控。
4. **别在 Telegram 里定规则**(见 §4)。"以后都 XX"这种话最多进那块 2 KB 黑板,
   可能被覆盖、可能不被执行。规则回 Claude Code 落成代码。
5. **希望它跨会话记住的操作事实,明说"记住:……"**。比如"记住:财报周不开新仓"。
   它会写进 MEMORY.md,每轮都在场——但注意 2200 字符上限,只记高价值短事实。

### 9.2 Context 管理(会话命令)

网关直接拦截这些命令,不经过模型,随时可发:

| 命令            | 作用                               | 什么时候用                        |
| --------------- | ---------------------------------- | --------------------------------- |
| `/status`       | 看会话状态(模型、token 用量等)     | 感觉它变慢/变贵时先看这个         |
| `/compact`      | 手动压缩:老消息变摘要,窗口腾出来   | 提示"session too large"或响应变慢 |
| `/new` `/reset` | 开全新会话,历史清零(db 里仍有存档) | 换话题/隔一段时间/它开始胡言乱语  |
| `/stop`         | 打断当前正在执行的轮次             | 它跑偏了、或你想撤回刚发的指令    |
| `/queue`        | 查看排队中的消息                   | 连发多条后确认没丢                |
| `/model`        | 查看/切换模型                      | 怀疑 fallback 切到了 deepseek 时  |

**建议的卫生习惯:**

- **每周或大行情节点后 `/new` 一次**。长会话三个代价:每轮重发全部历史(token 费),
  响应变慢,压缩后早期细节漂移。持仓、订单都在 Alpaca 服务端,重置会话不丢任何交易状态;
  长期事实在 MEMORY.md 也不丢。重置前想留笔记,先说一句"把本次会话要点记入记忆"。
- **别把会话历史当账本**。"我上周买了啥"这种问题让它查 `get_positions` /
  `list_open_orders`(真数据),而不是靠它回忆聊天记录(可能已被压缩)。
- 想审计它的记忆,在 Claude Code 里:
  `cat ~/.hermes/profiles/trader/memories/MEMORY.md`,发现过期/错误条目直接让 Claude 改。

### 9.3 加功能/改规则的标准流程(在 Claude Code 做)

```
1. 描述需求("加一个 XX 工具"/"止损规则改成 XX")
2. Claude 改 trading_server.py / risk_config.json + 写测试 → uv run pytest
3. 若动了规则文本:cp trader_profile.md ~/.hermes/profiles/trader/SOUL.md
4. trader gateway restart(网关和 MCP 子进程加载新代码)
5. 在 Telegram 里发一条测试指令验证,git commit
```

改完立刻在 Telegram 验证这一步别省——网关不重启就是旧代码在跑。

### 9.4 排障速查

| 症状                | 第一步                                                     |
| ------------------- | ---------------------------------------------------------- |
| Telegram 不回复     | `trader gateway status`;挂了就 `restart`                   |
| 回复但报工具错误    | 看 `~/.hermes/profiles/trader/logs/`,通常是 Finnhub/Alpaca |
| 答非所问/忘了刚说的 | `/status` 看 token;多半该 `/compact` 或 `/new`             |
| 风格突变/质量下降   | `/model` 确认是否 fallback 到了 deepseek                   |
| 紧急停止一切交易    | `./kill_switch.sh on`(不经过任何 LLM)                      |
