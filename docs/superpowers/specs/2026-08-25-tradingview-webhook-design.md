# TradingView Webhook 信号接入设计

日期:2026-08-25
状态:已与用户逐节确认

## 目标

TradingView 上画好突破位/设好警报后,警报通过 webhook 打到本机的接收服务,
转入本系统现有的 `preview_bracket_buy` 硬风控流程,经 Telegram 人工确认后在
Alpaca 模拟盘成交。信号层用 TradingView 的图表能力,执行层保持现有硬风控,
仅支持美股、只做多。

## 已确认的关键决策

| 决策点          | 结论                                                                          |
| --------------- | ----------------------------------------------------------------------------- |
| 确认环节        | Telegram 推送订单摘要 + 按钮,人工点击确认后才下单;信号有时效                  |
| Telegram 载体   | 新建专用 bot(与 Hermes 的 bot 完全独立),服务自收自发                          |
| qty / stop 来源 | 服务端按规则自算:stop 默认当日最低价,qty 按单笔风险比例计算                   |
| 部署            | 本机运行 + Cloudflare Tunnel 暴露公网 HTTPS                                   |
| 代码结构        | 新增 `webhook_server.py`,直接 `import trading_server` 复用风控;现有文件不改动 |

## 架构

新增单文件 `webhook_server.py`,独立进程,内部两块:

- **FastAPI 应用**:`POST /hook/{secret}` 接收 TradingView 警报(uvicorn 启动)
- **Telegram 后台线程**:用 `requests` 手写 `sendMessage` / `getUpdates` 长轮询 /
  `answerCallbackQuery` / `editMessageText`,处理【确认/放弃】按钮回调,
  不引入 Telegram 框架(与现有手写 Alpaca API 的风格一致)

复用方式:直接 `import trading_server`,调用其 `preview_bracket_buy`、
`place_bracket_buy`、`_latest_price` 等模块级函数(FastMCP 的 `@mcp.tool()`
不影响直接调用)。`.env`、`risk_config.json`、`KILL_SWITCH`、`state.json`
与 MCP 服务器天然共享。

待确认信号存内存字典:`signal_id → {symbol, qty, stop, confirm_token,
expires_at, tg_message_id}`。进程重启丢失 pending 属可接受行为(等新信号即可)。

已知权衡:MCP 服务器与 webhook 服务两个进程都读写 `state.json`(当日笔数、
已用令牌)。交易频率低(日限 6 笔),竞态窗口极小,不加文件锁。

`cloudflared` 隧道独立进程运行,配置步骤写入 README。

## 信号协议(TradingView 警报消息模板)

```json
{"secret": "<WEBHOOK_SECRET>", "action": "buy", "symbol": "{{ticker}}", "price": {{close}}, "stop": 123.45}
```

- `action`:只支持 `buy`,其他值拒绝(系统只做多)
- `symbol`:1–5 位字母,仅美股;不符合直接拒绝
- `price`:TradingView 触发时的收盘价,仅作日志参考,下单以实时行情为准
- `stop`:可选。带了就用 TradingView 侧算好的值;不带则服务端取当日最低价
  (Alpaca snapshot 的 `dailyBar.l`,符合现有交易规则)

## 处理流程

1. **鉴权**:URL path 中的 secret 与 body 中的 `secret` 双重校验,不符返回
   403,不泄露任何信息
2. **盘中检查**:查 Alpaca `/v2/clock`,非常规盘中时段的信号直接作废,
   推 Telegram 告知(突破信号盘外无意义,市价单也无法即时成交)
3. **定参**:确定 stop(见协议);计算
   `qty = floor(净值 × webhook_risk_pct_per_trade% ÷ (现价 − 止损))`,
   再按 `max_position_pct_of_equity`(30%)截断;qty < 1 则放弃并告知
4. **风控预检**:调用现有 `preview_bracket_buy` 跑全套硬风控。
   不通过 → 拒绝原因推送 Telegram;
   通过 → 推送订单摘要卡片(标的、股数、预估金额、止损、止损距离、最大亏损)
   - 【✅ 确认下单】【❌ 放弃】inline 按钮,pending 记入内存,
     有效期 `webhook_signal_ttl_minutes`(默认 30 分钟)
5. **确认执行**:用户点确认(仅 `TG_CHAT_ID` 白名单有效)→ 校验未过期 →
   调 `place_bracket_buy(confirm_token)`(内部再跑一遍完整风控)→
   结果编辑回原消息
6. **放弃/超时**:点放弃或超时 → pending 移除,消息编辑为已作废/已过期
7. **去重**:同一 symbol 已有 pending 未决时,新信号忽略并提示;
   已持仓标的的重复信号由现有"不重复建仓"风控拦截

## 配置变更

`risk_config.json` 新增(含 notes 说明):

- `webhook_risk_pct_per_trade`: 1 —— 单笔风险占账户净值百分比,决定 qty
- `webhook_signal_ttl_minutes`: 30 —— 信号确认时效,超时作废

`.env` 新增(`.env.example` 同步):

- `WEBHOOK_SECRET` —— `secrets.token_urlsafe` 生成的随机串
- `TG_BOT_TOKEN` —— 专用确认 bot 的 token
- `TG_CHAT_ID` —— 用户的 chat id,既是推送目标也是操作白名单

## 安全

- webhook 鉴权见处理流程第 1 条(TradingView 不支持自定义 header,
  故 secret 走 URL + body)
- Telegram 只处理 `chat_id == TG_CHAT_ID` 的回调,其余静默忽略
- `KILL_SWITCH` 由现有 `_risk_check` 覆盖,preview 与 place 两个时点都会检查;
  急停时信号被拒并推送告知
- confirm_token 机制原样生效:preview 与 place 同进程共享 `RUNTIME_SECRET`,
  令牌一次性、参数绑定

## 异常处理与审计

- Alpaca API 报错(限流、行情不可用等)→ 错误摘要推送 Telegram,信号作废
- Telegram 推送失败 → 记录 stderr 日志,不重试下单相关操作
  (宁可漏信号,不可重复下单)
- 全部事件(信号到达、风控拒绝、推送确认、成交、放弃、超时)追加写
  `signals_log.jsonl` 审计,方便逐笔复盘
- 过期清理:Telegram 轮询线程每轮顺带把超时 pending 编辑为"已过期",
  懒清理,无定时器

## 测试

- **单元测试**(pytest,TDD 先行):qty 计算(风险比例、仓位上限截断、
  qty<1 放弃)、payload 校验(secret 错误、symbol 非法、action 非 buy)、
  TTL 过期逻辑
- **集成手测**:`curl` 模拟 TradingView payload 打本地端口 → Telegram 收到
  卡片 → 点确认 → Alpaca 面板见单;再测拒绝路径(盘外信号、超仓位、
  重复 symbol)
- **端到端**:cloudflared 起隧道后,在 TradingView 建真实警报完整跑通

## 交付物

- `webhook_server.py`(新)
- `risk_config.json`、`.env.example` 增量字段
- `tests/test_webhook_server.py`(新)
- README 新增章节:TradingView 警报配置、cloudflared 隧道、专用 bot 创建步骤
- `pyproject.toml` 新增依赖:fastapi、uvicorn(pytest 为 dev 依赖)
