"""Paper round-trips show up as journal/calendar rows tagged Paper, not Manual."""
from __future__ import annotations
import os
import tempfile

from paper_account import PaperAccount
from journal import (
    build_calendar, enrich_trades, load_paper_trades, merge_paper_into, _is_paper_trade,
)


def test_paper_roundtrip_is_journal_row():
    path = os.path.join(tempfile.gettempdir(), "paper_jnl.json")
    try:
        os.remove(path)
    except OSError:
        pass
    os.environ  # keep linter quiet
    p = PaperAccount(path)
    p.buy_equity("AAPL", 8, 10.0, "ORB LONG AAPL")
    p.sell_equity("AAPL", 8, 10.50, "ORB LONG AAPL")
    rows = p.journal_rows()
    assert len(rows) == 1
    t = rows[0]
    assert t["source"] == "Paper" and t["paper"] is True
    assert t["symbol"] == "AAPL"
    assert t["qty"] == 8
    assert abs(t["pnl"] - 4.0) < 1e-6
    assert t["winner"] is True
    assert str(t["exit_order_id"]).startswith("PAPER")


def test_merge_keeps_manual_separate():
    path = os.path.join(tempfile.gettempdir(), "paper_jnl2.json")
    try:
        os.remove(path)
    except OSError:
        pass
    p = PaperAccount(path)
    p.buy_equity("MSFT", 5, 20.0, "scalp")
    p.sell_equity("MSFT", 5, 19.5, "scalp")
    # Pretend this is the live paper book path
    import journal as j
    old = j.PAPER_BOOK
    j.PAPER_BOOK = path
    try:
        manual = [{
            "symbol": "MSFT", "qty": 5, "entry_price": 20.0, "exit_price": 21.0,
            "pnl": 5.0, "winner": True, "source": "Manual",
            "exit_order_id": "schwab-1", "exit_time": "2026-08-30T20:00:00Z",
        }]
        merged = merge_paper_into(manual)
        sources = {t["source"] for t in merged}
        assert "Paper" in sources and "Manual" in sources
        assert sum(1 for t in merged if t.get("paper")) == 1
        cal = build_calendar(enrich_trades(merged), months=24)
        # Day of paper trade should exist with paper_count
        paper_days = [d for d in cal["days"].values() if d.get("paper_count")]
        assert paper_days, cal["days"]
        assert any(tr.get("paper") for d in paper_days for tr in d["trades"])
    finally:
        j.PAPER_BOOK = old


def test_is_paper_from_order_id():
    assert _is_paper_trade({"exit_order_id": "PAPER-X-abc", "source": "Bot (local)"})
    assert not _is_paper_trade({"exit_order_id": "12345", "source": "Manual"})


if __name__ == "__main__":
    test_paper_roundtrip_is_journal_row()
    print("OK roundtrip")
    test_is_paper_from_order_id()
    print("OK tag")
    test_merge_keeps_manual_separate()
    print("OK merge")
    print("ALL OK")
