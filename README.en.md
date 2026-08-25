<div align="right">

**English** | [中文](README.md)

</div>

# Hermes Conversational Trading System (Paper Trading)

Command a trader agent in plain language over Telegram, executing trades
under your swing-trading rules (Breakout / Pullback; longs stop at the day
low, shorts stop at the day high).
**Risk controls are hard-coded**: 50% position cap, 5% max stop distance,
daily trade limit, kill switch — all enforced at the tool layer, impossible
for the model to bypass. Connects to the Alpaca **paper trading** account by
default; no real money involved.

Two signal channels share the same hard risk controls:

1. **Hermes conversation**: tell the agent "buy on breakout" in Telegram;
   the agent checks quotes → pre-trade checks → you confirm → order placed
2. **TradingView alerts** (optional): draw your breakout level in TradingView,
   alerts hit a local webhook → pre-trade checks → Telegram card-button
   confirmation → order placed (see "TradingView Signal Integration" below)

## Files

| File                | Purpose                                                                   |
| ------------------- | ------------------------------------------------------------------------- |
| `trading_server.py` | MCP tool server: quotes + order placement/updates with hard risk controls |
| `webhook_server.py` | TradingView alert receiver: auth → risk pre-check → Telegram confirmation |
| `risk_config.json`  | Risk parameters (position cap, stop distance, daily limit, signal risk %) |
| `trader_profile.md` | The trader agent's persona instructions (your trading rules & discipline) |
| `kill_switch.sh`    | Emergency stop script — blocks trading directly, bypassing the LLM        |
| `tests/`            | Unit tests (`uv run pytest`)                                              |
| `signals_log.jsonl` | TradingView signal audit log (generated at runtime, gitignored)           |

## Deployment

### Step 1: Get market-data and trading accounts (both free)

1. Sign up at https://alpaca.markets → open the Paper Trading dashboard →
   generate an API key; note the Key ID and Secret (paper keys can only touch
   virtual funds)
2. Sign up at https://finnhub.io → get a free API key (used for real-time
   quotes; 60 calls/min is plenty)

### Step 2: Install the trading server (with uv)

```bash
uv sync                          # install deps (mcp 1.x + requests + fastapi/uvicorn)
cp .env.example .env             # copy the template if .env doesn't exist
# Edit .env and fill in APCA_API_KEY_ID / APCA_API_SECRET_KEY / FINNHUB_API_KEY
# (loaded automatically at server start; the file is gitignored)
# Quick self-check (success = Alpaca is reachable):
uv run python -c "import trading_server as t; print(t.get_account())"
```

**Market data sources**: latest price and intraday open/high/low come from
Finnhub's real-time consolidated quote (full market); volume and historical
daily bars come from Alpaca's full-market feeds (`delayed_sip`/`sip` — today's
data delayed 15 minutes, history exact). Without `FINNHUB_API_KEY` the system
falls back to Alpaca's free-tier default IEX feed — which covers only ~2% of
market volume and can deviate from the real market price by nearly 1%,
distorting breakout decisions. Not recommended.

### Step 3: Install and configure Hermes Agent

The commands below were actually deployed and verified (Hermes installed per
the official docs, `hermes` on PATH, a model provider connected — e.g. Nous
Portal / OpenRouter / a single API key):

1. **Create the trader profile** (separate from your everyday default
   profile; also generates the `trader` shortcut command):

   ```bash
   hermes profile create trader --description "Swing trading assistant with hard risk limits"
   cp trader_profile.md ~/.hermes/profiles/trader/SOUL.md   # persona instructions
   ```

   Note: a new profile does NOT inherit the global model config. Make sure
   `~/.hermes/profiles/trader/config.yaml` has a `model:` block
   (provider/default) — if missing you'll get "Provider authentication
   failed". OAuth credentials live in the shared pool; no need to log in again.

2. **Register this MCP server** (stdio, 13 tools, long and short, incl. a fast direct-order path):

   ```bash
   trader mcp add swing-trading --command uv \
     --args run --directory /absolute/path/hermes-swing-trader python trading_server.py
   trader mcp test swing-trading   # verify connectivity
   ```

3. **Trim permissions**: remove shell, browser, file writes and other tools
   unrelated to trading; keep vision (reading chart screenshots), memos,
   clarifying questions, and cron (end-of-day report):

   ```bash
   for p in cli telegram; do
     trader tools disable --platform $p web browser terminal file code_execution \
       image_gen bfl tts computer_use delegation session_search skills
   done
   ```

4. **Configure the Telegram gateway**: create a **dedicated** bot via
   @BotFather (separate from the TradingView channel's bot) and get its
   token; DM @userinfobot for your numeric user ID; then append to
   `~/.hermes/profiles/trader/.env` (no leading spaces):

   ```
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   TELEGRAM_ALLOWED_USERS=<your numeric user ID>
   TELEGRAM_HOME_CHANNEL=<your numeric user ID>
   ```

   `TELEGRAM_ALLOWED_USERS` is the whitelist and is **required** — anyone who
   can DM the bot can command it; `TELEGRAM_HOME_CHANNEL` is where proactive
   notifications (scheduled daily reports etc.) are delivered — for single
   user setups use the same ID.

5. **Install and start the gateway** (registered as a launchd service on
   macOS: starts at boot, auto-restarts on crash):

   ```bash
   trader gateway install
   trader gateway status
   tail -f ~/.hermes/profiles/trader/logs/gateway.log   # expect "telegram connected"
   ```

6. **Configure the model and fallback** (current verified deployment;
   tool calling tested): primary Kimi K3 (Moonshot international endpoint),
   automatically switching to DeepSeek V4 Pro on rate limits / 5xx.

   Put both API keys into `~/.hermes/profiles/trader/.env`
   (the variable names MUST be the two Hermes recognizes — they differ from
   the names in this repo's `.env`):

   ```
   KIMI_API_KEY=<key from platform.moonshot.ai>
   DEEPSEEK_API_KEY=<key from platform.deepseek.com>
   ```

   Then make sure `~/.hermes/profiles/trader/config.yaml` contains:

   ```yaml
   model:
     default: kimi-k3
     provider: kimi-coding # built-in provider, endpoint api.moonshot.ai/v1
     base_url: ""
   fallback_providers:
     - provider: deepseek # built-in provider, endpoint api.deepseek.com/v1
       model: deepseek-v4-pro
   ```

   Apply with `trader gateway restart`; inspect the chain with
   `trader fallback list`; one-shot verify with
   `trader -z "get an AAPL quote with get_quote"`. To switch models use the
   interactive `trader model` picker (for a trading agent, prefer reasoning
   models with strong tool calling).

### Step 4: Run one full round trip

Send this in Telegram:

> Look at NVDA — if it breaks yesterday's high, buy per the rules

Expected flow: agent checks quotes → verifies entry conditions →
`preview_bracket_buy` risk pre-check → sends you the order summary → you
reply "confirm" → agent executes with the confirm_token → reports the fill.

## TradingView Signal Integration (optional)

Draw breakout levels / set alerts in TradingView; signals enter this
system's risk controls via webhook, and fill on the Alpaca paper account
after Telegram button confirmation. US equities only, long only.

### Prerequisites

1. **Dedicated Telegram bot**: create a new bot via @BotFather (separate
   from Hermes' bot) and get its token; DM @userinfobot for your numeric
   chat ID; send the new bot a /start first
2. **Append to `.env`** (see `.env.example`): `WEBHOOK_SECRET` (random
   string — generate with
   `python -c "import secrets; print(secrets.token_urlsafe(24))"`),
   `TG_BOT_TOKEN`, `TG_CHAT_ID`
3. **Cloudflare Tunnel** (free):
   ```bash
   brew install cloudflared
   cloudflared tunnel --url http://localhost:8787
   ```

Note the printed `https://xxx.trycloudflare.com` URL (quick mode — it
changes on restart; for a stable domain configure a named tunnel with
`cloudflared tunnel create`)

### Start

```bash
uv run python webhook_server.py
```

### TradingView alert setup (requires a paid plan with webhook support)

- Webhook URL: `https://<your-tunnel-domain>/hook/<WEBHOOK_SECRET>`
- Alert Message:
  ```json
  {"secret": "<WEBHOOK_SECRET>", "action": "buy", "symbol": "{{ticker}}", "price": {{close}}}
  ```
  Optionally add `"stop": your_stop_price`; if omitted, the day's low is
  used automatically (matching this system's rules)

### Signal flow

Signal arrives → outside market hours / duplicate / fails risk checks →
Telegram explains why and the signal is voided; passes → you receive an
order summary card (share count auto-sized from the per-trade risk
`webhook_risk_pct_per_trade`%) → tap 【✅ Confirm】 within 30 minutes to
fill, tap 【❌ Discard】 or time out to void. The kill switch
(`KILL_SWITCH`), daily trade limit and all other hard controls are fully
shared with the Hermes channel. Every signal is audited in
`signals_log.jsonl`.

## Day-to-day operations

- **Emergency stop**: `./kill_switch.sh on` (or manually create a file
  named `KILL_SWITCH` in the server directory) instantly blocks all order
  placement/updates; `off` lifts it
- **Tune risk**: edit `risk_config.json`, then restart to apply;
  `trading_server.py` (via Hermes) and `webhook_server.py` are separate
  processes — restart both if both are running
- **Gateway management**: `trader gateway status` / `restart` / `stop`;
  logs at `~/.hermes/profiles/trader/logs/gateway.log`
- **Change trading rules**: after editing `trader_profile.md`, re-run
  `cp trader_profile.md ~/.hermes/profiles/trader/SOUL.md` and
  `trader gateway restart`
- **Disable two-step confirmation** (not recommended): set
  `require_confirmation` to `false` and the agent may place orders
  autonomously within risk limits (the TradingView channel's button
  confirmation is unaffected by this switch)
- **Audit**: all orders are visible in the Alpaca dashboard; TradingView
  signals are logged one-by-one in `signals_log.jsonl`; enabling tool-call
  logging on the Hermes side is recommended

## Safety red lines (once more)

1. Before connecting a real-money account, run the full system on paper for
   at least one month and review every trade
2. API keys go in environment variables/config only — never in profile
   instructions or chat messages
3. The Telegram bot MUST have a user whitelist — anyone who can DM it can
   command it
4. Run the whole system in Docker or on a dedicated machine
5. The model's judgment of web content ("bullish news" etc.) can be
   manipulated (prompt injection) — do not casually disable the
   confirmation step or the hard risk controls

## Disclaimer

This system is for learning and paper-trading research only. Markets are
risky and no model can predict them; evaluate thoroughly on your own before
switching to real funds — you bear all gains and losses.
