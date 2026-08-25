# Hermes 语言交互交易系统(模拟盘)

用自然语言在 Telegram 里指挥交易员 agent,按你的 swing trading 规则
(Breakout / Pullback + 当日低点止损)执行交易。
**风控是硬编码的**:30% 仓位上限、5% 止损距离、每日限笔数、急停开关,
全部在工具层强制执行,模型无法绕过。默认接 Alpaca **模拟盘**,不动真钱。

两条信号通道,共享同一套硬风控:

1. **Hermes 对话**:在 Telegram 里跟 agent 说"突破就买",agent 查行情 → 预检 → 你确认 → 下单
2. **TradingView 警报**(可选):在 TradingView 画好突破位,警报打到本机 webhook →
   预检 → Telegram 卡片按钮确认 → 下单(见下文「TradingView 信号接入」)

## 文件清单

| 文件                | 作用                                                     |
| ------------------- | -------------------------------------------------------- |
| `trading_server.py` | MCP 工具服务器:行情查询 + 带硬风控的下单/改单            |
| `webhook_server.py` | TradingView 警报接收服务:鉴权 → 风控预检 → Telegram 确认 |
| `risk_config.json`  | 风控参数(仓位上限、止损距离、日限笔数、信号风险比例等)   |
| `trader_profile.md` | 交易员 agent 的个性化指令(你的交易规则和纪律)            |
| `kill_switch.sh`    | 急停脚本,不经过 LLM 直接封锁交易                         |
| `tests/`            | 单元测试(`uv run pytest`)                                |
| `signals_log.jsonl` | TradingView 信号审计日志(运行时生成,已 gitignore)        |

## 部署步骤

### 第 1 步:准备 Alpaca 模拟盘账号(免费)

1. 注册 https://alpaca.markets → 进入 Paper Trading 面板 → 生成 API Key
2. 记下 Key ID 和 Secret(模拟盘 key 只能操作虚拟资金)

### 第 2 步:安装交易服务器(使用 uv)

```bash
uv sync                          # 安装依赖(mcp 1.x + requests + fastapi/uvicorn)
cp .env.example .env             # 若无 .env 则复制模板
# 编辑 .env,填入 Key ID 和 Secret(服务器启动时自动加载,已被 .gitignore 忽略)
# 快速自检(能连通 Alpaca 即成功):
uv run python -c "import trading_server as t; print(t.get_account())"
```

### 第 3 步:安装并配置 Hermes Agent

1. 按官方文档安装 Hermes 并连接你的模型提供商(Nous Portal / OpenRouter / 单家 API key)
2. 配置 Telegram 网关:在 @BotFather 创建 bot 拿到 token,填入 Hermes 的 Telegram 配置,
   **务必设置只响应你自己的 Telegram 用户 ID(白名单)**
3. 把本 MCP 服务器注册进 Hermes(stdio 方式)。注册命令因版本而异,请以
   官方 MCP 配置文档为准,典型配置形如:
   ```json
   {
     "mcpServers": {
       "swing-trading": {
         "command": "uv",
         "args": [
           "run",
           "--directory",
           "/绝对路径/hermes-swing-trader",
           "python",
           "trading_server.py"
         ]
       }
     }
   }
   ```
4. 创建交易员 profile,把 `trader_profile.md` 的内容设为该档案的个性化指令,
   并按需 pin 一个模型(建议工具调用能力强的模型)
5. **裁剪权限**:从该交易员档案中移除 shell、浏览器、文件写入等与交易无关的工具

### 第 4 步:跑通一条完整链路

在 Telegram 里发:

> 看一下 NVDA,如果突破了昨天的高点就按规则买

预期流程:agent 查行情 → 核对入场条件 → `preview_bracket_buy` 风控预检 →
把订单摘要发给你 → 你回复"确认" → agent 用 confirm_token 执行 → 回报成交。

## TradingView 信号接入(可选)

在 TradingView 上画突破位/设警报,信号经 webhook 进入本系统风控,
Telegram 按钮确认后在 Alpaca 模拟盘成交。仅美股、只做多。

### 准备

1. **专用 Telegram bot**:找 @BotFather 新建一个 bot(与 Hermes 的 bot 分开),
   拿到 token;私聊 @userinfobot 查自己的数字 chat id;先给新 bot 发一条 /start
2. **`.env` 追加**(参考 `.env.example`):`WEBHOOK_SECRET`(随机串,
   `python -c "import secrets; print(secrets.token_urlsafe(24))"` 生成)、
   `TG_BOT_TOKEN`、`TG_CHAT_ID`
3. **Cloudflare Tunnel**(免费):
   ```bash
   brew install cloudflared
   cloudflared tunnel --url http://localhost:8787
   ```

记下输出的 `https://xxx.trycloudflare.com` 地址(快速模式,重启会变;
要固定域名可用 `cloudflared tunnel create` 配置命名隧道)

### 启动

```bash
uv run python webhook_server.py
```

### TradingView 警报配置(需支持 webhook 的付费套餐)

- Webhook URL:`https://<你的隧道域名>/hook/<WEBHOOK_SECRET>`
- 警报消息(Message)填:
  ```json
  {"secret": "<WEBHOOK_SECRET>", "action": "buy", "symbol": "{{ticker}}", "price": {{close}}}
  ```
  可选加 `"stop": 你的止损价`;不加则自动用当日最低价(符合本系统规则)

### 信号流程

信号到达 → 非盘中/重复/风控不过 → Telegram 告知原因并作废;
通过 → 收到订单摘要卡片(股数按单笔风险 `webhook_risk_pct_per_trade`% 自动计算)
→ 30 分钟内点【✅ 确认下单】成交,点【❌ 放弃】或超时作废。
急停(`KILL_SWITCH`)、日限笔数等硬风控与 Hermes 通道完全共享。
审计日志逐条记录在 `signals_log.jsonl`。

## 日常操作

- **急停**:`./kill_switch.sh on`(或在服务器目录手动创建名为 `KILL_SWITCH` 的文件),
  瞬间封锁一切下单/改单;`off` 解除
- **调风控**:改 `risk_config.json` 后重启服务生效;`trading_server.py`(经 Hermes)
  和 `webhook_server.py` 是两个进程,都在跑的话两个都要重启
- **关闭二次确认**(不建议):`require_confirmation` 改为 `false`,
  agent 将可在风控范围内自主下单(TradingView 通道的按钮确认不受此开关影响)
- **审计**:Alpaca 面板可查全部订单;TradingView 信号逐条记录在 `signals_log.jsonl`;
  Hermes 侧建议开启工具调用日志

## 安全红线(再强调一次)

1. 真钱账户接入前,先在模拟盘完整跑至少一个月,并逐笔复盘
2. API key 只放环境变量/配置,绝不写进 profile 指令或聊天消息
3. Telegram bot 必须设用户白名单——能私聊它的人就能指挥它
4. 整套系统建议跑在 Docker 或专用机器里
5. 模型对"利好新闻"等网页内容的判断可能被操纵(提示注入),
   所以确认环节和硬风控不要轻易关闭

## 免责声明

本系统仅供学习与模拟交易研究。市场有风险,任何模型都不能预测行情;
切换到真实资金前请自行全面评估,盈亏自负。
