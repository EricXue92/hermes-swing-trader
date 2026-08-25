<div align="right">

[English](README.en.md) | **中文**

</div>

# Hermes 语言交互交易系统(模拟盘)

用自然语言在 Telegram 里指挥交易员 agent,按你的 swing trading 规则
(Breakout / Pullback;做多止损=当日低点,做空止损=当日高点)执行交易。
**风控是硬编码的**:50% 仓位上限、5% 止损距离、每日限笔数、急停开关,
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

### 第 1 步:准备行情与交易账号(均免费)

1. 注册 https://alpaca.markets → 进入 Paper Trading 面板 → 生成 API Key,
   记下 Key ID 和 Secret(模拟盘 key 只能操作虚拟资金)
2. 注册 https://finnhub.io → 拿免费 API key(实时报价用,60 次/分钟够用)

### 第 2 步:安装交易服务器(使用 uv)

```bash
uv sync                          # 安装依赖(mcp 1.x + requests + fastapi/uvicorn)
cp .env.example .env             # 若无 .env 则复制模板
# 编辑 .env,填入 APCA_API_KEY_ID / APCA_API_SECRET_KEY / FINNHUB_API_KEY
# (服务器启动时自动加载,已被 .gitignore 忽略)
# 快速自检(能连通 Alpaca 即成功):
uv run python -c "import trading_server as t; print(t.get_account())"
```

**行情数据源**:最新价/当日开高低走 Finnhub 实时合并报价(全市场);
成交量与历史日 K 走 Alpaca 全市场 feed(`delayed_sip`/`sip`,当日延迟 15 分钟,
历史精确)。不填 `FINNHUB_API_KEY` 会退回 Alpaca 免费档默认的 IEX 源——
只覆盖约 2% 成交量、价格可偏离真实市场价近 1%,突破判断会失真,不建议。

### 第 3 步:安装并配置 Hermes Agent

以下为实际部署并验证过的命令(Hermes 已按官方文档安装、`hermes` 在 PATH 中,
模型提供商已连接,如 Nous Portal / OpenRouter / 单家 API key):

1. **创建交易员 profile**(独立于日常使用的 default profile,会生成 `trader` 快捷命令):

   ```bash
   hermes profile create trader --description "Swing trading assistant with hard risk limits"
   cp trader_profile.md ~/.hermes/profiles/trader/SOUL.md   # 个性化指令
   ```

   注意:新建 profile 不会继承全局的模型配置,需确认
   `~/.hermes/profiles/trader/config.yaml` 里有 `model:` 块(provider/default),
   缺失会报 "Provider authentication failed";OAuth 凭据在共享凭据池中,无需重复登录。

2. **注册本 MCP 服务器**(stdio 方式,13 个工具,做多做空双向,含"直接下单"快速通道):

   ```bash
   trader mcp add swing-trading --command uv \
     --args run --directory /绝对路径/hermes-swing-trader python trading_server.py
   trader mcp test swing-trading   # 验证连通
   ```

3. **裁剪权限**:移除 shell、浏览器、文件写入等与交易无关的工具,
   保留图像识别(看行情截图)、备忘、澄清提问、定时任务(收盘日报):

   ```bash
   for p in cli telegram; do
     trader tools disable --platform $p web browser terminal file code_execution \
       image_gen bfl tts computer_use delegation session_search skills
   done
   ```

4. **配置 Telegram 网关**:在 @BotFather 创建**专用** bot(与 TradingView 通道的
   bot 分开)拿到 token,私聊 @userinfobot 查自己的数字用户 ID,然后在
   `~/.hermes/profiles/trader/.env` 中追加(行首不要留空格):

   ```
   TELEGRAM_BOT_TOKEN=<BotFather 给的 token>
   TELEGRAM_ALLOWED_USERS=<你的数字用户 ID>
   TELEGRAM_HOME_CHANNEL=<你的数字用户 ID>
   ```

   `TELEGRAM_ALLOWED_USERS` 是白名单,**必填**——能私聊 bot 的人就能指挥它;
   `TELEGRAM_HOME_CHANNEL` 是主动通知(定时日报等)的投递地址,单人使用填同一个 ID。

5. **安装并启动网关**(macOS 上注册为 launchd 服务,开机自启、崩溃自动重启):

   ```bash
   trader gateway install
   trader gateway status
   tail -f ~/.hermes/profiles/trader/logs/gateway.log   # 应看到 "telegram connected"
   ```

6. **配置模型与 fallback**(当前实际部署,工具调用已验证):
   主力 Kimi K3(Moonshot 国际端点),限流/5xx 时自动切 DeepSeek V4 Pro。

   把两个 API key 写入 `~/.hermes/profiles/trader/.env`
   (变量名必须用 Hermes 认的这两个,与本仓库 `.env` 里的名字不同):

   ```
   KIMI_API_KEY=<platform.moonshot.ai 的 key>
   DEEPSEEK_API_KEY=<platform.deepseek.com 的 key>
   ```

   再确认 `~/.hermes/profiles/trader/config.yaml` 有如下两块:

   ```yaml
   model:
     default: kimi-k3
     provider: kimi-coding # 内置 provider,端点 api.moonshot.ai/v1
     base_url: ""
   fallback_providers:
     - provider: deepseek # 内置 provider,端点 api.deepseek.com/v1
       model: deepseek-v4-pro
   ```

   改完 `trader gateway restart` 生效;`trader fallback list` 可查看链条,
   `trader -z "用 get_quote 查一下 AAPL"` 可单次验证。换模型用 `trader model`
   交互选择(交易 agent 优先选工具调用能力强的推理模型)。

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
- **网关管理**:`trader gateway status` / `restart` / `stop`;
  日志在 `~/.hermes/profiles/trader/logs/gateway.log`
- **改交易规则**:改 `trader_profile.md` 后需重新
  `cp trader_profile.md ~/.hermes/profiles/trader/SOUL.md` 并 `trader gateway restart`
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
