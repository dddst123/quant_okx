from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal

from okx_quant.guardian import GuardianState, GuardianStateStore


class GuardianStateStoreTest(unittest.TestCase):
    def test_state_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = GuardianStateStore(f"{tmpdir}/guardian_state.json")
            state = GuardianState(
                initial_equity_quote=Decimal("123.45"),
                last_daily_summary_date="2026-04-10",
                last_trading_halted=True,
                last_halt_reason="drawdown",
            )

            store.save(state)
            loaded = store.load()

            self.assertEqual(loaded.initial_equity_quote, Decimal("123.45"))
            self.assertEqual(loaded.last_daily_summary_date, "2026-04-10")
            self.assertTrue(loaded.last_trading_halted)
            self.assertEqual(loaded.last_halt_reason, "drawdown")


if __name__ == "__main__":
    unittest.main()
