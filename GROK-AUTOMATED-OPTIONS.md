# GROK — Implement automated options on the VPS TOS bot

**Audience:** Grok (or any coding agent) working on DennTech's live operator bot.  
**Date:** Friday, August 28, 2026.  
**Not financial advice. Do not invent win rates, ROI, expectancy, or “the bot prints.”**  
**Do not commit, print, or paste `config.json`, tokens, app keys, or account hashes.**

This spec tells you **how to add an options sleeve** to the existing Schwab bot, **what scanner to build** so it only trades names that can actually fill, and **how confirmation + stops work**. Build against the live tree. Do not rewrite the equity ORB book unless the operator says so.

---

## 1. Live system (do not mix these up)

| Thing | Path / fact | What it is |
|---|---|---|
| **This bot** | `/var/www/denntech-site/tos_bot` on VPS `187.124.235.132` | Schwab **Trader API** operator bot. systemd `tos-bot.service`. UI bind `127.0.0.1:5001`, nginx `/tos-trading-2026/` (admin auth). **Not** `/var/www/denntech_site` (underscore path does not exist). |
| **systemd** | `WorkingDirectory=/var/www/denntech-site/tos_bot` | `ExecStart=.../.venv/bin/python main.py --port 5001` |
| **DTS-SITE scanners** | `/var/www/DTS-SITE/scanner/tos_*.py` | Universe screeners for the YouTube/stream scanner. **They do not place orders.** Do **not** edit them for this job. |
| **cryptotradebot.info** | keys-on-PC desktop product | Different product. Do not mix OAuth, keys, or copy. |
| **thinkorswim desktop** | User trades watchlists in TOS | This bot is **not** thinkScript. It talks to Schwab Trader API (`schwab_client.py`). Fills in the Schwab journal can be **bot AND manual TOS** — do not treat all fills as bot fills. |

**Name in systemd:** “TOS ORB Trading Bot.” Auto engine is **equity only**. **Click-ticket options already exist** (see §2). Do not rebuild order JSON from scratch.

Local audit copy (read-only reference, may be stale vs VPS): `/workspace/tos-bot-audit/tos_bot`. Always SSH/read the VPS tree before editing.

---

## 2. What the bot already does (equity)

As of the 2026-08-25 process lock (re-verify on VPS before you touch it):

- `dry_run=True`, `auto_trade=False`, `auto_start_trading=False`
- **ORB only.** Swing / divergence / scalp / RSI / first-pullback **off**. ETH **off**.
- Orders: always **LIMIT**, `assetType: EQUITY`. OCO stop+target. SEAMLESS **rejects STOP** (ETH stop becomes a LIMIT sell). Duration **DAY**.
- Scanner universe: Schwab **movers** up+down + `watchlist` + `VOLUME_MONITOR_LIST`. Engine default **`max_price $10`**. Rel-vol / gap-style gates. **This universe is wrong for liquid options** (NVDA, AAPL, WDAY, MRVL live well above $10).
- Session: RTH 9:30–16:00 ET. EOD flatten ~15:55 ET. Entry cutoff noon ET on ORB.
- Strategy interface: `strategy_base.py` → `evaluate(symbol) -> TradeSignal`. Registry in `strategy_registry.py`. Engine in `trading_engine.py`.

**Auto engine:** shares only. No strategy evaluates options.

**Click-ticket options already live** in `scanner_proxy.py` (verified on VPS 2026-08-28):

- `GET /api/scanner/option-quote`
- `POST /api/scanner/option-place` — single-leg `BUY_TO_OPEN` / `SELL_TO_CLOSE` via the same Schwab trader API (`_trader_request`)
- Caps (env names only): `CLICK_MAX_CONTRACTS`, `CLICK_MAX_OPTION_NOTIONAL` (also `CLICK_MAX_SHARES`, `CLICK_MAX_NOTIONAL` for equity clicks)

That is **manual click from the DTS scanner UI**, not an automated confirmation loop. Reuse this path for the sleeve. Do **not** invent a second order JSON.

Equity ORB stays as-is. Automated options = **new strategy + new universe scanner + level engine** that call the **existing** option-place/quote helpers, gated by flags, default **off** and **dry_run**.

---

## 3. Goal

Add a sleeve that:

1. **Scans** for underlyings whose **options are actually tradable** (tight book, size, listed weeklies).
2. **Finds levels** on the **underlying** (not on the option premium).
3. **Enters only on confirmation** (closed 5-minute bar through the level).
4. **Expresses** the trade as an option (default: next **Wednesday or Friday weekly**, not same-day 0DTE).
5. **Stops on the underlying level**, then exits the option. Do not stop on the option last price.
6. Stays **defined-risk** (long call/put or debit spread). No naked short premium in v1.
7. Never arms live until `dry_run` is off **in the UI** after a bot-only journal exists for this sleeve.

Operator intent (Aug 28, 2026, from the desk): they scalp; they buy **9/02 or 9/04** (then the next Wed/Fri weeklies) **so theta does not kill them while they wait for confirmation**. They do **not** buy Friday 0DTE. They want the bot to find levels and only buy on confirmation with a stop.

---

## 4. Operator rules (hard)

These override any “clever” default you might invent.

### 4.1 Confirmation

- Levels are **5-minute closes** unless the operator says otherwise.
- A poke through the level is **not** an entry. A **completed 5-min bar close** through the level is.
- After a failed break, **do not chase the middle of the box**. Wait for a new close through the **new** lid/floor.
- Example card format the operator uses:

```
NVDA

Calls on confirmation over 219.43-220.90

Puts on confirmation under 218.50-217.89
```

The bot should log the same card when it arms a ticker.

### 4.2 Expiry

- **Never** buy the weekly that expires **today**.
- Default expiries: **next Wednesday weekly** (e.g. 2026-09-02) **or** **next Friday weekly** (e.g. 2026-09-04).
- After those dates, roll the rule: “next Wed or next Fri that is **≥ 1 calendar day away**.”
- If the name has no Wednesday weekly, use Friday. If neither exists, **skip the name**. Do not jump to monthly unless `allow_monthly=True` (default false).

### 4.3 Stop

- Stop lives on the **stock**. Example: long calls armed on close > 219.71; kill if a 5-min **closes back under** 219.43.
- On stop: **sell the option** (or close the debit spread) at LIMIT (bid * (1 − buffer), cap the give-up). Do **not** convert to a market order in the first version.
- Do not place a Schwab STOP on the option as the primary risk control in v1 (wide option prints will fake you out). Optional later: option STOP as a backstop **beyond** the underlying kill.

### 4.4 Structure (v1)

Prefer, in order:

1. **Long call or long put**, ATM or first ITM, next Wed/Fri weekly, size by **max premium dollars**.
2. If the ATM weekly bid-ask is wider than the spread cap (below), **debit vertical** (call debit for longs, put debit for shorts), width 1–2 strikes.

**Do not** in v1: naked short calls/puts, iron condors, 0DTE, earnings-lotto weeklies unless `allow_earnings=True` (default false).

### 4.5 Chop / no-trade

If the underlying is inside a defined box (example: NVDA 217.89–219.83) **do not trade**. Wait until a 5-min close **leaves** the box.

### 4.6 Hours

- Options sleeve: **RTH only** (9:30–15:45 ET last new entry). No ETH options in v1 (spreads are worse; SEAMLESS + options is a separate project).
- No new entries in the last **15 minutes** of RTH.
- Flatten or leave: default **leave** next-week options overnight (`eod_flat_options=False`). Equity ORB still flattens on its own flag. Do not let equity EOD flatten close option positions unless `eod_flat_options=True`.

---

## 5. Architecture

```
                    ┌──────────────────────────────┐
                    │  options_universe_scanner    │  liquid names, not $10 movers
                    └──────────────┬───────────────┘
                                   │ symbols[]
                    ┌──────────────▼───────────────┐
                    │  level_engine (5m OHLC)      │  box, lid, floor, 5m close
                    └──────────────┬───────────────┘
                                   │ Signal(side, break, target, kill)
                    ┌──────────────▼───────────────┐
                    │  contract_selector           │  expiry, strike, spread check
                    └──────────────┬───────────────┘
                                   │ OCC / legs
                    ┌──────────────▼───────────────┐
                    │  schwab_client options API   │  chain, quote, LIMIT, multi-leg
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │  options_risk / flatten      │  underlying stop, max $ , 1 pos
                    └──────────────────────────────┘
```

Wire it as a new `StrategyBase` implementation: `options_confirm_strategy.py` with `strategy_id = "opt_confirm"`. Engine polls it **only if** `use_options_confirm=True`.

Keep equity ORB on its scanner. **Do not** run the $10 rel-vol scanner as the options universe.

---

## 6. The scanner (this is the hard part)

The current `scanner.py` is built for **cheap equity ORB**: movers, rel-vol, gap %, float, **max_price $10**. That will **miss** every name the operator actually scalped today (NVDA, AAPL, MRVL, WDAY) and **catch** junk like dilution runners (WCT) that have **no tradable options**.

You need a **second scanner**: `options_universe.py`.

### 6.1 Job

Every N seconds (default 60), emit a list of underlyings that:

- Have **listed weekly options** (Wed and/or Fri).
- Have a **tradable** ATM weekly (spread, size, OI).
- Are **liquid in the stock** so 5-min levels mean something.
- Are **not** halted / not a $0.15 offering / not a 1-cent name.

Rank by **tradability**, not by today’s % change. (The under-$10 desk rule still applies here: do not rank the options universe by same-day rvol or % as the primary score. Liquidity and spread first. Today’s range can be a **column**, not the sort.)

### 6.2 Seed universe (v1, keep it small)

Do **not** scan the whole market in v1. Start with a **core liquid list** plus the operator watchlist.

Hard-coded core (same idea as DTS `unusual_options.py` `_CORE_UNDERLYINGS`, but this bot must use **Schwab**, not Alpaca, unless you add Alpaca as a **read-only** chain helper — prefer Schwab so one broker = fill + data):

```
SPY QQQ IWM
AAPL MSFT NVDA AMZN META TSLA AMD GOOGL AVGO
MRVL MU INTC
CRWD WDAY ADSK AFRM
NFLX CRM ORCL
JPM BAC XOM
GLD TLT
```

Plus `config.json` key `options_watchlist: []` for names the operator adds (e.g. IREN — only if the chain passes the tradability gates; if it fails, log `SKIP IREN: spread` and do not trade it).

Cap: **40 names**. Quality over coverage.

### 6.3 Per-name tradability gates (all must pass)

Pull Schwab option chain for the **candidate expiry** (next Wed, else next Fri). Look at **ATM** call and put (strike nearest last).

| Gate | Default | Why |
|---|---|---|
| Underlying last | $20–$600 (indices/ETFs exempt) | Penny names have garbage options |
| Underlying ADV | ≥ 5,000,000 shares **or** 20-day avg $ volume ≥ $50M | 5-min levels need a real tape |
| Bid-ask on ATM weekly | spread ≤ **8% of mid** **and** ≤ **$0.15** | Operator scalps; a $0.40-wide book is the stop |
| Mid | ≥ $0.40 | Avoid 5-cent lottery tickets |
| Open interest ATM | ≥ 500 | Or skip |
| Option volume today (ATM) | ≥ 100 **or** underlying is in core mega (NVDA/AAPL/SPY) | First 30 min of RTH: OI can substitute |
| Weekly expiry exists | next Wed or Fri ≥ 1 day out | No 0DTE |
| Multiplier | 100 | Skip non-standard |
| Halt / LULD | skip | |
| Spread as % of expected move | optional later | |

If **both** call and put fail the spread cap, **drop the name** for that scan cycle. Do not “pick the less-bad side.”

### 6.4 What this scanner is not

- Not the DTS-SITE `UnusualOptionsScanner` (Alpaca, ranks by contract volume). You may **read** that code for OCC parsing (`parse_occ_symbol`) but **do not import Alpaca into the order path**.
- Not the under-$10 quiet-swing scanner (fundamentals + base). That desk still exists; it does not feed this sleeve.
- Not “unusual options flow” as an entry. Flow can be a **later overlay**. v1 entry is **level confirmation on the stock**.

### 6.5 Output record

```python
{
  "symbol": "NVDA",
  "last": 218.47,
  "expiry": "2026-09-02",          # chosen Wed/Fri
  "atm_strike": 220.0,
  "call": {"bid": 4.20, "ask": 4.35, "oi": 12000, "vol": 8000, "occ": "..."},
  "put":  {"bid": 5.10, "ask": 5.25, "oi": 9000,  "vol": 7000, "occ": "..."},
  "tradable": True,
  "skip_reason": None,
}
```

Log skips with a reason. The operator will ask why WCT never fired. Answer: no chain / failed spread / below $20.

---

## 7. Level engine (underlying)

New module: `level_engine.py`. Input: Schwab 1-min candles for the session (already in `scanner.py` / `get_price_history`). Build **5-min bars** from completed 1-min bars. **Never** use the in-progress 5-min as a “close.”

### 7.1 Box

For the **current RTH session**:

- `session_high`, `session_low`
- After a dump + coil (bear-flag shape): `box_low` = session low or the **rising floor** of the last N 5-min lows (e.g. last 8 bars). `box_high` = recent 5-min rejection highs.

v1 can be simpler and still match how the operator traded NVDA today:

1. Track last **swing high** and **swing low** on 5-min (pivot: high with lower highs on both sides).
2. `call_break` = last rejected 5-min high (or session high of the coil).
3. `put_break` = last defended 5-min low (or session low).
4. `call_target` = next level up (prior breakdown level, e.g. NVDA 220.90 Thursday low / day’s first breakdown).
5. `put_target` = next level down (session low, then extension).

If you cannot find a clean two-sided box, **do not invent levels**. Skip the name until pivots exist (need at least ~6 completed 5-min bars → after ~10:00 ET).

### 7.2 Confirmation

```
CALL signal: last COMPLETED 5-min close > call_break
             and prior 5-min close <= call_break   # first close through
PUT  signal: last COMPLETED 5-min close < put_break
             and prior 5-min close >= put_break
```

Invalidation (kill):

```
long calls:  completed 5-min close < kill_level   # usually the break that just flipped, or the pre-break close
long puts:   completed 5-min close > kill_level
```

**Do not** fire a second signal in the same box after a fail until a **new** break level is printed (higher high / lower low).

### 7.3 Anti-chase

If `abs(last - break) / break > 0.004` (0.4%) already **after** the close, skip — you missed it. Wait for a **retest fail/hold** of that break as new support/resistance (1 completed 5-min hold), then enter.

This is the “wait for the retest” rule the operator asked for on NVDA.

---

## 8. Contract selection

Module: `contract_selector.py`.

Given `side` (call/put), `symbol`, `last`, `expiry`:

1. Fetch chain for that expiry. **TODO:VERIFY** Schwab path: Trader API marketdata chains (login-walled docs). Typical: `GET /marketdata/v1/chains?symbol=NVDA&contractType=ALL&strikeCount=10&includeQuotes=TRUE&fromDate=YYYY-MM-DD&toDate=YYYY-MM-DD`. Confirm on [developer.schwab.com](https://developer.schwab.com) before coding the URL. Do not guess a second path if the first 401s — check token scopes (`MoveMoney` vs market data).
2. Pick strike: **ATM or first ITM** in the trade direction (calls: strike ≤ last, nearest; puts: strike ≥ last, nearest). Config `options_strike=atm|itm1`.
3. Re-check spread gates on **that** contract (not yesterday’s ATM if the stock has moved).
4. Size:

```
max_premium_dollars = cfg.get("options_max_premium", 150)   # $150 default
multiplier = 100
contracts = floor(max_premium_dollars / (ask * multiplier))
if contracts < 1: skip
```

Cap `options_max_contracts` default **2**. One underlying at a time (`options_max_concurrent` default **1**).

5. Debit spread fallback: if ATM spread fails the cap, try vertical: buy ATM, sell 1–2 strikes OTM, net debit ≤ `options_max_premium`, both legs OI ≥ 200. If that fails, skip.

OCC: `ROOT + YYMMDD + C|P + strike*1000 padded to 8`. Reuse the regex in DTS `unusual_options.py` `parse_occ_symbol` (copy the function, don’t import the scanner package).

---

## 9. Schwab client additions (`schwab_client.py`)

First **extract/reuse** `scanner_proxy.py` option-quote and option-place (they already POST Schwab `assetType: OPTION`). Then expose the same helpers on `schwab_client.py` if the strategy should not go through Flask. Do not break equity `place_market_order`. Do not duplicate a second payload if the click-ticket one already fills.

```python
def get_option_chain(self, symbol, from_date, to_date, contract_type="ALL") -> dict | None: ...
def get_option_quote(self, occ_symbol) -> dict | None: ...
def place_option_limit(self, occ_symbol, quantity, price, instruction="BUY_TO_OPEN") -> str | None: ...
def place_vertical_debit(self, long_occ, short_occ, quantity, net_debit) -> str | None: ...
def close_option_limit(self, occ_symbol, quantity, price, instruction="SELL_TO_CLOSE") -> str | None: ...
```

Order JSON (v1 single-leg) — **TODO:VERIFY** field names against Schwab Trader API order spec:

- `orderType: LIMIT`
- `session: NORMAL` (RTH only)
- `duration: DAY`
- `orderStrategyType: SINGLE`
- leg `instruction`: `BUY_TO_OPEN` / `SELL_TO_CLOSE`
- `instrument`: `{ "symbol": occ, "assetType": "OPTION" }`

Vertical: `orderStrategyType: VERTICAL` or `TRIGGER`/`OCO` as docs require. **Do not invent multi-leg JSON.** If the spec is unclear, implement **single-leg only** in v1 and leave a `TODO:VERIFY` for verticals.

Always LIMIT. Buffer: buy at `min(ask, mid + $0.02)` not `ask * 1.005` (that equity buffer is wrong on a $4 option). Sell at `max(bid, mid - $0.02)`.

If `dry_run`: log the payload, return `DRYRUN-<uuid>`, **do not POST**.

---

## 10. Engine integration (`trading_engine.py`)

New flags (defaults **safe**):

```python
use_options_confirm = False
options_dry_run = True          # independent of equity dry_run if you can; else inherit dry_run
options_expiry_mode = "wed_then_fri"   # wed_then_fri | fri_only
options_max_premium = 150
options_max_contracts = 2
options_max_concurrent = 1
options_strike = "atm"
options_eod_flat = False
options_last_entry_et = (15, 45)
options_watchlist = []
```

Loop (RTH, if enabled):

1. Refresh universe every 60s.
2. For each tradable symbol with no open options position: `level_engine.evaluate` → maybe signal.
3. `contract_selector` → OCC.
4. `place_option_limit` if `auto_trade and not dry_run` **and** `use_options_confirm`.
5. Manage: every 5-min close, if kill level prints → `close_option_limit`. If target 5-min close through `target` → close (or 50% — v1 **full close** is fine).
6. Journal: `source=bot`, `strategy_id=opt_confirm`, store underlying levels + OCC + expiry. Do not mix with equity ORB rows without the strategy_id.

**Do not** let `_flatten_all("eod_flat")` close option positions unless `options_eod_flat`.

Dashboard: new tab using `get_tab_data`. Show the card (calls over X–Y / puts under A–B), last 5-min close, dry_run badge.

---

## 11. Files to add / change

**Add**

| File | Role |
|---|---|
| `options_universe.py` | Tradability scanner |
| `level_engine.py` | 5-min box + confirmation |
| `contract_selector.py` | Expiry, strike, size, OCC |
| `options_confirm_strategy.py` | `StrategyBase` wrapper |
| `GROK-AUTOMATED-OPTIONS.md` | This spec (keep next to the bot or in `BOT-UPGRADE/`) |

**Change**

| File | Change |
|---|---|
| `scanner_proxy.py` | **Existing** option-quote / option-place. Reuse. Add chain list if missing. |
| `schwab_client.py` | Lift quote/place helpers here if the engine should not call Flask. Chain fetch. Vertical only if verified. |
| `strategy_registry.py` | Register `opt_confirm`, default **off** |
| `trading_engine.py` | Flags, poll, do not EOD-flatten options |
| `web_dashboard.py` | Tab + cards + dry_run |
| `journal.py` | Options fields (OCC, expiry, underlying kill) |

**Do not change** (unless a bug blocks the sleeve)

- DTS-SITE `/var/www/DTS-SITE/scanner/**`
- Equity ORB defaults from the 8/25 lock (`dry_run` / ORB-only) except adding **new** flags defaulting off
- `cryptotradebot.info` anything

Backup VPS tree before patch: `/var/www/denntech-site/tos_bot/_bak_options_<UTC>/`

---

## 12. Implementation sequence (do this order)

1. **Read live VPS** `/var/www/denntech-site/tos_bot` (not only the Aug 25 audit copy). Confirm `dry_run`, flags, `schwab_client.py` still has no options methods.
2. **Verify Schwab chain + option order JSON** against current Trader API docs. Mark remaining unknowns `TODO:VERIFY`. Do not guess VERTICAL payloads.
3. `get_option_chain` + `get_option_quote` + unit-style smoke: one symbol NVDA, print expiry list, ATM bid/ask (no order).
4. `options_universe.py` on the core list. Log pass/fail reasons.
5. `level_engine.py` replay **today’s NVDA** 1-min (Aug 28): box 217.89–219.83, third reject, roll to 218.50, **no auto put until 5-min close under 218.50**. If your engine fires puts at 218.93 on “third reject,” it is **wrong**.
6. `contract_selector` dry quotes for NVDA 9/02 and 9/04.
7. `place_option_limit` **dry_run only**. Log payload.
8. Wire strategy + dashboard. Keep `use_options_confirm=False`.
9. Paper/dry journal for a full RTH session.
10. Operator turns the flag on. You do **not** turn `dry_run` off yourself.

---

## 13. Risk

- `options_max_premium` is the real stop on **debit**. Underlying kill is the **time** stop.
- One name, small size. No martingale after a failed break.
- PDT/IMD: equity day-trades are a separate clock. Options opening/closing can still trip pattern flags — **do not invent IMD math**. If the account is restricted, the sleeve must no-op and log it.
- WCT-class names (dilution, $0.15 deal, $0.90 tape): scanner must **exclude**. Options on that junk are a gift to the market maker.
- No profitability claim in the UI, README, or stream copy.

---

## 14. Acceptance tests

A build is done when **all** of these are true:

- [ ] `use_options_confirm` default False; equity ORB behavior unchanged.
- [ ] NVDA-class names can pass the universe; WCT-class names cannot.
- [ ] No order POST when `dry_run` or `auto_trade` is False.
- [ ] 0DTE (expiry = today) is impossible in `contract_selector`.
- [ ] Entry requires a **completed** 5-min close through the level; a 1-min poke does not fire.
- [ ] After a failed break, no entry in the mid-box.
- [ ] Stop is evaluated on **underlying 5-min close**, then option is LIMITed out.
- [ ] Journal rows have `strategy_id=opt_confirm` and OCC.
- [ ] EOD equity flatten does not close the options sleeve unless `options_eod_flat`.
- [ ] No secrets in git, logs to disk that get copied, or this markdown.

Replay fixture (operator tape, Aug 28 2026 CT):

| Time (CT) | NVDA | Correct bot behavior |
|---|---|---|
| 12:10 | low 217.89 | box floor |
| 12:17 | 1-min close 219.20 | **not** a 5-min confirm |
| 12:20 / 12:25 / 12:35 | 5-min rejects of ~218.90–219.43 | **no puts** from 218.66–218.93 |
| 12:45 | 5-min close 219.57 over 219.43 | call **candidate** only if still the live break; then 12:52 high 219.83 became the new lid |
| 13:17 | 218.35, last 218.47 | **not** 217.89; puts only on **5-min close under 218.50** |

If the bot shorts the “third rejection” at 218.93, fail the test.

---

## 15. Team (only if you need them)

Operator said the desk may help. Do **not** fan out by default.

| Agent | Use for |
|---|---|
| Hedge Fund manager/CEO | Levels, confirmation rules, “is this name even an options name” |
| Stock Scanner / DTS scanner agents | Universe ideas; they do **not** place Schwab orders |
| Ai Trading Bot CEO / Quant / Risk / Execution | Separate Kraken/Schwab **paper** book — do not steal their keys or mix books |
| Scanner Debugger / Project Manager | `/var/www/DTS-SITE/scanner` only |

This options sleeve lives **only** in `/var/www/denntech-site/tos_bot`.

---

## 16. TODO:VERIFY (do not skip)

- [ ] Schwab Trader API **chains** URL, query params, and quote fields (bid, ask, OI, volume, multiplier).
- [ ] Option **order** JSON (`assetType OPTION`, instructions `BUY_TO_OPEN` / `SELL_TO_CLOSE`).
- [ ] Whether VERTICAL / multi-leg is documented for this app’s OAuth scopes.
- [ ] Live VPS `config.json` flags **without printing secrets** (keys only).
- [ ] Whether NVDA/AAPL have Wednesday weeklies on this broker (most do; confirm).
- [ ] Token scopes include market data **and** options trading. If options trading is not enabled on the Schwab account, **stop** and tell the operator — do not paper over with equity.

---

## 17. One-paragraph summary for Grok

The VPS bot at `/var/www/denntech-site/tos_bot` is a Schwab equity ORB auto-engine. Click-ticket single-leg options already POST through `scanner_proxy.py`. Add a separate, default-off sleeve that **reuses that order path**: scan ~40 liquid names for **tight weekly options**, map **5-minute levels on the stock**, enter **only on a completed 5-min close** through the level, buy the **next Wed or Fri weekly** (never today), size by a small premium cap, and **kill on the underlying close** the other way. Keep `dry_run` on. Do not touch DTS-SITE scanners, do not mix cryptotradebot.info, do not invent win rates, do not turn live orders on yourself.
