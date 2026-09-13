"""
journal.py
Trade Journal — processes and aggregates trade history from:
  • Schwab API  (all account activity — bot AND manual TOS trades)
  • Local bot data (positions.json / trade_log.json) — fallback + labelling
"""

import json
import os
import re
import datetime
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set



def _normalize_ts(ts: str) -> str:
    """Normalize timestamps for consistent JSON / JS parsing."""
    if not ts:
        return ""
    s = str(ts).strip()
    # Schwab: 2026-07-09T19:18:08+0000 → Z
    m = re.match(r"^(.+)[+-]\d{4}$", s)
    if m:
        return m.group(1) + "Z"
    return s


DATA_DIR      = os.environ.get("JOURNAL_DATA_DIR") or "saved_data"
JOURNAL_CACHE = os.path.join(DATA_DIR, "journal_cache.json")
TRADE_LOG     = os.path.join(DATA_DIR, "trade_log.json")
POSITIONS     = os.path.join(DATA_DIR, "positions.json")
PAPER_BOOK    = os.path.join(DATA_DIR, "paper_account.json")


def set_data_dir(path: str) -> None:
    """Point cache/log paths at a writable folder (desktop app)."""
    global DATA_DIR, JOURNAL_CACHE, TRADE_LOG, POSITIONS, PAPER_BOOK
    DATA_DIR = str(path or "saved_data")
    JOURNAL_CACHE = os.path.join(DATA_DIR, "journal_cache.json")
    TRADE_LOG = os.path.join(DATA_DIR, "trade_log.json")
    POSITIONS = os.path.join(DATA_DIR, "positions.json")
    PAPER_BOOK = os.path.join(DATA_DIR, "paper_account.json")


# ─────────────────────────────────────────────────────────────────────────────
# Schwab transaction parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_schwab_transactions(raw: List[Dict]) -> List[Dict]:
    """Convert raw Schwab transaction objects into normalised fill records.

    Schwab returns one transaction per activity.  Each transaction has
    transferItems containing the equity leg (qty + price).
    amount > 0 → BUY shares,  amount < 0 → SELL shares.
    """
    fills: List[Dict] = []
    for tx in raw:
        if tx.get("type") != "TRADE":
            continue
        order_id = str(tx.get("orderId", ""))
        tx_time  = tx.get("time") or tx.get("tradeDate", "")

        for item in tx.get("transferItems", []):
            instr  = item.get("instrument", {})
            symbol = instr.get("symbol", "")
            if not symbol:
                continue
            # Schwab: amount = shares (positive = bought, negative = sold)
            amount = float(item.get("amount", 0) or 0)
            price  = float(item.get("price",  0) or 0)
            if amount == 0 or price == 0:
                continue
            fills.append({
                "symbol":   symbol,
                "qty":      abs(amount),
                "price":    price,
                "side":     "BUY" if amount > 0 else "SELL",
                "time":     tx_time,
                "order_id": order_id,
            })
    return fills


# ─────────────────────────────────────────────────────────────────────────────
# Round-trip matching (FIFO)
# ─────────────────────────────────────────────────────────────────────────────

def match_round_trips(fills: List[Dict]) -> List[Dict]:
    """FIFO-match BUY fills to SELL fills to produce completed trade records."""
    by_symbol: Dict[str, List] = defaultdict(list)
    for f in sorted(fills, key=lambda x: x["time"]):
        by_symbol[f["symbol"]].append(f)

    trades: List[Dict] = []
    for symbol, sym_fills in by_symbol.items():
        open_lots: deque = deque()

        for fill in sym_fills:
            if fill["side"] == "BUY":
                open_lots.append({
                    "qty":      fill["qty"],
                    "price":    fill["price"],
                    "time":     fill["time"],
                    "order_id": fill["order_id"],
                })
            elif fill["side"] == "SELL" and open_lots:
                remaining  = fill["qty"]
                cost_basis = 0.0
                entry_time = None
                entry_ids: List[str] = []

                while remaining > 0 and open_lots:
                    lot     = open_lots[0]
                    matched = min(lot["qty"], remaining)
                    cost_basis += matched * lot["price"]
                    entry_time  = entry_time or lot["time"]
                    entry_ids.append(lot["order_id"])
                    lot["qty"] -= matched
                    remaining  -= matched
                    if lot["qty"] <= 0:
                        open_lots.popleft()

                matched_qty = fill["qty"] - remaining
                if matched_qty > 0 and cost_basis > 0:
                    avg_entry = cost_basis / matched_qty
                    pnl       = (fill["price"] - avg_entry) * matched_qty
                    pnl_pct   = round((fill["price"] / avg_entry - 1) * 100, 2) if avg_entry else 0
                    trades.append({
                        "symbol":           symbol,
                        "direction":        "LONG",
                        "qty":              round(matched_qty, 0),
                        "entry_price":      round(avg_entry, 4),
                        "exit_price":       fill["price"],
                        "pnl":              round(pnl, 2),
                        "pnl_pct":          pnl_pct,
                        "entry_time":       _normalize_ts(entry_time or ""),
                        "exit_time":        _normalize_ts(fill["time"]),
                        "winner":           pnl > 0,
                        "entry_order_ids":  entry_ids,
                        "exit_order_id":    fill["order_id"],
                    })

    return sorted(trades, key=lambda x: x["exit_time"], reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# Source labelling (Bot vs Manual)
# ─────────────────────────────────────────────────────────────────────────────

def get_bot_order_ids() -> Set[str]:
    """Return live bot order IDs from trade_log.json. Paper ids are excluded."""
    try:
        log = json.load(open(TRADE_LOG, encoding="utf-8"))
        ids = set()
        for t in log:
            oid = str(t.get("order_id") or "")
            if oid and not oid.startswith("PAPER"):
                ids.add(oid)
        return ids
    except Exception:
        return set()


def _is_paper_trade(t: Dict) -> bool:
    if t.get("paper") is True:
        return True
    src = str(t.get("source") or "")
    if src.lower().startswith("paper"):
        return True
    ids = [str(x) for x in (t.get("entry_order_ids") or []) if x]
    ids.append(str(t.get("exit_order_id") or ""))
    ids.append(str(t.get("order_id") or ""))
    return any(i.startswith("PAPER") for i in ids)


def label_sources(trades: List[Dict], bot_ids: Set[str]) -> List[Dict]:
    """Tag each trade Paper / Bot / Manual. Paper never mixes with Schwab fills."""
    live_bot_ids = {i for i in bot_ids if i and not str(i).startswith("PAPER")}
    for t in trades:
        if _is_paper_trade(t):
            t["source"] = "Paper"
            t["paper"] = True
            continue
        is_bot = (
            any(str(oid) in live_bot_ids for oid in t.get("entry_order_ids", []))
            or str(t.get("exit_order_id", "") or "") in live_bot_ids
        )
        t["source"] = "Bot" if is_bot else "Manual"
        t["paper"] = False
    return trades




# ─────────────────────────────────────────────────────────────────────────────
# Strategy tagging (from entry_reason in positions.json)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_entry_reasons_from_logs() -> Dict[str, str]:
    """Build order-id → entry_reason map from bot.log* signal + order lines."""
    mapping: Dict[str, str] = {}
    pending: Dict[str, str] = {}       # symbol → reason from latest signal
    momentum_syms: Set[str] = set()

    log_files = ["bot.log", "bot.log.1", "bot.log.2", "bot.log.3"]
    for fname in log_files:
        path = os.path.join(DATA_DIR, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    mo = re.search(r"\[ORB\] (\w+) momentum bypass", line)
                    if mo:
                        momentum_syms.add(mo.group(1).upper())

                    div_m = re.search(r"\[DIV\] SIGNAL LONG (\w+) @", line)
                    if div_m:
                        sym = div_m.group(1).upper()
                        tail = line.split("SIGNAL LONG", 1)[-1].strip()
                        if "div_swing" in line.lower() or "bb21" in line.lower():
                            pending[sym] = f"div_swing LONG {tail[:100]}"
                        else:
                            pending[sym] = f"Divergence LONG {tail[:100]}"
                        continue

                    orb_m = re.search(
                        r"\[ORB\] SIGNAL: LONG (\w+) entry=([^\s]+).*?rsi=([\d.]+).*?vol=([\d.]+x)",
                        line,
                    )
                    if orb_m:
                        sym, entry, rsi, vol = orb_m.groups()
                        sym = sym.upper()
                        kind = "momentum" if sym in momentum_syms else "breakout"
                        pending[sym] = (
                            f"ORB LONG {kind} entry={entry} (RSI={rsi} vol={vol})"
                        )
                        continue

                    ord_m = re.search(
                        r"\[ORDER\] (?:Placing )?LIMIT BUY \d+ (\w+).*?ID: (\d+)",
                        line,
                    )
                    if ord_m:
                        sym, oid = ord_m.group(1).upper(), ord_m.group(2)
                        if sym in pending:
                            mapping[oid] = pending[sym]
        except Exception:
            continue
    return mapping


def get_entry_reason_by_order_id() -> Dict[str, str]:
    """Map entry order IDs to bot entry_reason strings."""
    mapping: Dict[str, str] = _parse_entry_reasons_from_logs()
    try:
        pos = json.load(open(POSITIONS, encoding="utf-8"))
        for bucket in ("open", "closed"):
            for p in pos.get(bucket, []):
                oid = str(p.get("entry_order_id", "") or "")
                reason = str(p.get("entry_reason", "") or "").strip()
                if oid and reason:
                    mapping[oid] = reason  # positions.json wins over log
    except Exception:
        pass
    return mapping


def classify_strategy(entry_reason: str, source: str = "") -> Dict[str, str]:
    """Return display label + filter key from entry_reason text."""
    r = (entry_reason or "").strip()
    rl = r.lower()
    src = (source or "").strip()

    if not rl:
        if src.startswith("Manual"):
            return {"strategy": "Manual", "strategy_key": "manual"}
        return {"strategy": "—", "strategy_key": "unknown"}

    if "div_swing" in rl:
        return {"strategy": "Div Swing", "strategy_key": "div_swing"}
    if "divergence long" in rl:
        return {"strategy": "Divergence", "strategy_key": "divergence"}
    if "bb21" in rl or ("lower=" in rl and "mid=" in rl):
        return {"strategy": "Divergence", "strategy_key": "divergence"}
    if rl.startswith("orb") or "orb long" in rl or "orb short" in rl:
        if "momentum" in rl:
            return {"strategy": "ORB · Momentum", "strategy_key": "orb_momentum"}
        if "pullback" in rl:
            return {"strategy": "ORB · Pullback", "strategy_key": "orb_pullback"}
        return {"strategy": "ORB", "strategy_key": "orb"}
    if "opt_confirm" in rl or rl.startswith("opt ") or ("weekly" in rl and ("call" in rl or "put" in rl)):
        return {"strategy": "Options Confirm", "strategy_key": "opt_confirm"}
    if "scalp" in rl:
        return {"strategy": "Scalp", "strategy_key": "scalp"}
    if "first_pullback" in rl or ("pullback" in rl and "orb" not in rl):
        return {"strategy": "First Pullback", "strategy_key": "fp"}
    if "swing" in rl:
        return {"strategy": "Swing", "strategy_key": "swing"}
    return {"strategy": "Other", "strategy_key": "other"}


def enrich_trades(trades: List[Dict]) -> List[Dict]:
    """Attach entry_reason + strategy tags to trade records."""
    reason_map = get_entry_reason_by_order_id()
    for t in trades:
        reason = str(t.get("entry_reason", "") or "").strip()
        if not reason:
            for oid in t.get("entry_order_ids", []) or []:
                if str(oid) in reason_map:
                    reason = reason_map[str(oid)]
                    break
        t["entry_reason"] = reason
        if _is_paper_trade(t):
            t["source"] = "Paper"
            t["paper"] = True
        tags = classify_strategy(reason, t.get("source", ""))
        t["strategy"] = tags["strategy"]
        t["strategy_key"] = tags["strategy_key"]
    return trades

# ─────────────────────────────────────────────────────────────────────────────
# Local fallback (bot positions.json)
# ─────────────────────────────────────────────────────────────────────────────



def load_trades_from_trade_log() -> List[Dict]:
    """Build completed round-trip records from saved trade_log.json."""
    try:
        log = json.load(open(TRADE_LOG, encoding="utf-8"))
    except Exception:
        return []

    from collections import deque
    open_lots: Dict[str, deque] = defaultdict(deque)
    trades: List[Dict] = []

    for rec in log:
        sym = (rec.get("symbol") or "").upper()
        if not sym:
            continue

        is_open = (
            (rec.get("status") == "filled" and rec.get("shares"))
            or rec.get("paper")
            or str(rec.get("source") or "").lower().startswith("paper")
            or str(rec.get("order_id") or "").startswith("PAPER-")
        ) and rec.get("exit_price") is None and rec.get("shares")
        if is_open:
            open_lots[sym].append({
                "shares":   int(rec.get("shares", 0) or 0),
                "entry":    float(rec.get("entry", 0) or 0),
                "order_id": str(rec.get("order_id", "") or ""),
                "time":     str(rec.get("time", "") or ""),
                "paper":    True if rec.get("paper") or str(rec.get("order_id") or "").startswith("PAPER") else False,
                "reason":   str(rec.get("strategy_id") or rec.get("entry_reason") or ""),
            })
            continue

        if rec.get("exit_price") is None:
            continue

        exit_p = float(rec.get("exit_price", 0) or 0)
        pnl    = float(rec.get("pnl", 0) or 0)
        reason = str(rec.get("reason", "") or "")
        lot    = open_lots[sym].popleft() if open_lots[sym] else None

        qty   = int((lot or {}).get("shares", 0) or 0)
        entry = float((lot or {}).get("entry", 0) or 0)
        if qty <= 0:
            qty = 1
        if entry <= 0 and pnl != 0:
            entry = exit_p - (pnl / qty)

        pnl_pct = round((exit_p / entry - 1) * 100, 2) if entry else 0.0
        trades.append({
            "symbol":          sym,
            "direction":       "LONG",
            "qty":             qty,
            "entry_price":     round(entry, 4),
            "exit_price":      round(exit_p, 4),
            "pnl":             round(pnl, 2),
            "pnl_pct":         pnl_pct,
            "entry_time":      _normalize_ts((lot or {}).get("time", "") or ""),
            "exit_time":       _normalize_ts(str(rec.get("time", "") or rec.get("entry_time", "") or "")),
            "winner":          pnl > 0,
            "entry_order_ids": [lot["order_id"]] if lot and lot.get("order_id") else [],
            "exit_order_id":   str(rec.get("order_id", "") or ""),
            "source":          "Paper" if (lot or {}).get("paper") or rec.get("paper") or str(rec.get("source") or "").lower().startswith("paper") else "Bot",
            "paper":           bool((lot or {}).get("paper") or rec.get("paper")),
            "exit_reason":     reason,
            "entry_reason":    str((lot or {}).get("reason") or rec.get("strategy_id") or ""),
        })

    return sorted(trades, key=lambda x: (x.get("exit_time") or "", x.get("symbol") or ""), reverse=True)


def load_all_local_trades() -> List[Dict]:
    """Merge closed positions + trade_log round-trips (deduped)."""
    seen = set()
    merged: List[Dict] = []

    def _key(t: Dict) -> tuple:
        return (
            t.get("symbol"),
            t.get("qty"),
            round(float(t.get("entry_price", 0) or 0), 4),
            round(float(t.get("exit_price", 0) or 0), 4),
            round(float(t.get("pnl", 0) or 0), 2),
        )

    for src_fn in (load_bot_trades_local, load_trades_from_trade_log, load_paper_trades):
        for t in src_fn():
            k = _key(t)
            if k in seen:
                continue
            seen.add(k)
            merged.append(t)

    return sorted(merged, key=lambda x: x.get("exit_time", ""), reverse=True)


def load_paper_trades() -> List[Dict]:
    """Completed paper round-trips from the $5,000 paper book."""
    try:
        raw = json.load(open(PAPER_BOOK, encoding="utf-8"))
    except Exception:
        return []
    rows = list(raw.get("closed_journal") or [])
    out: List[Dict] = []
    for p in rows:
        if not p.get("exit_price"):
            continue
        row = dict(p)
        row["source"] = "Paper"
        row["paper"] = True
        row["winner"] = bool(row.get("winner") if "winner" in row else float(row.get("pnl") or 0) > 0)
        out.append(row)
    return out


def merge_paper_into(trades: List[Dict]) -> List[Dict]:
    """Keep Schwab/manual rows, add paper rows that are not already present."""
    merged: List[Dict] = []
    seen = set()

    def _k(t: Dict) -> tuple:
        return (
            "Paper" if _is_paper_trade(t) else (t.get("source") or ""),
            t.get("symbol"),
            t.get("qty"),
            round(float(t.get("entry_price") or 0), 4),
            round(float(t.get("exit_price") or 0), 4),
            str(t.get("exit_order_id") or ""),
        )

    for t in trades or []:
        if _is_paper_trade(t):
            t = dict(t)
            t["source"] = "Paper"
            t["paper"] = True
        k = _k(t)
        if k in seen:
            continue
        seen.add(k)
        merged.append(t)
    for p in load_paper_trades():
        k = _k(p)
        if k in seen:
            continue
        seen.add(k)
        merged.append(p)
    return sorted(merged, key=lambda x: x.get("exit_time") or "", reverse=True)


def trade_identity(t: Dict) -> tuple:
    """Stable key so the same fill is not stored twice."""
    oid = str(t.get("exit_order_id") or "").strip()
    paper = "P" if _is_paper_trade(t) else "L"
    if oid:
        return ("oid", paper, oid)
    try:
        qty = round(float(t.get("qty") or 0), 4)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        entry = round(float(t.get("entry_price") or 0), 4)
    except (TypeError, ValueError):
        entry = 0.0
    try:
        exit_p = round(float(t.get("exit_price") or 0), 4)
    except (TypeError, ValueError):
        exit_p = 0.0
    return (
        "row",
        paper,
        str(t.get("symbol") or "").upper(),
        qty,
        entry,
        exit_p,
        str(t.get("exit_time") or ""),
    )


def union_trades(incoming: List[Dict], existing: List[Dict]) -> List[Dict]:
    """Keep every unique trade. Incoming wins when the identity matches.

    Schwab refresh only returns a recent window. Union with the saved cache so
    older months never drop.
    """
    merged: List[Dict] = []
    seen = set()
    for t in list(incoming or []) + list(existing or []):
        if not isinstance(t, dict):
            continue
        k = trade_identity(t)
        if k in seen:
            continue
        seen.add(k)
        merged.append(dict(t))
    merged.sort(key=lambda x: x.get("exit_time") or "", reverse=True)
    return merged


def union_with_saved_cache(trades: List[Dict]) -> List[Dict]:
    cached = load_cache() or {}
    return union_trades(trades, cached.get("trades") or [])


def load_bot_trades_local() -> List[Dict]:
    """Load completed trades from the bot's positions.json (offline fallback)."""
    try:
        pos = json.load(open(POSITIONS, encoding="utf-8"))
        result: List[Dict] = []
        for p in pos.get("closed", []):
            if not p.get("exit_price"):
                continue
            entry = float(p.get("entry_price", 0))
            exit_ = float(p.get("exit_price",  0))
            qty   = int(p.get("shares",        0))
            pnl   = float(p.get("pnl",         0))
            pnl_pct = round((exit_ / entry - 1) * 100, 2) if entry else 0
            result.append({
                "symbol":           p.get("symbol", "?"),
                "direction":        p.get("direction", "LONG"),
                "qty":              qty,
                "entry_price":      entry,
                "exit_price":       exit_,
                "pnl":              round(pnl, 2),
                "pnl_pct":          pnl_pct,
                "entry_time":       _normalize_ts(str(p.get("entry_time", ""))),
                "exit_time":        _normalize_ts(str(p.get("exit_time",  ""))),
                "winner":           pnl > 0,
                "entry_order_ids":  [str(p.get("entry_order_id", ""))],
                "exit_order_id":    str(p.get("oco_order_id",    "")),
                "entry_reason":     str(p.get("entry_reason",    "") or ""),
                "source":           "Paper" if str(p.get("entry_order_id") or "").startswith("PAPER") else "Bot (local)",
                "paper":            str(p.get("entry_order_id") or "").startswith("PAPER"),
            })
        return result
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Summary statistics
# ─────────────────────────────────────────────────────────────────────────────

def compute_summary(trades: List[Dict]) -> Dict:
    if not trades:
        return {
            "total": 0, "winners": 0, "losers": 0,
            "win_rate": 0.0, "total_pnl": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0,
            "profit_factor": None,
            "gross_win": 0.0, "gross_loss": 0.0,
        }
    winners    = [t for t in trades if t.get("winner")]
    losers     = [t for t in trades if not t.get("winner")]
    gross_win  = sum(t["pnl"] for t in winners)
    gross_loss = abs(sum(t["pnl"] for t in losers))
    paper_rows = [t for t in trades if _is_paper_trade(t)]
    real_rows  = [t for t in trades if not _is_paper_trade(t)]
    return {
        "total":         len(trades),
        "winners":       len(winners),
        "losers":        len(losers),
        "win_rate":      round(len(winners) / len(trades) * 100, 1),
        "total_pnl":     round(sum(t["pnl"] for t in trades), 2),
        "avg_win":       round(gross_win  / len(winners), 2) if winners else 0.0,
        "avg_loss":      round(gross_loss / len(losers),  2) if losers  else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "gross_win":     round(gross_win,  2),
        "gross_loss":    round(gross_loss, 2),
        "paper_count":   len(paper_rows),
        "paper_pnl":     round(sum(float(t.get("pnl") or 0) for t in paper_rows), 2),
        "manual_count":  len(real_rows),
        "manual_pnl":    round(sum(float(t.get("pnl") or 0) for t in real_rows), 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cache helpers
# ─────────────────────────────────────────────────────────────────────────────

def save_cache(trades: List[Dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(JOURNAL_CACHE, "w", encoding="utf-8") as f:
        json.dump({"trades": trades,
                   "updated": datetime.datetime.utcnow().isoformat() + "Z"},
                  f, indent=2)


def load_cache() -> Optional[Dict]:
    try:
        with open(JOURNAL_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 24-month trade calendar (US Eastern, 12-hour timestamps)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_ts(ts) -> Optional[datetime.datetime]:
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime.datetime):
        dt = ts
    else:
        s = str(ts).strip()
        if not s:
            return None
        try:
            if s.replace(".", "", 1).isdigit():
                dt = datetime.datetime.fromtimestamp(float(s), tz=datetime.timezone.utc)
            else:
                iso = s.replace("Z", "+00:00")
                if len(iso) >= 5 and (iso[-5] in "+-") and iso[-3] != ":":
                    iso = iso[:-2] + ":" + iso[-2:]
                dt = datetime.datetime.fromisoformat(iso)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _fmt_12h(ts, with_date: bool = True) -> str:
    try:
        try:
            from app.utils import format_12h
        except Exception:
            from utils import format_12h
        return format_12h(ts, with_date=with_date)
    except Exception:
        dt = _parse_ts(ts)
        if not dt:
            return "—"
        et = datetime.timezone(datetime.timedelta(hours=-4))
        local = dt.astimezone(et)
        clock = local.strftime("%I:%M %p").lstrip("0")
        return local.strftime("%b %d, %Y ") + clock if with_date else clock


def load_journal_trades() -> List[Dict]:
    """Cached Schwab/local trades plus paper book, enriched."""
    cached = load_cache()
    if cached and cached.get("trades"):
        trades = merge_paper_into(cached["trades"])
        return enrich_trades(trades)
    return enrich_trades(merge_paper_into(load_all_local_trades()))


def build_calendar(trades: Optional[List[Dict]] = None, months: int = 24) -> Dict:
    """Standard monthly calendar covering `months` (default 24) of P/L.

    Days are grouped in America/New_York. Empty days are omitted from `days`
    (the UI still draws the full month grid). Timestamps are 12-hour Eastern.
    """
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = datetime.datetime.now(et)
    months = max(1, min(int(months or 24), 24))

    # First day of the month that is (months-1) months before the current month.
    start_ord = now.year * 12 + (now.month - 1) - (months - 1)
    start_y, start_mi = divmod(start_ord, 12)
    start = datetime.date(start_y, start_mi + 1, 1)

    if trades is None:
        trades = load_journal_trades()

    days: Dict[str, Dict] = {}
    for t in trades or []:
        dt = _parse_ts(t.get("exit_time") or t.get("entry_time"))
        if not dt:
            continue
        local = dt.astimezone(et)
        d = local.date()
        if d < start:
            continue
        key = d.isoformat()
        rec = days.get(key)
        if rec is None:
            rec = {
                "date": key,
                "pnl": 0.0,
                "real_pnl": 0.0,
                "paper_pnl": 0.0,
                "count": 0,
                "paper_count": 0,
                "wins": 0,
                "losses": 0,
                "trades": [],
            }
            days[key] = rec
        pnl = float(t.get("pnl") or 0)
        is_paper = _is_paper_trade(t)
        rec["pnl"] = round(rec["pnl"] + pnl, 2)
        if is_paper:
            rec["paper_pnl"] = round(rec["paper_pnl"] + pnl, 2)
            rec["paper_count"] += 1
        else:
            rec["real_pnl"] = round(rec["real_pnl"] + pnl, 2)
        rec["count"] += 1
        if t.get("winner") or pnl > 0:
            rec["wins"] += 1
        else:
            rec["losses"] += 1
        rec["trades"].append({
            "symbol": t.get("symbol"),
            "qty": t.get("qty"),
            "direction": t.get("direction") or "LONG",
            "entry_price": t.get("entry_price"),
            "exit_price": t.get("exit_price"),
            "pnl": round(pnl, 2),
            "pnl_pct": t.get("pnl_pct"),
            "winner": bool(t.get("winner")),
            "source": "Paper" if is_paper else (t.get("source") or ""),
            "paper": is_paper,
            "strategy": t.get("strategy") or "",
            "entry_time": _fmt_12h(t.get("entry_time")),
            "exit_time": _fmt_12h(t.get("exit_time")),
        })

    month_list = []
    y, m = start.year, start.month
    for _ in range(months):
        if m == 12:
            last = datetime.date(y + 1, 1, 1) - datetime.timedelta(days=1)
        else:
            last = datetime.date(y, m + 1, 1) - datetime.timedelta(days=1)
        m_pnl = 0.0
        m_paper = 0.0
        m_real = 0.0
        m_count = 0
        m_paper_count = 0
        m_wins = 0
        prefix = f"{y:04d}-{m:02d}"
        for key, rec in days.items():
            if key.startswith(prefix):
                m_pnl += rec["pnl"]
                m_paper += rec.get("paper_pnl") or 0
                m_real += rec.get("real_pnl") or 0
                m_count += rec["count"]
                m_paper_count += rec.get("paper_count") or 0
                m_wins += rec["wins"]
        month_list.append({
            "year": y,
            "month": m,
            "key": prefix,
            "label": datetime.date(y, m, 1).strftime("%b %Y"),
            "pnl": round(m_pnl, 2),
            "paper_pnl": round(m_paper, 2),
            "real_pnl": round(m_real, 2),
            "count": m_count,
            "paper_count": m_paper_count,
            "wins": m_wins,
            "days_in_month": last.day,
        })
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1

    return {
        "timezone": "America/New_York",
        "clock": "12-hour",
        "start": start.isoformat(),
        "end": now.date().isoformat(),
        "today": now.date().isoformat(),
        "months": month_list,
        "days": days,
        "summary": compute_summary(trades or []),
    }
