from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from okx_quant.config import Settings


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _ts_rank(value: Any) -> float:
    parsed = _parse_ts(value)
    return float("-inf") if parsed is None else parsed.timestamp()


def _numeric_map(payload: Any) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    return {str(key): _safe_float(value) for key, value in payload.items()}


def _is_stop_reason(reason: Any) -> bool:
    text = str(reason or "").lower()
    return any(token in text for token in ("stop", "drawdown", "liquidat", "risk trim"))


def _format_money(value: Any) -> str:
    amount = _safe_float(value)
    decimals = 0 if abs(amount) >= 1000 else 2
    return f"${amount:,.{decimals}f}"


def _format_signed_money(value: Any) -> str:
    amount = _safe_float(value)
    prefix = "+" if amount > 0 else "-" if amount < 0 else ""
    return f"{prefix}{_format_money(abs(amount))}"


def _format_pct(value: Any) -> str:
    return f"{_safe_float(value) * 100:.2f}%"


def _position_action(
    before_quote: float,
    after_quote: float,
    delta_quote: float,
    before_size: float,
    after_size: float,
    delta_size: float,
) -> str:
    eps = 1e-9
    if after_quote <= eps and after_size <= eps and (before_quote > eps or before_size > eps):
        return "clear"
    if delta_quote > eps or delta_size > eps:
        return "increase"
    if delta_quote < -eps or delta_size < -eps:
        return "decrease"
    return "flat"


class DashboardDataStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("okx_quant.dashboard")
        self.event_log_path = Path(settings.factor_guardian_output_dir) / "factor_guardian_events.jsonl"
        self.daily_log_path = Path(settings.factor_guardian_output_dir) / "factor_guardian_daily.jsonl"
        self.state_path = Path(settings.factor_state_path)
        self.backtest_output_dir = Path(settings.factor_backtest_output_dir)

    def _read_jsonl_tail(self, path: Path, limit: int, *, event_type: str | None = None) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: deque[dict[str, Any]] = deque(maxlen=limit)
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    # Skip partial writes while the guardian is appending.
                    continue
                if event_type is not None and payload.get("event_type") != event_type:
                    continue
                rows.append(payload)
        return list(rows)

    def _latest_backtest(self) -> dict[str, Any] | None:
        candidates = sorted(
            self.backtest_output_dir.rglob("factor_backtest_*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            return {
                "report_path": str(path),
                "years": payload.get("years"),
                "start": payload.get("start"),
                "end": payload.get("end"),
                "total_return": payload.get("total_return"),
                "cagr": payload.get("cagr"),
                "sharpe_ratio": payload.get("sharpe_ratio"),
                "max_drawdown": payload.get("max_drawdown"),
                "benchmark_return": payload.get("benchmark_return"),
                "total_trades": payload.get("total_trades"),
                "turnover_ratio": payload.get("turnover_ratio"),
                "picks_last": payload.get("picks_last", []),
            }
        return None

    def _current_tick(self) -> dict[str, Any] | None:
        ticks = self._read_jsonl_tail(self.event_log_path, 1, event_type="tick")
        return ticks[-1] if ticks else None

    def _holding_delta(
        self,
        previous_tick: dict[str, Any] | None,
        current_tick: dict[str, Any],
    ) -> dict[str, Any]:
        before_holdings_quote = _numeric_map(None if previous_tick is None else previous_tick.get("holdings_quote"))
        after_holdings_quote = _numeric_map(current_tick.get("holdings_quote"))
        before_holdings_size = _numeric_map(None if previous_tick is None else previous_tick.get("holdings"))
        after_holdings_size = _numeric_map(current_tick.get("holdings"))
        inst_ids = set(before_holdings_quote) | set(after_holdings_quote) | set(before_holdings_size) | set(after_holdings_size)

        changes = []
        for inst_id in inst_ids:
            before_quote = before_holdings_quote.get(inst_id, 0.0)
            after_quote = after_holdings_quote.get(inst_id, 0.0)
            before_size = before_holdings_size.get(inst_id, 0.0)
            after_size = after_holdings_size.get(inst_id, 0.0)
            delta_quote = after_quote - before_quote
            delta_size = after_size - before_size
            if abs(delta_quote) < 1e-9 and abs(delta_size) < 1e-9:
                continue
            direction = "buy" if delta_quote > 1e-9 else "sell" if delta_quote < -1e-9 else "flat"
            changes.append(
                {
                    "inst_id": inst_id,
                    "before_quote": before_quote,
                    "after_quote": after_quote,
                    "delta_quote": delta_quote,
                    "before_size": before_size,
                    "after_size": after_size,
                    "delta_size": delta_size,
                    "direction": direction,
                    "position_action": _position_action(
                        before_quote,
                        after_quote,
                        delta_quote,
                        before_size,
                        after_size,
                        delta_size,
                    ),
                }
            )

        changes.sort(key=lambda item: (-abs(float(item["delta_quote"])), -abs(float(item["delta_size"])), item["inst_id"]))  # type: ignore[arg-type]
        before_total = sum(before_holdings_quote.values())
        after_total = sum(after_holdings_quote.values())
        return {
            "before_holdings_quote": before_total,
            "after_holdings_quote": after_total,
            "net_holdings_quote_delta": after_total - before_total,
            "holding_changes": changes,
        }

    def _recent_rebalances(self, ticks: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for tick_idx in range(len(ticks) - 1, -1, -1):
            item = ticks[tick_idx]
            planned = item.get("planned_orders") or []
            executed = item.get("executed_orders") or []
            if not planned and not executed:
                continue
            holdings_delta = self._holding_delta(ticks[tick_idx - 1] if tick_idx > 0 else None, item)

            if planned:
                total_planned_quote = sum(_safe_float(order.get("est_quote_value")) for order in planned)
                order_rows = [
                    {
                        "inst_id": order.get("inst_id", ""),
                        "side": order.get("side", ""),
                        "reason": order.get("reason", ""),
                        "est_quote_value": _safe_float(order.get("est_quote_value")),
                        "status": "filled" if order_idx < len(executed) else "planned",
                        "event_kind": "stop-loss" if _is_stop_reason(order.get("reason")) else "order",
                    }
                    for order_idx, order in enumerate(planned)
                ]
                status = "filled" if executed and len(executed) >= len(planned) else "partial" if executed else "planned"
            else:
                total_planned_quote = 0.0
                order_rows = [
                    {
                        "inst_id": order.get("instId", ""),
                        "side": order.get("side", ""),
                        "reason": "execution captured without local plan",
                        "est_quote_value": 0.0,
                        "status": "filled",
                        "event_kind": "order",
                    }
                    for order in executed
                ]
                status = "filled"

            rows.append(
                {
                    "ts": item.get("ts", ""),
                    "status": status,
                    "planned_count": len(planned),
                    "executed_count": len(executed),
                    "total_planned_quote": total_planned_quote,
                    "picks": list(item.get("picks", [])),
                    "orders": order_rows,
                    "stop_count": sum(1 for order in order_rows if order["event_kind"] == "stop-loss"),
                    **holdings_delta,
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _event_priority(self, event: dict[str, Any]) -> int:
        return {
            "halted": 4,
            "stop-loss": 3,
            "order": 2,
            "resumed": 1,
        }.get(str(event.get("kind")), 0)

    def _transition_event(self, item: dict[str, Any]) -> dict[str, Any]:
        halted = item.get("event_type") == "halted"
        reason = item.get("halt_reason") or item.get("previous_halt_reason") or "guardian state changed"
        return {
            "ts": item.get("ts", ""),
            "category": "circuit-breaker",
            "kind": "halted" if halted else "resumed",
            "label": "Circuit Breaker" if halted else "Guardian Resume",
            "severity": "danger" if halted else "live",
            "title": "Circuit breaker triggered" if halted else "Trading resumed",
            "summary": reason,
            "status": "HALTED" if halted else "RESUMED",
            "metric_label": "Equity",
            "metric_value": _safe_float(item.get("equity_quote")),
            "facts": [
                {"label": "State", "value": "HALTED" if halted else "ACTIVE"},
                {"label": "Equity", "value": _format_money(item.get("equity_quote"))},
                {"label": "Drawdown", "value": _format_pct(item.get("drawdown"))},
            ],
            "orders": [],
            "transition": item,
        }

    def _rebalance_event(
        self,
        activity: dict[str, Any],
        *,
        category: str,
        orders: list[dict[str, Any]],
    ) -> dict[str, Any]:
        executed_count = sum(1 for order in orders if order.get("status") == "filled")
        planned_count = len(orders)
        total_planned_quote = sum(_safe_float(order.get("est_quote_value")) for order in orders)
        if executed_count >= planned_count and executed_count > 0:
            status = "filled"
        elif executed_count > 0:
            status = "partial"
        else:
            status = "planned"
        symbols = ", ".join(dict.fromkeys(order.get("inst_id", "") for order in orders if order.get("inst_id"))) or "n/a"
        reason = next((order.get("reason") for order in orders if order.get("reason")), symbols)
        is_stop = category == "stop-loss"
        return {
            "ts": activity.get("ts", ""),
            "category": category,
            "kind": category,
            "label": "Stop-Loss" if is_stop else "Order Flow",
            "severity": "danger" if is_stop else "accent",
            "title": "Stop-loss / risk trim fired" if is_stop else "Rebalance orders submitted",
            "summary": reason if is_stop else symbols,
            "status": status.upper(),
            "metric_label": "Planned",
            "metric_value": total_planned_quote,
            "facts": [
                {"label": "Symbols", "value": symbols},
                {"label": "Orders", "value": f"{executed_count} sent / {planned_count} planned"},
                {"label": "Holdings delta", "value": _format_signed_money(activity.get("net_holdings_quote_delta"))},
            ],
            "planned_count": planned_count,
            "executed_count": executed_count,
            "total_planned_quote": total_planned_quote,
            "orders": orders,
            "picks": list(activity.get("picks", [])),
            "before_holdings_quote": activity.get("before_holdings_quote", 0.0),
            "after_holdings_quote": activity.get("after_holdings_quote", 0.0),
            "net_holdings_quote_delta": activity.get("net_holdings_quote_delta", 0.0),
            "holding_changes": list(activity.get("holding_changes", [])),
            "rebalance": activity,
        }

    def _recent_events(
        self,
        events: list[dict[str, Any]],
        recent_rebalances: list[dict[str, Any]],
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in events:
            if item.get("event_type") in {"halted", "resumed"}:
                rows.append(self._transition_event(item))

        for activity in recent_rebalances:
            stop_orders = [order for order in activity.get("orders", []) if order.get("event_kind") == "stop-loss"]
            regular_orders = [order for order in activity.get("orders", []) if order.get("event_kind") != "stop-loss"]
            if stop_orders:
                rows.append(self._rebalance_event(activity, category="stop-loss", orders=stop_orders))
            if regular_orders:
                rows.append(self._rebalance_event(activity, category="order", orders=regular_orders))

        rows.sort(key=lambda item: (_ts_rank(item.get("ts")), self._event_priority(item)), reverse=True)
        return rows[:limit]

    def snapshot(self, *, limit: int = 240) -> dict[str, Any]:
        ticks = self._read_jsonl_tail(self.event_log_path, max(1, limit), event_type="tick")
        events = self._read_jsonl_tail(self.event_log_path, max(32, limit * 2))
        latest_tick = ticks[-1] if ticks else None
        latest_transition = next(
            (item for item in reversed(events) if item.get("event_type") in {"halted", "resumed"}),
            None,
        )
        latest_backtest = self._latest_backtest()
        recent_rebalances = self._recent_rebalances(ticks)
        recent_events = self._recent_events(events, recent_rebalances)
        latest_rebalance = recent_rebalances[0] if recent_rebalances else None
        latest_highlight = recent_events[0] if recent_events else None

        curve = [
            {
                "ts": item.get("ts", ""),
                "nav": _safe_float(item.get("nav")),
                "equity_quote": _safe_float(item.get("equity_quote")),
                "cash_quote": _safe_float(item.get("cash_quote")),
                "drawdown": _safe_float(item.get("drawdown")),
                "trading_halted": bool(item.get("trading_halted", False)),
            }
            for item in ticks
        ]

        return {
            "meta": {
                "profile": "simulated" if self.settings.simulated else "live",
                "event_log_path": str(self.event_log_path),
                "daily_log_path": str(self.daily_log_path),
                "state_path": str(self.state_path),
                "backtest_output_dir": str(self.backtest_output_dir),
            },
            "status": {
                "has_guardian_data": bool(latest_tick),
                "curve_points": len(curve),
                "updated_at": None if latest_tick is None else latest_tick.get("ts"),
                "message": (
                    "Run guardian in another terminal to stream fresh ticks into the dashboard."
                    if latest_tick is None
                    else "Streaming guardian snapshots from the latest local log."
                ),
            },
            "latest_tick": latest_tick,
            "latest_transition": latest_transition,
            "curve": curve,
            "latest_rebalance": latest_rebalance,
            "latest_highlight": latest_highlight,
            "recent_events": recent_events,
            "recent_rebalances": recent_rebalances,
            "latest_backtest": latest_backtest,
        }


def _dashboard_html(default_limit: int) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OKX Quant Dashboard</title>
  <style>
    :root {{
      --bg: #f4efe7;
      --panel: rgba(255, 251, 245, 0.92);
      --ink: #1c1917;
      --muted: #6b645c;
      --accent: #0f766e;
      --accent-soft: rgba(15, 118, 110, 0.12);
      --danger: #b42318;
      --line: rgba(28, 25, 23, 0.12);
      --shadow: 0 18px 44px rgba(28, 25, 23, 0.12);
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
      background:
        radial-gradient(circle at top right, rgba(15, 118, 110, 0.14), transparent 30%),
        radial-gradient(circle at bottom left, rgba(180, 35, 24, 0.12), transparent 25%),
        linear-gradient(135deg, #f5efe5 0%, #ece7de 100%);
    }}

    .shell {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px;
    }}

    .hero {{
      display: grid;
      grid-template-columns: 1.5fr 1fr;
      gap: 18px;
      margin-bottom: 18px;
    }}

    .panel {{
      background: var(--panel);
      backdrop-filter: blur(14px);
      border: 1px solid rgba(255, 255, 255, 0.6);
      border-radius: 24px;
      box-shadow: var(--shadow);
      padding: 22px;
    }}

    .eyebrow {{
      letter-spacing: 0.12em;
      text-transform: uppercase;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 8px;
    }}

    h1 {{
      margin: 0;
      font-size: clamp(30px, 5vw, 54px);
      line-height: 0.95;
    }}

    .subtle {{
      margin-top: 12px;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.6;
    }}

    .status-pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
    }}

    .status-pill.alert {{
      background: rgba(180, 35, 24, 0.10);
      color: var(--danger);
    }}

    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }}

    .metric {{
      min-height: 148px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}

    .metric-label {{
      color: var(--muted);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.10em;
    }}

    .metric-value {{
      font-size: clamp(26px, 3vw, 38px);
      font-weight: 700;
      line-height: 1.05;
    }}

    .metric-note {{
      color: var(--muted);
      font-size: 14px;
    }}

    .layout {{
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 18px;
    }}

    .chart-wrap {{
      height: 360px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background:
        linear-gradient(180deg, rgba(15, 118, 110, 0.08), transparent 45%),
        repeating-linear-gradient(
          to bottom,
          rgba(28, 25, 23, 0.05),
          rgba(28, 25, 23, 0.05) 1px,
          transparent 1px,
          transparent 72px
        );
      overflow: hidden;
    }}

    svg {{
      width: 100%;
      height: 100%;
      display: block;
    }}

    .axis-labels {{
      display: flex;
      justify-content: space-between;
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .stack {{
      display: grid;
      gap: 18px;
    }}

    .list {{
      display: grid;
      gap: 12px;
      margin-top: 14px;
    }}

    .row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 0;
      border-bottom: 1px solid var(--line);
      font-size: 15px;
    }}

    .row:last-child {{
      border-bottom: 0;
      padding-bottom: 0;
    }}

    .row .key {{
      color: var(--muted);
      flex: 0 0 auto;
    }}

    .row .value {{
      text-align: right;
      font-weight: 600;
      word-break: break-word;
    }}

    .empty {{
      padding: 20px;
      border-radius: 18px;
      border: 1px dashed var(--line);
      color: var(--muted);
      font-size: 15px;
      line-height: 1.6;
    }}

    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 4px 10px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      white-space: nowrap;
    }}

    .badge.warn {{
      background: rgba(180, 35, 24, 0.10);
      color: var(--danger);
    }}

    .badge.live {{
      background: rgba(28, 25, 23, 0.08);
      color: var(--ink);
    }}

    .highlight-panel {{
      padding: 18px;
      border-radius: 20px;
      background: linear-gradient(135deg, rgba(15, 118, 110, 0.10), rgba(255, 255, 255, 0.72));
      border: 1px solid rgba(15, 118, 110, 0.16);
    }}

    .highlight-panel.danger {{
      background: linear-gradient(135deg, rgba(180, 35, 24, 0.12), rgba(255, 255, 255, 0.7));
      border-color: rgba(180, 35, 24, 0.20);
    }}

    .highlight-panel.live {{
      background: linear-gradient(135deg, rgba(28, 25, 23, 0.07), rgba(255, 255, 255, 0.72));
      border-color: rgba(28, 25, 23, 0.14);
    }}

    .highlight-panel.flash {{
      animation: highlight-flash 1.35s ease;
    }}

    .highlight-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }}

    .highlight-title {{
      font-size: 24px;
      line-height: 1.05;
      font-weight: 700;
      margin-bottom: 8px;
    }}

    .highlight-summary {{
      color: var(--muted);
      line-height: 1.6;
      margin-bottom: 14px;
    }}

    .fact-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}

    .fact-card {{
      padding: 12px;
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.58);
      border: 1px solid rgba(28, 25, 23, 0.08);
    }}

    .fact-label {{
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.10em;
      text-transform: uppercase;
      margin-bottom: 6px;
    }}

    .fact-value {{
      font-size: 15px;
      font-weight: 700;
      word-break: break-word;
    }}

    .filter-bar {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
      margin-bottom: 12px;
    }}

    .filter-pill {{
      appearance: none;
      border: 1px solid rgba(28, 25, 23, 0.10);
      background: rgba(28, 25, 23, 0.04);
      color: var(--ink);
      padding: 9px 14px;
      border-radius: 999px;
      font: inherit;
      font-size: 13px;
      cursor: pointer;
      transition: transform 120ms ease, background 120ms ease, border-color 120ms ease;
    }}

    .filter-pill:hover {{
      transform: translateY(-1px);
      border-color: rgba(15, 118, 110, 0.25);
    }}

    .filter-pill.active {{
      background: var(--accent-soft);
      color: var(--accent);
      border-color: rgba(15, 118, 110, 0.24);
      box-shadow: inset 0 0 0 1px rgba(15, 118, 110, 0.08);
    }}

    .event-groups {{
      display: grid;
      gap: 12px;
      margin-top: 14px;
    }}

    .event-group {{
      border: 1px solid var(--line);
      border-radius: 20px;
      background: rgba(255, 255, 255, 0.44);
      overflow: hidden;
    }}

    .event-group-summary {{
      list-style: none;
      cursor: pointer;
      padding: 16px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      font-weight: 700;
    }}

    .event-group-summary::-webkit-details-marker {{
      display: none;
    }}

    .event-group-summary-main {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}

    .event-group-summary-meta {{
      color: var(--muted);
      font-size: 13px;
      font-weight: 500;
      text-align: right;
    }}

    .event-group-caret {{
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .event-group[open] .event-group-caret::before {{
      content: "Collapse";
    }}

    .event-group:not([open]) .event-group-caret::before {{
      content: "Expand";
    }}

    .event-group-body {{
      padding: 0 18px 18px;
    }}

    .tape-table {{
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }}

    .tape-head,
    .tape-row {{
      display: grid;
      grid-template-columns: 168px 96px 120px 110px minmax(240px, 1fr);
      gap: 12px;
      align-items: start;
    }}

    .tape-head {{
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.10em;
      text-transform: uppercase;
    }}

    .tape-row {{
      padding: 14px 0;
      border-top: 1px solid var(--line);
      font-size: 14px;
    }}

    .order-stack {{
      display: grid;
      gap: 8px;
    }}

    .section-label {{
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.10em;
      text-transform: uppercase;
    }}

    .rebalance-change {{
      display: grid;
      gap: 10px;
      margin: 14px 0;
      padding: 14px;
      border-radius: 18px;
      background: rgba(15, 118, 110, 0.06);
      border: 1px solid rgba(15, 118, 110, 0.12);
    }}

    .rebalance-change-summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }}

    .change-grid {{
      display: grid;
      gap: 8px;
    }}

    .change-chip {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid rgba(28, 25, 23, 0.08);
    }}

    .change-chip.positive {{
      border-color: rgba(15, 118, 110, 0.20);
    }}

    .change-chip.negative {{
      border-color: rgba(180, 35, 24, 0.18);
    }}

    .change-name {{
      font-weight: 700;
      margin-bottom: 4px;
    }}

    .change-tags {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 6px;
    }}

    .action-pill {{
      display: inline-flex;
      align-items: center;
      padding: 3px 8px;
      border-radius: 999px;
      background: rgba(28, 25, 23, 0.08);
      color: var(--ink);
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      white-space: nowrap;
    }}

    .action-pill.increase {{
      background: rgba(15, 118, 110, 0.12);
      color: var(--accent);
    }}

    .action-pill.decrease,
    .action-pill.clear {{
      background: rgba(180, 35, 24, 0.10);
      color: var(--danger);
    }}

    .change-values {{
      text-align: right;
      min-width: 112px;
    }}

    .change-delta {{
      font-size: 15px;
      font-weight: 700;
      margin-bottom: 4px;
    }}

    .change-delta.positive {{
      color: var(--accent);
    }}

    .change-delta.negative {{
      color: var(--danger);
    }}

    .change-delta.flat {{
      color: var(--muted);
    }}

    .order-chip {{
      padding: 10px 12px;
      border-radius: 16px;
      background: rgba(28, 25, 23, 0.05);
      border: 1px solid rgba(28, 25, 23, 0.08);
    }}

    .order-title {{
      display: flex;
      align-items: center;
      gap: 8px;
      justify-content: space-between;
      font-weight: 700;
      margin-bottom: 6px;
    }}

    .order-reason {{
      color: var(--muted);
      line-height: 1.5;
    }}

    .cell-muted {{
      color: var(--muted);
    }}

    .event-summary {{
      color: var(--muted);
      line-height: 1.5;
      margin-bottom: 10px;
    }}

    .event-facts {{
      display: grid;
      gap: 8px;
    }}

    .event-fact {{
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.62);
      border: 1px solid rgba(28, 25, 23, 0.08);
    }}

    @keyframes highlight-flash {{
      0% {{
        transform: translateY(0);
        box-shadow: 0 0 0 rgba(15, 118, 110, 0);
      }}
      20% {{
        transform: translateY(-2px);
        box-shadow: 0 0 0 6px rgba(15, 118, 110, 0.14);
      }}
      100% {{
        transform: translateY(0);
        box-shadow: 0 0 0 rgba(15, 118, 110, 0);
      }}
    }}

    code {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      font-size: 12px;
      word-break: break-all;
    }}

    @media (max-width: 980px) {{
      .hero,
      .layout {{
        grid-template-columns: 1fr;
      }}

      .grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}

    @media (max-width: 640px) {{
      .shell {{
        padding: 14px;
      }}

      .panel {{
        padding: 18px;
        border-radius: 18px;
      }}

      .grid {{
        grid-template-columns: 1fr;
      }}

      .tape-head,
      .tape-row {{
        grid-template-columns: 1fr;
      }}

      .fact-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="panel">
        <div class="eyebrow">OKX Quant Control Room</div>
        <h1>Local realtime curve, risk state, and bot progress.</h1>
        <div class="subtle" id="message">
          Loading guardian data from local logs. Keep the guardian running in another terminal for live updates.
        </div>
      </div>
      <div class="panel">
        <div class="eyebrow">Server</div>
        <div class="status-pill" id="stream-status">Waiting for local data</div>
        <div class="subtle" style="margin-top:14px;">
          This page polls your local guardian logs every 5 seconds and never opens a public socket by default.
        </div>
        <div class="list" style="margin-top:18px;">
          <div class="row"><span class="key">Curve window</span><span class="value" id="curve-count">0 points</span></div>
          <div class="row"><span class="key">Last update</span><span class="value" id="updated-at">n/a</span></div>
          <div class="row"><span class="key">Event log</span><span class="value"><code id="event-log">n/a</code></span></div>
        </div>
      </div>
    </section>

    <section class="grid">
      <div class="panel metric">
        <div class="metric-label">Total Equity</div>
        <div class="metric-value" id="equity-value">--</div>
        <div class="metric-note" id="equity-note">Waiting for guardian ticks</div>
      </div>
      <div class="panel metric">
        <div class="metric-label">NAV</div>
        <div class="metric-value" id="nav-value">--</div>
        <div class="metric-note" id="nav-note">Baseline vs guardian start</div>
      </div>
      <div class="panel metric">
        <div class="metric-label">Drawdown</div>
        <div class="metric-value" id="drawdown-value">--</div>
        <div class="metric-note" id="drawdown-note">Portfolio stress monitor</div>
      </div>
      <div class="panel metric">
        <div class="metric-label">Trading State</div>
        <div class="metric-value" id="halt-value">--</div>
        <div class="metric-note" id="halt-note">Guardian has not streamed any state yet</div>
      </div>
    </section>

    <section class="layout">
      <div class="stack">
        <div class="panel">
          <div class="eyebrow">Realtime Equity Curve</div>
          <div class="chart-wrap">
            <svg viewBox="0 0 900 360" preserveAspectRatio="none">
              <polyline id="equity-line" fill="none" stroke="#0f766e" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" points=""></polyline>
              <polyline id="drawdown-line" fill="none" stroke="#b42318" stroke-width="2" stroke-dasharray="8 8" stroke-linecap="round" stroke-linejoin="round" points=""></polyline>
            </svg>
          </div>
          <div class="axis-labels">
            <span id="chart-start">Start</span>
            <span>Equity / Drawdown</span>
            <span id="chart-end">Now</span>
          </div>
        </div>
        <div class="panel">
          <div class="eyebrow">Latest Market State</div>
          <div id="market-state" class="empty">No market-state snapshot has been captured yet.</div>
        </div>
      </div>

      <div class="stack">
        <div class="panel">
          <div class="eyebrow">Latest Trigger</div>
          <div id="highlight-panel" class="empty">No orders, stop-loss actions, or circuit-breaker transitions recorded yet.</div>
        </div>
        <div class="panel">
          <div class="eyebrow">Portfolio</div>
          <div id="portfolio-panel" class="empty">No holdings snapshot yet.</div>
        </div>
        <div class="panel">
          <div class="eyebrow">Latest Rebalance</div>
          <div id="orders-panel" class="empty">No rebalance orders have been captured yet.</div>
        </div>
        <div class="panel">
          <div class="eyebrow">Latest Backtest</div>
          <div id="backtest-panel" class="empty">No backtest report found under the configured reports directory.</div>
        </div>
      </div>
    </section>

    <section class="panel" style="margin-top:18px;">
      <div class="eyebrow">Event Tape</div>
      <div class="filter-bar" id="event-filters">
        <button class="filter-pill active" type="button" data-filter="all">All Events</button>
        <button class="filter-pill" type="button" data-filter="order">Orders</button>
        <button class="filter-pill" type="button" data-filter="stop-loss">Stop-Loss</button>
        <button class="filter-pill" type="button" data-filter="circuit-breaker">Circuit Breaker</button>
      </div>
      <div id="tape-panel" class="empty">No recent events matched the active filter.</div>
    </section>
  </div>

  <script>
    const DEFAULT_LIMIT = {default_limit};
    const EVENT_FILTER_STORAGE_KEY = "okx-quant.dashboard.event-filter";
    const EVENT_GROUP_STORAGE_KEY = "okx-quant.dashboard.event-groups";
    const EVENT_FILTERS = ["all", "order", "stop-loss", "circuit-breaker"];
    const EVENT_GROUP_ORDER = ["circuit-breaker", "stop-loss", "order"];
    const EVENT_GROUP_LABELS = {{
      order: "Orders",
      "stop-loss": "Stop-Loss",
      "circuit-breaker": "Circuit Breaker",
    }};
    let currentEventFilter = "all";
    let collapsedEventGroups = {{}};
    let lastHighlightKey = "";

    function money(value) {{
      if (!Number.isFinite(value)) return "--";
      return new Intl.NumberFormat("en-US", {{
        style: "currency",
        currency: "USD",
        maximumFractionDigits: value >= 1000 ? 0 : 2,
      }}).format(value);
    }}

    function pct(value) {{
      if (!Number.isFinite(value)) return "--";
      return (value * 100).toFixed(2) + "%";
    }}

    function toLocalTime(value) {{
      if (!value) return "n/a";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString();
    }}

    function numberText(value) {{
      if (!Number.isFinite(value)) return "--";
      return new Intl.NumberFormat("en-US", {{
        maximumFractionDigits: Math.abs(value) >= 10 ? 2 : 4,
      }}).format(value);
    }}

    function signedMoney(value) {{
      if (!Number.isFinite(value)) return "--";
      const prefix = value > 0 ? "+" : value < 0 ? "-" : "";
      return prefix + money(Math.abs(value));
    }}

    function signedNumber(value) {{
      if (!Number.isFinite(value)) return "--";
      const prefix = value > 0 ? "+" : value < 0 ? "-" : "";
      return prefix + numberText(Math.abs(value));
    }}

    function toneClass(value) {{
      return value > 0 ? "positive" : value < 0 ? "negative" : "flat";
    }}

    function linePoints(values, height, width, invert = false) {{
      if (!values.length) return "";
      const pad = 22;
      const lo = Math.min(...values);
      const hi = Math.max(...values);
      const range = Math.max(hi - lo, 1e-9);
      return values.map((value, index) => {{
        const x = pad + (index / Math.max(values.length - 1, 1)) * (width - pad * 2);
        const normalized = (value - lo) / range;
        const y = invert
          ? pad + normalized * (height - pad * 2)
          : height - pad - normalized * (height - pad * 2);
        return x.toFixed(1) + "," + y.toFixed(1);
      }}).join(" ");
    }}

    function renderList(rows) {{
      return '<div class="list">' + rows.map((row) => (
        '<div class="row"><span class="key">' + row[0] + '</span><span class="value">' + row[1] + '</span></div>'
      )).join("") + "</div>";
    }}

    function escapeHtml(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }}

    function storageGet(key, fallback) {{
      try {{
        const value = window.localStorage.getItem(key);
        return value === null ? fallback : value;
      }} catch (_error) {{
        return fallback;
      }}
    }}

    function storageSet(key, value) {{
      try {{
        window.localStorage.setItem(key, value);
      }} catch (_error) {{
        // Ignore storage failures so the dashboard still works in private mode.
      }}
    }}

    function normalizeFilter(value) {{
      return EVENT_FILTERS.includes(value) ? value : "all";
    }}

    function loadUiState() {{
      currentEventFilter = normalizeFilter(storageGet(EVENT_FILTER_STORAGE_KEY, "all"));
      let storedGroups = {{}};
      try {{
        const rawGroups = storageGet(EVENT_GROUP_STORAGE_KEY, "{{}}");
        storedGroups = JSON.parse(rawGroups);
      }} catch (_error) {{
        storedGroups = {{}};
      }}
      collapsedEventGroups = {{}};
      EVENT_GROUP_ORDER.forEach((category) => {{
        collapsedEventGroups[category] = Boolean(storedGroups[category]);
      }});
    }}

    function persistFilter() {{
      storageSet(EVENT_FILTER_STORAGE_KEY, currentEventFilter);
    }}

    function persistCollapsedGroups() {{
      storageSet(EVENT_GROUP_STORAGE_KEY, JSON.stringify(collapsedEventGroups));
    }}

    function highlightKey(event) {{
      if (!event) return "";
      return [
        event.category || "",
        event.kind || "",
        event.ts || "",
        event.title || "",
      ].join("|");
    }}

    function positionActionLabel(action) {{
      if (action === "increase") return "增仓";
      if (action === "decrease") return "减仓";
      if (action === "clear") return "清仓";
      return "持平";
    }}

    function positionActionClass(action) {{
      if (action === "increase" || action === "decrease" || action === "clear") {{
        return action;
      }}
      return "flat";
    }}

    function filteredEvents(events) {{
      if (currentEventFilter === "all") {{
        return events;
      }}
      return events.filter((event) => event.category === currentEventFilter);
    }}

    function groupedEvents(events) {{
      const buckets = new Map(EVENT_GROUP_ORDER.map((category) => [category, []]));
      events.forEach((event) => {{
        const category = event.category || "order";
        if (!buckets.has(category)) {{
          buckets.set(category, []);
        }}
        buckets.get(category).push(event);
      }});
      return Array.from(buckets.entries()).filter(([, rows]) => rows.length > 0);
    }}

    function refreshFilterButtons() {{
      document.querySelectorAll("#event-filters [data-filter]").forEach((button) => {{
        button.classList.toggle("active", button.dataset.filter === currentEventFilter);
      }});
    }}

    function bindEventGroupToggles() {{
      document.querySelectorAll("#tape-panel details[data-category]").forEach((node) => {{
        node.addEventListener("toggle", () => {{
          const category = node.dataset.category || "order";
          collapsedEventGroups[category] = !node.open;
          persistCollapsedGroups();
        }});
      }});
    }}

    function maybeRevealHighlight(highlight) {{
      const nextKey = highlightKey(highlight);
      const changed = Boolean(lastHighlightKey) && Boolean(nextKey) && nextKey !== lastHighlightKey;
      lastHighlightKey = nextKey;
      if (!changed) {{
        return;
      }}
      window.scrollTo({{ top: 0, behavior: "smooth" }});
      const container = document.getElementById("highlight-panel");
      const card = container.firstElementChild;
      if (!card) {{
        return;
      }}
      card.classList.remove("flash");
      void card.offsetWidth;
      card.classList.add("flash");
      window.setTimeout(() => card.classList.remove("flash"), 1400);
    }}

    function renderPortfolio(latest) {{
      if (!latest) {{
        return '<div class="empty">No holdings snapshot yet.</div>';
      }}
      const holdings = latest.holdings_quote || {{}};
      const rows = Object.entries(holdings)
        .sort((left, right) => Number(right[1]) - Number(left[1]))
        .map(([inst, value]) => [inst, money(Number(value))]);
      if (!rows.length) {{
        rows.push(["Holdings", "Flat"]);
      }}
      const picks = (latest.picks || []).join(", ") || "None";
      rows.unshift(["Preferred picks", picks]);
      rows.unshift(["Cash", money(Number(latest.cash_quote || 0))]);
      return renderList(rows);
    }}

    function renderHighlight(highlight) {{
      if (!highlight) {{
        return '<div class="empty">No orders, stop-loss actions, or circuit-breaker transitions recorded yet.</div>';
      }}
      const badgeClass = highlight.severity === "danger"
        ? "badge warn"
        : highlight.severity === "live"
          ? "badge live"
          : "badge";
      const panelClass = "highlight-panel" + (
        highlight.severity === "danger"
          ? " danger"
          : highlight.severity === "live"
            ? " live"
            : ""
      );
      const facts = (highlight.facts || []).map((fact) => (
        '<div class="fact-card">' +
          '<div class="fact-label">' + escapeHtml(fact.label || "") + '</div>' +
          '<div class="fact-value">' + escapeHtml(fact.value || "n/a") + '</div>' +
        '</div>'
      )).join("");
      return (
        '<div class="' + panelClass + '">' +
          '<div class="highlight-top">' +
            '<span class="' + badgeClass + '">' + escapeHtml(highlight.label || "Event") + '</span>' +
            '<span class="cell-muted">' + escapeHtml(toLocalTime(highlight.ts)) + '</span>' +
          '</div>' +
          '<div class="highlight-title">' + escapeHtml(highlight.title || "Latest event") + '</div>' +
          '<div class="highlight-summary">' + escapeHtml(highlight.summary || "Guardian captured a fresh event.") + '</div>' +
          '<div class="fact-grid">' + facts + '</div>' +
        '</div>'
      );
    }}

    function renderHoldingChanges(activity) {{
      if (!activity) {{
        return "";
      }}
      const changes = activity.holding_changes || [];
      const chips = changes.map((change) => {{
        const deltaQuote = Number(change.delta_quote || 0);
        const deltaSize = Number(change.delta_size || 0);
        const beforeSize = Number(change.before_size || 0);
        const afterSize = Number(change.after_size || 0);
        const showSize = Math.abs(beforeSize) > 1e-9 || Math.abs(afterSize) > 1e-9 || Math.abs(deltaSize) > 1e-9;
        const tone = toneClass(deltaQuote);
        return (
          '<div class="change-chip ' + tone + '">' +
            '<div>' +
              '<div class="change-tags"><span class="action-pill ' + positionActionClass(change.position_action) + '">' +
                escapeHtml(positionActionLabel(change.position_action)) +
              '</span></div>' +
              '<div class="change-name">' + escapeHtml(change.inst_id || "") + '</div>' +
              '<div class="cell-muted">' + money(Number(change.before_quote || 0)) + ' -> ' + money(Number(change.after_quote || 0)) + '</div>' +
              (showSize
                ? '<div class="cell-muted">Qty ' + numberText(beforeSize) + ' -> ' + numberText(afterSize) + '</div>'
                : '') +
            '</div>' +
            '<div class="change-values">' +
              '<div class="change-delta ' + tone + '">' + signedMoney(deltaQuote) + '</div>' +
              (showSize ? '<div class="cell-muted">' + signedNumber(deltaSize) + '</div>' : '') +
            '</div>' +
          '</div>'
        );
      }}).join("");
      const noChanges = chips || '<div class="cell-muted">No position delta captured between adjacent guardian snapshots.</div>';
      return (
        '<div class="rebalance-change">' +
          '<div class="section-label">Holdings Shift</div>' +
          '<div class="rebalance-change-summary">' +
            '<span class="badge live">Before ' + money(Number(activity.before_holdings_quote || 0)) + '</span>' +
            '<span class="badge live">After ' + money(Number(activity.after_holdings_quote || 0)) + '</span>' +
            '<span class="' + (Number(activity.net_holdings_quote_delta || 0) < 0 ? 'badge warn' : 'badge') + '">' +
              escapeHtml('Delta ' + signedMoney(Number(activity.net_holdings_quote_delta || 0))) +
            '</span>' +
          '</div>' +
          '<div class="change-grid">' + noChanges + '</div>' +
        '</div>'
      );
    }}

    function renderOrders(activity) {{
      if (!activity) {{
        return '<div class="empty">No rebalance orders have been captured yet.</div>';
      }}
      const statusClass = activity.status === "filled"
        ? "badge"
        : activity.status === "partial"
          ? "badge live"
          : "badge warn";
      const header = renderList([
        ["Timestamp", toLocalTime(activity.ts)],
        ["Orders", String(activity.planned_count || 0)],
        ["Executed", String(activity.executed_count || 0)],
        ["Status", '<span class="' + statusClass + '">' + escapeHtml(activity.status || "planned") + '</span>'],
        ["Picks", escapeHtml((activity.picks || []).join(", ") || "None")],
      ]);
      const rows = (activity.orders || []).map((order) => {{
        const orderStatusClass = order.status === "filled" ? "badge" : "badge warn";
        const reasonBadge = order.event_kind === "stop-loss" ? '<span class="badge warn">stop</span>' : "";
        const side = String(order.side || "").toUpperCase();
        const inst = order.inst_id || "";
        return (
          '<div class="order-chip">' +
            '<div class="order-title"><span>' + escapeHtml((side ? side + " " : "") + inst) + '</span>' +
              '<span style="display:flex;gap:8px;align-items:center;">' +
                reasonBadge +
                '<span class="' + orderStatusClass + '">' + escapeHtml(order.status || "planned") + '</span>' +
              '</span>' +
            '</div>' +
            '<div class="cell-muted">' + money(Number(order.est_quote_value || 0)) + '</div>' +
            '<div class="order-reason">' + escapeHtml(order.reason || "no reason captured") + '</div>' +
          '</div>'
        );
      }}).join("");
      return header + renderHoldingChanges(activity) + '<div class="section-label">Orders</div><div class="order-stack">' + rows + '</div>';
    }}

    function renderEventFacts(event) {{
      return (event.facts || []).map((fact) => (
        '<div class="event-fact">' +
          '<div class="fact-label">' + escapeHtml(fact.label || "") + '</div>' +
          '<div class="fact-value">' + escapeHtml(fact.value || "n/a") + '</div>' +
        '</div>'
      )).join("");
    }}

    function renderMarketState(latest) {{
      if (!latest || !latest.market_state) {{
        return '<div class="empty">No market-state snapshot has been captured yet.</div>';
      }}
      const state = latest.market_state;
      return renderList([
        ["Risk score", pct(Number(state.risk_score || 0))],
        ["Exposure", pct(Number(state.exposure_multiplier || 0))],
        ["Entries allowed", state.entries_allowed ? "Yes" : "No"],
        ["Reduce only", state.reduce_only ? "Yes" : "No"],
        ["Reason", state.reason || "n/a"],
      ]);
    }}

    function renderBacktest(backtest) {{
      if (!backtest) {{
        return '<div class="empty">No backtest report found under the configured reports directory.</div>';
      }}
      return renderList([
        ["Window", String(backtest.years || "?") + "y"],
        ["Return", pct(Number(backtest.total_return || 0))],
        ["Sharpe", Number(backtest.sharpe_ratio || 0).toFixed(2)],
        ["Max drawdown", pct(Number(backtest.max_drawdown || 0))],
        ["Trades", String(backtest.total_trades || 0)],
      ]);
    }}

    function renderEventRows(events) {{
      return events.map((event) => {{
        const kindClass = event.category === 'circuit-breaker'
          ? 'badge warn'
          : event.category === 'stop-loss'
            ? 'badge warn'
            : 'badge';
        const statusClass = event.severity === 'danger'
          ? 'badge warn'
          : event.severity === 'live'
            ? 'badge live'
            : 'badge';
        let details = (
          '<div class="event-summary">' + escapeHtml(event.summary || 'no details captured') + '</div>' +
          '<div class="event-facts">' + renderEventFacts(event) + '</div>'
        );
        if (event.orders && event.orders.length) {{
          const orders = event.orders.map((order) => (
            '<div class="order-chip">' +
              '<div class="order-title"><span>' + escapeHtml(String(order.side || '').toUpperCase() + ' ' + (order.inst_id || '')) + '</span>' +
                '<span style="display:flex;gap:8px;align-items:center;">' +
                  (order.event_kind === 'stop-loss' ? '<span class="badge warn">stop</span>' : '') +
                  '<span class="' + (order.status === 'filled' ? 'badge' : 'badge warn') + '">' + escapeHtml(order.status || 'planned') + '</span>' +
                '</span>' +
              '</div>' +
              '<div class="cell-muted">' + money(Number(order.est_quote_value || 0)) + '</div>' +
              '<div class="order-reason">' + escapeHtml(order.reason || 'no reason captured') + '</div>' +
            '</div>'
          )).join('');
          details += renderHoldingChanges(event) + '<div class="section-label">Orders</div><div class="order-stack">' + orders + '</div>';
        }}
        return (
          '<div class="tape-row">' +
            '<div>' + escapeHtml(toLocalTime(event.ts)) + '</div>' +
            '<div><span class="' + kindClass + '">' + escapeHtml(event.label || event.category || 'event') + '</span></div>' +
            '<div><span class="' + statusClass + '">' + escapeHtml(event.status || 'n/a') + '</span></div>' +
            '<div>' + escapeHtml(event.metric_label || 'Metric') + ': ' + money(Number(event.metric_value || 0)) + '</div>' +
            '<div>' + details + '</div>' +
          '</div>'
        );
      }}).join('');
    }}

    function renderEventGroup(category, events) {{
      const label = EVENT_GROUP_LABELS[category] || category;
      const collapsed = Boolean(collapsedEventGroups[category]);
      const latestTs = events.length ? toLocalTime(events[0].ts) : "n/a";
      const head = (
        '<div class="tape-head">' +
          '<div>Timestamp</div>' +
          '<div>Type</div>' +
          '<div>Status</div>' +
          '<div>Metric</div>' +
          '<div>Details</div>' +
        '</div>'
      );
      return (
        '<details class="event-group" data-category="' + escapeHtml(category) + '"' + (collapsed ? '' : ' open') + '>' +
          '<summary class="event-group-summary">' +
            '<span class="event-group-summary-main">' +
              '<span class="' + (category === 'order' ? 'badge' : 'badge warn') + '">' + escapeHtml(label) + '</span>' +
              '<span>' + escapeHtml(String(events.length)) + ' events</span>' +
            '</span>' +
            '<span class="event-group-summary-main">' +
              '<span class="event-group-summary-meta">Latest ' + escapeHtml(latestTs) + '</span>' +
              '<span class="event-group-caret"></span>' +
            '</span>' +
          '</summary>' +
          '<div class="event-group-body">' +
            '<div class="tape-table">' + head + renderEventRows(events) + '</div>' +
          '</div>' +
        '</details>'
      );
    }}

    function renderEventTape(events) {{
      if (!events || !events.length) {{
        return '<div class="empty">No recent events matched the active filter.</div>';
      }}
      const groups = groupedEvents(events);
      if (!groups.length) {{
        return '<div class="empty">No recent events matched the active filter.</div>';
      }}
      return '<div class="event-groups">' + groups.map(([category, rows]) => renderEventGroup(category, rows)).join('') + '</div>';
    }}

    async function refresh() {{
      const response = await fetch("/api/snapshot?limit=" + DEFAULT_LIMIT, {{ cache: "no-store" }});
      if (!response.ok) {{
        throw new Error("dashboard api returned " + response.status);
      }}
      const payload = await response.json();
      const latest = payload.latest_tick;
      const curve = payload.curve || [];
      const activities = payload.recent_rebalances || [];
      const recentEvents = payload.recent_events || [];
      const visibleEvents = filteredEvents(recentEvents);
      const latestRebalance = payload.latest_rebalance || (activities.length ? activities[0] : null);
      const status = payload.status || {{}};
      const meta = payload.meta || {{}};

      document.getElementById("message").textContent = status.message || "Dashboard online.";
      document.getElementById("curve-count").textContent = String(status.curve_points || 0) + " points";
      document.getElementById("updated-at").textContent = toLocalTime(status.updated_at);
      document.getElementById("event-log").textContent = meta.event_log_path || "n/a";

      const streamStatus = document.getElementById("stream-status");
      streamStatus.textContent = status.has_guardian_data ? "Guardian stream detected" : "No guardian ticks yet";
      streamStatus.className = "status-pill" + (latest && latest.trading_halted ? " alert" : "");

      document.getElementById("equity-value").textContent = latest ? money(Number(latest.equity_quote || 0)) : "--";
      document.getElementById("equity-note").textContent = latest ? "Available cash " + money(Number(latest.cash_quote || 0)) : "Waiting for guardian ticks";
      document.getElementById("nav-value").textContent = latest ? pct(Number(latest.nav || 0) - 1) : "--";
      document.getElementById("nav-note").textContent = latest ? "Latest timestamp " + toLocalTime(latest.ts) : "Baseline vs guardian start";
      document.getElementById("drawdown-value").textContent = latest ? pct(Number(latest.drawdown || 0)) : "--";
      document.getElementById("drawdown-note").textContent = latest ? "Current drawdown from guardian log" : "Portfolio stress monitor";
      document.getElementById("halt-value").textContent = latest ? (latest.trading_halted ? "HALTED" : "ACTIVE") : "--";
      document.getElementById("halt-note").textContent = latest ? (latest.halt_reason || "No active halt reason") : "Guardian has not streamed any state yet";

      document.getElementById("highlight-panel").innerHTML = renderHighlight(payload.latest_highlight);
      maybeRevealHighlight(payload.latest_highlight);
      document.getElementById("portfolio-panel").innerHTML = renderPortfolio(latest);
      document.getElementById("orders-panel").innerHTML = renderOrders(latestRebalance);
      document.getElementById("market-state").innerHTML = renderMarketState(latest);
      document.getElementById("backtest-panel").innerHTML = renderBacktest(payload.latest_backtest);
      refreshFilterButtons();
      document.getElementById("tape-panel").innerHTML = renderEventTape(visibleEvents);
      bindEventGroupToggles();

      if (curve.length) {{
        const equities = curve.map((point) => Number(point.equity_quote || 0));
        const drawdowns = curve.map((point) => Number(point.drawdown || 0));
        document.getElementById("equity-line").setAttribute("points", linePoints(equities, 360, 900));
        document.getElementById("drawdown-line").setAttribute("points", linePoints(drawdowns, 360, 900, true));
        document.getElementById("chart-start").textContent = toLocalTime(curve[0].ts);
        document.getElementById("chart-end").textContent = toLocalTime(curve[curve.length - 1].ts);
      }} else {{
        document.getElementById("equity-line").setAttribute("points", "");
        document.getElementById("drawdown-line").setAttribute("points", "");
        document.getElementById("chart-start").textContent = "Start";
        document.getElementById("chart-end").textContent = "Now";
      }}
    }}

    async function boot() {{
      loadUiState();
      refreshFilterButtons();
      document.querySelectorAll("#event-filters [data-filter]").forEach((button) => {{
        button.addEventListener("click", () => {{
          currentEventFilter = normalizeFilter(button.dataset.filter || "all");
          persistFilter();
          refreshFilterButtons();
          refresh().catch((error) => {{
            document.getElementById("message").textContent = "Dashboard refresh failed: " + error.message;
          }});
        }});
      }});
      try {{
        await refresh();
      }} catch (error) {{
        document.getElementById("message").textContent = "Dashboard load failed: " + error.message;
        document.getElementById("stream-status").textContent = "API unavailable";
        document.getElementById("stream-status").className = "status-pill alert";
      }}
      setInterval(() => {{
        refresh().catch((error) => {{
          document.getElementById("message").textContent = "Dashboard refresh failed: " + error.message;
        }});
      }}, 5000);
    }}

    boot();
  </script>
</body>
</html>
"""


class FactorDashboardServer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("okx_quant.dashboard")
        self.store = DashboardDataStore(settings)

    def serve(self, host: str = "127.0.0.1", port: int = 8787, *, default_limit: int = 240) -> None:
        store = self.store
        logger = self.logger
        html = _dashboard_html(default_limit)

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
                body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                self.send_response(status.value)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, content: str) -> None:
                body = content.encode("utf-8")
                self.send_response(HTTPStatus.OK.value)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send_html(html)
                    return
                if parsed.path == "/api/snapshot":
                    query = parse_qs(parsed.query)
                    limit = default_limit
                    if "limit" in query:
                        try:
                            limit = max(10, min(1000, int(query["limit"][0])))
                        except (TypeError, ValueError):
                            limit = default_limit
                    self._send_json(store.snapshot(limit=limit))
                    return
                if parsed.path == "/health":
                    self._send_json({"ok": True})
                    return
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

            def log_message(self, format: str, *args: object) -> None:
                logger.info("dashboard %s", format % args)

        server = ThreadingHTTPServer((host, port), Handler)
        self.logger.info("Dashboard listening at http://%s:%s", host, port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            self.logger.info("Dashboard server interrupted, shutting down")
        finally:
            server.server_close()
