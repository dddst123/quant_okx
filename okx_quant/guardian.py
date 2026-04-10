from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

from okx_quant.alerts import AlertManager
from okx_quant.config import Settings
from okx_quant.factor_bot import FactorPortfolioBot, FactorRunSnapshot


@dataclass
class GuardianState:
    initial_equity_quote: Decimal = Decimal("0")
    last_daily_summary_date: str = ""
    last_trading_halted: bool = False
    last_halt_reason: str = ""


class GuardianStateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> GuardianState:
        if not self.path.exists():
            return GuardianState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return GuardianState(
            initial_equity_quote=Decimal(data.get("initial_equity_quote", "0")),
            last_daily_summary_date=data.get("last_daily_summary_date", ""),
            last_trading_halted=bool(data.get("last_trading_halted", False)),
            last_halt_reason=data.get("last_halt_reason", ""),
        )

    def save(self, state: GuardianState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "initial_equity_quote": str(state.initial_equity_quote),
            "last_daily_summary_date": state.last_daily_summary_date,
            "last_trading_halted": state.last_trading_halted,
            "last_halt_reason": state.last_halt_reason,
        }
        tmp_path: str | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                dir=self.path.parent,
                prefix=".tmp_",
                suffix=".json",
                encoding="utf-8",
                delete=False,
            ) as tmp:
                tmp_path = tmp.name
                json.dump(payload, tmp, indent=2, ensure_ascii=True)
            Path(tmp_path).replace(self.path)
        except BaseException:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
            raise


class FactorGuardian:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bot = FactorPortfolioBot(settings)
        self.alerts = AlertManager(settings)
        self.logger = logging.getLogger("okx_quant.guardian")
        self.state_store = GuardianStateStore(settings.factor_guardian_state_path)
        self.output_dir = Path(settings.factor_guardian_output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.event_log_path = self.output_dir / "factor_guardian_events.jsonl"
        self.daily_log_path = self.output_dir / "factor_guardian_daily.jsonl"

    def _local_date(self, ts: datetime) -> str:
        return ts.astimezone().date().isoformat()

    def _append_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")

    def _snapshot_payload(self, snapshot: FactorRunSnapshot, state: GuardianState) -> dict[str, object]:
        baseline = state.initial_equity_quote or snapshot.total_equity_quote or Decimal("1")
        nav = float(snapshot.total_equity_quote / baseline) if baseline > 0 else 1.0
        return {
            "event_type": "tick",
            "ts": snapshot.ts.isoformat(),
            "date": self._local_date(snapshot.ts),
            "equity_quote": str(snapshot.total_equity_quote),
            "cash_quote": str(snapshot.available_quote),
            "drawdown": str(snapshot.drawdown),
            "trading_halted": snapshot.trading_halted,
            "halt_reason": snapshot.halt_reason,
            "nav": nav,
            "holdings": {inst_id: str(size) for inst_id, size in snapshot.holdings.items()},
            "holdings_quote": {inst_id: str(notional) for inst_id, notional in snapshot.holdings_quote.items()},
            "picks": [pick.inst_id for pick in snapshot.picks],
            "planned_orders": [
                {
                    "inst_id": order.inst_id,
                    "side": order.side,
                    "size": str(order.size),
                    "target_currency": order.target_currency,
                    "est_quote_value": str(order.est_quote_value),
                    "reason": order.reason,
                }
                for order in snapshot.planned_orders
            ],
            "executed_orders": snapshot.executed_orders,
            "market_state": None if snapshot.market_state is None else snapshot.market_state.to_dict(),
        }

    def _holdings_text(self, snapshot: FactorRunSnapshot) -> str:
        return ", ".join(
            f"{inst}={amount}"
            for inst, amount in sorted(snapshot.holdings_quote.items(), key=lambda item: item[1], reverse=True)
        ) or "flat"

    def _send_state_change_alert(
        self,
        snapshot: FactorRunSnapshot,
        state: GuardianState,
        *,
        transition: str,
        previous_reason: str,
    ) -> None:
        baseline = state.initial_equity_quote or snapshot.total_equity_quote or Decimal("1")
        nav = float(snapshot.total_equity_quote / baseline) if baseline > 0 else 1.0
        holdings_text = self._holdings_text(snapshot)
        if transition == "halted":
            self.alerts.send(
                "Guardian Halt",
                (
                    f"date={self._local_date(snapshot.ts)} equity={snapshot.total_equity_quote} nav={nav:.4f} "
                    f"drawdown={snapshot.drawdown:.2%} halt_reason={snapshot.halt_reason or 'unknown'} "
                    f"holdings={holdings_text}"
                ),
            )
            return
        self.alerts.send(
            "Guardian Resume",
            (
                f"date={self._local_date(snapshot.ts)} equity={snapshot.total_equity_quote} nav={nav:.4f} "
                f"drawdown={snapshot.drawdown:.2%} resumed_from={previous_reason or 'unknown'} "
                f"holdings={holdings_text}"
            ),
        )

    def _send_daily_summary(self, snapshot: FactorRunSnapshot, state: GuardianState) -> None:
        holdings_text = self._holdings_text(snapshot)
        baseline = state.initial_equity_quote or snapshot.total_equity_quote or Decimal("1")
        nav = float(snapshot.total_equity_quote / baseline) if baseline > 0 else 1.0
        self.alerts.send(
            "Daily Guardian Summary",
            (
                f"date={self._local_date(snapshot.ts)} equity={snapshot.total_equity_quote} "
                f"nav={nav:.4f} drawdown={snapshot.drawdown:.2%} halted={snapshot.trading_halted} "
                f"halt_reason={snapshot.halt_reason or 'none'} holdings={holdings_text}"
            ),
        )

    def run_once(self) -> FactorRunSnapshot:
        state = self.state_store.load()
        snapshot = self.bot.run_once()
        if state.initial_equity_quote <= 0:
            state.initial_equity_quote = snapshot.total_equity_quote

        payload = self._snapshot_payload(snapshot, state)
        self._append_jsonl(self.event_log_path, payload)

        local_date = payload["date"]
        if state.last_daily_summary_date != local_date:
            self._append_jsonl(self.daily_log_path, payload)
            self._send_daily_summary(snapshot, state)
            state.last_daily_summary_date = str(local_date)

        if state.last_trading_halted != snapshot.trading_halted:
            transition = "halted" if snapshot.trading_halted else "resumed"
            transition_payload = {
                "event_type": transition,
                "ts": snapshot.ts.isoformat(),
                "date": str(local_date),
                "equity_quote": str(snapshot.total_equity_quote),
                "drawdown": str(snapshot.drawdown),
                "nav": payload["nav"],
                "previous_halt_reason": state.last_halt_reason,
                "halt_reason": snapshot.halt_reason,
                "holdings_quote": payload["holdings_quote"],
            }
            self._append_jsonl(self.event_log_path, transition_payload)
            self._send_state_change_alert(
                snapshot,
                state,
                transition=transition,
                previous_reason=state.last_halt_reason,
            )

        state.last_trading_halted = snapshot.trading_halted
        state.last_halt_reason = snapshot.halt_reason if snapshot.trading_halted else ""
        self.state_store.save(state)
        return snapshot

    def serve(self, max_iterations: int | None = None) -> None:
        iteration = 0
        error_sleep = self.settings.factor_rebalance_interval_sec
        while max_iterations is None or iteration < max_iterations:
            try:
                snapshot = self.run_once()
                self.logger.info(
                    "Guardian tick %s equity=%s drawdown=%s halted=%s holdings=%s",
                    snapshot.ts.isoformat(),
                    snapshot.total_equity_quote,
                    snapshot.drawdown,
                    snapshot.trading_halted,
                    sorted(snapshot.holdings),
                )
                error_sleep = self.settings.factor_rebalance_interval_sec
            except Exception:
                self.logger.exception("Guardian loop failed")
                error_sleep = min(error_sleep * 2, 1800)
            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break
            time.sleep(error_sleep)

    def serve_forever(self) -> None:
        self.serve()
