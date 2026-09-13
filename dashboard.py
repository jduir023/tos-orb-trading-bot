"""
dashboard.py
PyQt5 trading dashboard for the TOS/Schwab ORB bot.
Modeled after pyqt_dashboard.py from the crypto bot.
"""

import sys
import threading
import time
from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget, QSizePolicy, QHeaderView,
)

from trading_engine import TradingEngine

# ------------------------------------------------------------------
# Dark glossy stylesheet — matches crypto bot visual style
# ------------------------------------------------------------------
STYLESHEET = """
QMainWindow, QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 12px;
}
QFrame#panel {
    background-color: #16213e;
    border: 1px solid #0f3460;
    border-radius: 6px;
    padding: 6px;
}
QLabel#panelTitle {
    color: #e94560;
    font-size: 13px;
    font-weight: bold;
    padding-bottom: 4px;
}
QPushButton {
    background-color: #0f3460;
    color: #e0e0e0;
    border: 1px solid #e94560;
    border-radius: 4px;
    padding: 6px 14px;
    font-weight: bold;
}
QPushButton:hover  { background-color: #e94560; color: #fff; }
QPushButton:pressed { background-color: #c73652; }
QPushButton#green  { border-color: #00b894; }
QPushButton#green:hover { background-color: #00b894; }
QPushButton#red    { border-color: #e74c3c; }
QPushButton#red:hover   { background-color: #e74c3c; }
QLineEdit, QComboBox {
    background-color: #0f3460;
    border: 1px solid #4a4a6a;
    border-radius: 3px;
    padding: 4px;
    color: #e0e0e0;
}
QTableWidget {
    background-color: #16213e;
    gridline-color: #0f3460;
    color: #e0e0e0;
    selection-background-color: #e94560;
}
QTableWidget QHeaderView::section {
    background-color: #0f3460;
    color: #e94560;
    font-weight: bold;
    padding: 4px;
    border: none;
}
QTextEdit {
    background-color: #0d0d1a;
    color: #a8ff78;
    font-family: Consolas, monospace;
    font-size: 11px;
    border: 1px solid #0f3460;
    border-radius: 3px;
}
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 14px; height: 14px; }
QLabel#stat { color: #a0d8ef; font-size: 13px; }
QLabel#statValue { color: #ffffff; font-size: 14px; font-weight: bold; }
QLabel#pnlPos { color: #00b894; font-size: 16px; font-weight: bold; }
QLabel#pnlNeg { color: #e74c3c; font-size: 16px; font-weight: bold; }
"""


class GlossPanel(QFrame):
    def __init__(self, title: str = "") -> None:
        super().__init__()
        self.setObjectName("panel")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(6)
        if title:
            lbl = QLabel(title)
            lbl.setObjectName("panelTitle")
            lbl.setAlignment(Qt.AlignCenter)
            self._layout.addWidget(lbl)

    def addWidget(self, w):
        self._layout.addWidget(w)

    def addLayout(self, lay):
        self._layout.addLayout(lay)

    def layout(self):
        return self._layout


# ------------------------------------------------------------------
# Signal bridge — safely emit from worker threads to Qt main thread
# ------------------------------------------------------------------
class _Bridge(QObject):
    log_received      = pyqtSignal(str)
    balance_updated   = pyqtSignal(dict)
    positions_updated = pyqtSignal(dict)
    scan_results      = pyqtSignal(list)
    trade_placed      = pyqtSignal(dict)
    day_pnl_updated   = pyqtSignal(float)
    status_changed    = pyqtSignal(str)
    signal_received   = pyqtSignal(object)


# ------------------------------------------------------------------
# Main Dashboard
# ------------------------------------------------------------------
class TradingDashboard(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.engine = TradingEngine()
        self._bridge = _Bridge()

        self.setWindowTitle("TOS ORB Bot — Schwab")
        self.setMinimumSize(1280, 800)
        self.setStyleSheet(STYLESHEET)

        self._build_ui()
        self._wire_engine_events()
        self._start_ui_refresh_timer()

        # Auto-restore auth if tokens saved
        if self.engine.is_authenticated():
            self._log("Schwab tokens loaded — authenticated.")
            self._set_status("Ready")
        else:
            self._log("Not authenticated. Enter API keys and click Authorize.")

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(8, 8, 8, 8)

        # Left column
        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(self._build_account_panel())
        left.addWidget(self._build_config_panel())
        left.addWidget(self._build_control_panel())
        left.addStretch()

        # Center column
        center = QVBoxLayout()
        center.setSpacing(8)
        center.addWidget(self._build_scan_panel(), stretch=2)
        center.addWidget(self._build_positions_panel(), stretch=3)

        # Right column
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self._build_stats_panel())
        right.addWidget(self._build_log_panel(), stretch=1)

        root.addLayout(left, stretch=2)
        root.addLayout(center, stretch=4)
        root.addLayout(right, stretch=3)

    def _build_account_panel(self) -> GlossPanel:
        panel = GlossPanel("Account")

        # API keys
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("App Key:"))
        self.input_key = QLineEdit()
        self.input_key.setPlaceholderText("Schwab App Key")
        self.input_key.setEchoMode(QLineEdit.Password)
        key_row.addWidget(self.input_key)
        panel.addLayout(key_row)

        sec_row = QHBoxLayout()
        sec_row.addWidget(QLabel("App Secret:"))
        self.input_secret = QLineEdit()
        self.input_secret.setPlaceholderText("Schwab App Secret")
        self.input_secret.setEchoMode(QLineEdit.Password)
        sec_row.addWidget(self.input_secret)
        panel.addLayout(sec_row)

        # Pre-fill from saved config
        cfg = self.engine.data_handler.load_config()
        self.input_key.setText(cfg.get("app_key", ""))
        self.input_secret.setText(cfg.get("app_secret", ""))

        btn_auth = QPushButton("Authorize Schwab")
        btn_auth.clicked.connect(self._on_authorize)
        panel.addWidget(btn_auth)

        self.lbl_auth_status = QLabel("Status: Not authenticated")
        self.lbl_auth_status.setAlignment(Qt.AlignCenter)
        panel.addWidget(self.lbl_auth_status)

        # Balance row
        bal_row = QHBoxLayout()
        lbl_cash = QLabel("Cash Available:")
        lbl_cash.setObjectName("stat")
        self.lbl_cash = QLabel("—")
        self.lbl_cash.setObjectName("statValue")
        bal_row.addWidget(lbl_cash)
        bal_row.addWidget(self.lbl_cash)
        panel.addLayout(bal_row)

        return panel

    def _build_config_panel(self) -> GlossPanel:
        panel = GlossPanel("Trade Config")
        cfg = self.engine.data_handler.load_config()

        def row(label, widget):
            h = QHBoxLayout()
            h.addWidget(QLabel(label))
            h.addWidget(widget)
            panel.addLayout(h)

        self.inp_account  = QLineEdit(str(cfg.get("account_size", 7111)))
        self.inp_risk_pct = QLineEdit(str(cfg.get("risk_pct", 1.5)))
        self.inp_orb_min  = QLineEdit(str(cfg.get("orb_minutes", 15)))
        self.inp_max_trades = QLineEdit(str(cfg.get("max_trades_per_day", 3)))
        self.inp_rr       = QLineEdit(str(cfg.get("rr_target", 2.0)))
        self.inp_cutoff   = QLineEdit(str(cfg.get("entry_cutoff_hour", 12)))

        row("Account $:", self.inp_account)
        row("Risk %:", self.inp_risk_pct)
        row("ORB Minutes:", self.inp_orb_min)
        row("Max Trades/Day:", self.inp_max_trades)
        row("R:R Target:", self.inp_rr)
        row("Entry Cutoff (Hour):", self.inp_cutoff)

        chk_row = QHBoxLayout()
        self.chk_auto  = QCheckBox("Auto Trade")
        self.chk_dry   = QCheckBox("Dry Run")
        self.chk_vwap  = QCheckBox("Require VWAP")
        self.chk_auto.setChecked(cfg.get("auto_trade", True))
        self.chk_dry.setChecked(cfg.get("dry_run", True))
        self.chk_vwap.setChecked(cfg.get("require_vwap_above", True))
        chk_row.addWidget(self.chk_auto)
        chk_row.addWidget(self.chk_dry)
        chk_row.addWidget(self.chk_vwap)
        panel.addLayout(chk_row)

        btn_save = QPushButton("Save Config")
        btn_save.clicked.connect(self._on_save_config)
        panel.addWidget(btn_save)

        return panel

    def _build_control_panel(self) -> GlossPanel:
        panel = GlossPanel("Controls")

        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("Start Bot")
        self.btn_start.setObjectName("green")
        self.btn_stop  = QPushButton("Stop Bot")
        self.btn_stop.setObjectName("red")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        panel.addLayout(btn_row)

        # Manual trade row
        manual_row = QHBoxLayout()
        self.inp_manual_symbol = QLineEdit()
        self.inp_manual_symbol.setPlaceholderText("Symbol e.g. AMC")
        btn_buy  = QPushButton("Manual BUY")
        btn_sell = QPushButton("Manual SELL")
        btn_buy.setObjectName("green")
        btn_sell.setObjectName("red")
        btn_buy.clicked.connect(self._on_manual_buy)
        btn_sell.clicked.connect(self._on_manual_sell)
        manual_row.addWidget(self.inp_manual_symbol)
        manual_row.addWidget(btn_buy)
        manual_row.addWidget(btn_sell)
        panel.addLayout(manual_row)

        self.lbl_status = QLabel("Status: Idle")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        panel.addWidget(self.lbl_status)

        return panel

    def _build_scan_panel(self) -> GlossPanel:
        panel = GlossPanel("Pre-Market Scanner Results")

        self.scan_table = QTableWidget(0, 7)
        self.scan_table.setHorizontalHeaderLabels(
            ["Symbol", "Last", "Gap%", "RelVol", "Volume", "Float", "Direction"]
        )
        self.scan_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.scan_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.scan_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.scan_table.verticalHeader().setVisible(False)
        panel.addWidget(self.scan_table)

        # Add to watchlist
        wl_row = QHBoxLayout()
        self.inp_add_symbol = QLineEdit()
        self.inp_add_symbol.setPlaceholderText("Add symbol to watchlist")
        btn_add = QPushButton("Add")
        btn_add.clicked.connect(self._on_add_to_watchlist)
        wl_row.addWidget(self.inp_add_symbol)
        wl_row.addWidget(btn_add)
        panel.addLayout(wl_row)

        return panel

    def _build_positions_panel(self) -> GlossPanel:
        panel = GlossPanel("Open Positions")

        self.pos_table = QTableWidget(0, 8)
        self.pos_table.setHorizontalHeaderLabels(
            ["Symbol", "Dir", "Shares", "Entry", "Stop", "Target", "Current", "Unreal PnL"]
        )
        self.pos_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.pos_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.pos_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.pos_table.verticalHeader().setVisible(False)
        panel.addWidget(self.pos_table)

        closed_lbl = QLabel("Closed Trades Today")
        closed_lbl.setObjectName("panelTitle")
        closed_lbl.setAlignment(Qt.AlignCenter)
        panel.addWidget(closed_lbl)

        self.closed_table = QTableWidget(0, 6)
        self.closed_table.setHorizontalHeaderLabels(
            ["Symbol", "Dir", "Shares", "Entry", "Exit", "PnL"]
        )
        self.closed_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.closed_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.closed_table.verticalHeader().setVisible(False)
        panel.addWidget(self.closed_table)

        return panel

    def _build_stats_panel(self) -> GlossPanel:
        panel = GlossPanel("Today's Stats")

        def stat_row(label):
            h = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setObjectName("stat")
            val = QLabel("—")
            val.setObjectName("statValue")
            h.addWidget(lbl)
            h.addStretch()
            h.addWidget(val)
            panel.addLayout(h)
            return val

        self.lbl_day_pnl    = stat_row("Day PnL:")
        self.lbl_trades_today = stat_row("Trades:")
        self.lbl_win_rate   = stat_row("Win Rate:")
        self.lbl_total_val  = stat_row("Account Value:")

        # Day PnL large display
        self.lbl_pnl_big = QLabel("$0.00")
        self.lbl_pnl_big.setObjectName("pnlPos")
        self.lbl_pnl_big.setAlignment(Qt.AlignCenter)
        panel.addWidget(self.lbl_pnl_big)

        weekly_lbl = QLabel("Weekly Target: $248.89 (3.5%)")
        weekly_lbl.setAlignment(Qt.AlignCenter)
        weekly_lbl.setStyleSheet("color: #f39c12; font-size: 11px;")
        panel.addWidget(weekly_lbl)

        return panel

    def _build_log_panel(self) -> GlossPanel:
        panel = GlossPanel("Log")
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        panel.addWidget(self.log_box)
        btn_clear = QPushButton("Clear Log")
        btn_clear.clicked.connect(self.log_box.clear)
        panel.addWidget(btn_clear)
        return panel

    # ------------------------------------------------------------------
    # Engine event wiring (thread-safe via Qt signals)
    # ------------------------------------------------------------------

    def _wire_engine_events(self) -> None:
        self._bridge.log_received.connect(self._on_log)
        self._bridge.balance_updated.connect(self._on_balance)
        self._bridge.positions_updated.connect(self._on_positions)
        self._bridge.scan_results.connect(self._on_scan_results)
        self._bridge.trade_placed.connect(self._on_trade_placed)
        self._bridge.day_pnl_updated.connect(self._on_day_pnl)
        self._bridge.status_changed.connect(self._set_status)
        self._bridge.signal_received.connect(self._on_signal)

        self.engine.on("log",          lambda m: self._bridge.log_received.emit(str(m)))
        self.engine.on("balance",      lambda d: self._bridge.balance_updated.emit(d or {}))
        self.engine.on("positions",    lambda d: self._bridge.positions_updated.emit(d or {}))
        self.engine.on("scan_results", lambda r: self._bridge.scan_results.emit(r or []))
        self.engine.on("trade_placed", lambda d: self._bridge.trade_placed.emit(d or {}))
        self.engine.on("day_pnl",      lambda v: self._bridge.day_pnl_updated.emit(float(v or 0)))
        self.engine.on("status",       lambda s: self._bridge.status_changed.emit(str(s)))
        self.engine.on("signal",       lambda s: self._bridge.signal_received.emit(s))
        self.engine.on("auth_status",  lambda ok: self._on_auth_status(ok))

    # ------------------------------------------------------------------
    # UI refresh timer
    # ------------------------------------------------------------------

    def _start_ui_refresh_timer(self) -> None:
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start(5000)  # every 5 seconds

    def _refresh_ui(self) -> None:
        if not self.engine.trading_active:
            return
        bal = self.engine.get_balance()
        if bal:
            self._bridge.balance_updated.emit(bal)
        pnl = self.engine.get_day_pnl()
        self._bridge.day_pnl_updated.emit(pnl)
        self._bridge.positions_updated.emit({
            "open":   [vars(p) for p in self.engine.get_open_positions()],
            "closed": [vars(p) for p in self.engine.get_closed_positions()],
        })

    # ------------------------------------------------------------------
    # Slot handlers
    # ------------------------------------------------------------------

    def _on_log(self, msg: str) -> None:
        self.log_box.append(msg)

    def _log(self, msg: str) -> None:
        self.log_box.append(msg)

    def _set_status(self, status: str) -> None:
        self.lbl_status.setText(f"Status: {status.capitalize()}")

    def _on_auth_status(self, ok: bool) -> None:
        if ok:
            self.lbl_auth_status.setText("Status: Authenticated")
            self.lbl_auth_status.setStyleSheet("color: #00b894;")
        else:
            self.lbl_auth_status.setText("Status: Not authenticated")
            self.lbl_auth_status.setStyleSheet("color: #e74c3c;")

    def _on_balance(self, data: Dict) -> None:
        cash = data.get("cash_available", 0)
        total = data.get("total_value", 0)
        self.lbl_cash.setText(f"${cash:,.2f}")
        self.lbl_total_val.setText(f"${total:,.2f}")

    def _on_day_pnl(self, pnl: float) -> None:
        txt = f"${pnl:+.2f}"
        self.lbl_day_pnl.setText(txt)
        self.lbl_pnl_big.setText(txt)
        obj = "pnlPos" if pnl >= 0 else "pnlNeg"
        self.lbl_pnl_big.setObjectName(obj)
        self.lbl_pnl_big.setStyleSheet("color: #00b894;" if pnl >= 0 else "color: #e74c3c;")

        # Win rate
        closed = self.engine.get_closed_positions()
        if closed:
            wins = sum(1 for p in closed if p.pnl > 0)
            wr = wins / len(closed) * 100
            self.lbl_win_rate.setText(f"{wr:.0f}%")
        self.lbl_trades_today.setText(str(self.engine.get_trades_today()))

    def _on_positions(self, data: Dict) -> None:
        open_pos   = data.get("open", [])
        closed_pos = data.get("closed", [])

        # Open positions table
        self.pos_table.setRowCount(len(open_pos))
        for row, pos in enumerate(open_pos):
            self._set_cell(self.pos_table, row, 0, pos.get("symbol", ""))
            self._set_cell(self.pos_table, row, 1, pos.get("direction", ""))
            self._set_cell(self.pos_table, row, 2, str(pos.get("shares", 0)))
            self._set_cell(self.pos_table, row, 3, f"{pos.get('entry_price', 0):.4f}")
            self._set_cell(self.pos_table, row, 4, f"{pos.get('stop_price', 0):.4f}")
            self._set_cell(self.pos_table, row, 5, f"{pos.get('target_r2', 0):.4f}")
            self._set_cell(self.pos_table, row, 6, "—")
            unreal = pos.get("pnl", 0)
            item = QTableWidgetItem(f"${unreal:+.2f}")
            item.setForeground(QColor("#00b894") if unreal >= 0 else QColor("#e74c3c"))
            self.pos_table.setItem(row, 7, item)

        # Closed trades table
        self.closed_table.setRowCount(len(closed_pos))
        for row, pos in enumerate(closed_pos):
            self._set_cell(self.closed_table, row, 0, pos.get("symbol", ""))
            self._set_cell(self.closed_table, row, 1, pos.get("direction", ""))
            self._set_cell(self.closed_table, row, 2, str(pos.get("shares", 0)))
            self._set_cell(self.closed_table, row, 3, f"{pos.get('entry_price', 0):.4f}")
            self._set_cell(self.closed_table, row, 4, f"{pos.get('exit_price', 0):.4f}")
            pnl = pos.get("pnl", 0)
            item = QTableWidgetItem(f"${pnl:+.2f}")
            item.setForeground(QColor("#00b894") if pnl >= 0 else QColor("#e74c3c"))
            self.closed_table.setItem(row, 5, item)

    def _on_scan_results(self, results: List[Dict]) -> None:
        self.scan_table.setRowCount(len(results))
        for row, r in enumerate(results):
            self._set_cell(self.scan_table, row, 0, r.get("symbol", ""))
            self._set_cell(self.scan_table, row, 1, f"{r.get('last', 0):.4f}")
            gap = r.get("gap_pct", 0)
            item = QTableWidgetItem(f"{gap:+.2f}%")
            item.setForeground(QColor("#00b894") if gap >= 0 else QColor("#e74c3c"))
            self.scan_table.setItem(row, 2, item)
            self._set_cell(self.scan_table, row, 3, f"{r.get('rel_vol', 0):.1f}x")
            self._set_cell(self.scan_table, row, 4, f"{r.get('volume', 0):,}")
            fl = r.get("float", 0)
            self._set_cell(self.scan_table, row, 5, f"{fl/1_000_000:.1f}M" if fl else "N/A")
            self._set_cell(self.scan_table, row, 6, r.get("direction", ""))

    def _on_trade_placed(self, data: Dict) -> None:
        sym = data.get("symbol", "")
        self._log(
            f"TRADE PLACED: {sym} | {data.get('shares')} shares | "
            f"Entry:{data.get('entry')} Stop:{data.get('stop')} Target:{data.get('target')}"
        )

    def _on_signal(self, signal) -> None:
        self._log(
            f"SIGNAL: {signal.direction} {signal.symbol} @ {signal.entry_price:.4f} | "
            f"stop={signal.stop_price:.4f} target={signal.target_r2:.4f} "
            f"shares={signal.shares} risk=${signal.risk_dollars:.2f}"
        )

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_authorize(self) -> None:
        key    = self.input_key.text().strip()
        secret = self.input_secret.text().strip()
        if not key or not secret:
            QMessageBox.warning(self, "Missing Keys", "Enter App Key and App Secret first.")
            return
        self.engine.set_api_keys(key, secret)
        self._log("Opening Schwab authorization in browser...")
        threading.Thread(target=self._do_authorize, daemon=True).start()

    def _do_authorize(self) -> None:
        result = self.engine.authorize()
        self._bridge.log_received.emit("Authorization complete." if result else "Authorization failed.")

    def _on_start(self) -> None:
        if not self.engine.is_authenticated():
            QMessageBox.warning(self, "Not Authenticated", "Authorize with Schwab first.")
            return
        self._on_save_config()
        self.engine.start()
        self._log("Bot started.")

    def _on_stop(self) -> None:
        self.engine.stop()
        self._log("Bot stopped.")

    def _on_save_config(self) -> None:
        try:
            self.engine.set_config(
                account_size=float(self.inp_account.text()),
                risk_pct=float(self.inp_risk_pct.text()),
                orb_minutes=int(self.inp_orb_min.text()),
                max_trades=int(self.inp_max_trades.text()),
                auto_trade=self.chk_auto.isChecked(),
                dry_run=self.chk_dry.isChecked(),
            )
            self.engine.strategy.rr_target = float(self.inp_rr.text())
            self.engine.strategy.require_vwap_above = self.chk_vwap.isChecked()
            self.engine.strategy.entry_cutoff_hour = int(self.inp_cutoff.text())
            self._log("Config saved.")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid Config", str(e))

    def _on_manual_buy(self) -> None:
        sym = self.inp_manual_symbol.text().strip().upper()
        if not sym:
            return
        threading.Thread(target=self.engine.manual_buy, args=(sym,), daemon=True).start()

    def _on_manual_sell(self) -> None:
        sym = self.inp_manual_symbol.text().strip().upper()
        if not sym:
            return
        threading.Thread(target=self.engine.manual_sell, args=(sym,), daemon=True).start()

    def _on_add_to_watchlist(self) -> None:
        sym = self.inp_add_symbol.text().strip().upper()
        if not sym:
            return
        wl = self.engine.get_watchlist()
        if sym not in wl:
            wl.append(sym)
            self.engine.set_watchlist(wl)
            self._log(f"Added {sym} to watchlist.")
        self.inp_add_symbol.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _set_cell(table: QTableWidget, row: int, col: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        table.setItem(row, col, item)
