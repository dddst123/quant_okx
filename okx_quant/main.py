from __future__ import annotations

import argparse

from okx_quant.backtest import FactorBacktester
from okx_quant.bot import TradingBot
from okx_quant.config import Settings
from okx_quant.dashboard import FactorDashboardServer
from okx_quant.factor_bot import FactorPortfolioBot
from okx_quant.guardian import FactorGuardian
from okx_quant.logging_utils import configure_logging
from okx_quant.walk_forward import FactorWalkForwardAnalyzer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OKX quant trading MVP")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("signal", help="Fetch candles and print the latest strategy signal")
    subparsers.add_parser("balances", help="Fetch current base and quote balances")
    subparsers.add_parser("once", help="Run one trading cycle")
    subparsers.add_parser("run", help="Run the continuous trading loop")
    subparsers.add_parser("factors-rank", help="Rank liquid OKX spot assets with the volume-confirmed trend factor")
    subparsers.add_parser("factors-market-state", help="Inspect the current market-state snapshot used for de-risking")
    subparsers.add_parser("factors-once", help="Run one factor-portfolio rebalance cycle")
    subparsers.add_parser("factors-run", help="Run the continuous factor-portfolio rebalance loop")
    guard = subparsers.add_parser("factors-guard", help="Run the simulated guardian loop with daily equity/holding summaries")
    guard.add_argument("--once", action="store_true", help="Run one guardian tick and exit")
    guard.add_argument("--max-loops", type=int, default=None, help="Run the guardian loop for a fixed number of ticks")
    dashboard = subparsers.add_parser("factors-dashboard", help="Serve a local dashboard for guardian curves and portfolio state")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8787)
    backtest = subparsers.add_parser("factors-backtest", help="Run factor backtests for the requested year windows")
    backtest.add_argument("--years", type=int, nargs="*", default=None, help="Backtest windows in years, e.g. --years 1 2 3")
    walk_forward = subparsers.add_parser("factors-walk-forward", help="Run rolling train/validation walk-forward analysis")
    walk_forward.add_argument("--lookback-years", type=int, default=4)
    walk_forward.add_argument("--train-days", type=int, default=365)
    walk_forward.add_argument("--test-days", type=int, default=90)
    walk_forward.add_argument("--step-days", type=int, default=90)
    walk_forward.add_argument("--search-profile", choices=("quick", "default", "full"), default="default")
    walk_forward.add_argument("--max-configs", type=int, default=None, help="Limit the number of parameter sets evaluated per split")
    subparsers.add_parser("factors-risk-reset", help="Reset the persisted factor risk state and circuit breaker")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = Settings()
    settings.validate()
    configure_logging(settings)
    bot = TradingBot(settings)
    factor_bot = FactorPortfolioBot(settings)
    backtester = FactorBacktester(settings)
    guardian = FactorGuardian(settings)
    dashboard = FactorDashboardServer(settings)
    walk_forward = FactorWalkForwardAnalyzer(settings)

    if args.command == "signal":
        signal = bot.get_signal_only()
        print(f"instrument={settings.instrument_id} price={signal.price} action={signal.action.value} reason={signal.reason}")
        return

    if args.command == "balances":
        balances = bot.get_balances()
        print(f"{settings.base_currency}={balances[settings.base_currency]} {settings.quote_currency}={balances[settings.quote_currency]}")
        return

    if args.command == "once":
        snapshot = bot.run_once()
        print(
            "instrument={inst} price={price} action={action} decision={decision} order={order}".format(
                inst=settings.instrument_id,
                price=snapshot.price,
                action=snapshot.signal.action.value,
                decision=snapshot.decision.reason if snapshot.decision else "n/a",
                order=snapshot.order_result or "none",
            )
        )
        return

    if args.command == "factors-rank":
        picks = factor_bot.rank_candidates()
        if not picks:
            print("No factor candidates passed the liquidity and trend filters.")
            return
        for pick in picks:
            print(

                    f"inst={pick.inst_id} weight={pick.weight:.2%} score={pick.score:.4f} "
                    f"mom7={pick.momentum_short:.2%} mom28={pick.momentum_medium:.2%} mom60={pick.momentum_long:.2%} "
                    f"fast_gap={pick.fast_gap:.2%} slow_gap={pick.slow_gap:.2%} vol_ratio={pick.volume_ratio:.2f}"

            )
        return

    if args.command == "factors-market-state":
        market_state = factor_bot.inspect_market_state()
        print(
            (
                "ts={ts} entries_allowed={entries} reduce_only={reduce_only} risk_score={risk_score:.2f} "
                "exposure_multiplier={exposure:.2f} breadth={breadth:.2%} benchmark_momentum={momentum:.2%} "
                "benchmark_slow_gap={slow_gap:.2%} spread_bps={spread} funding_rate={funding} "
                "open_interest_usd={oi} reason={reason}"
            ).format(
                ts=market_state.ts.isoformat(),
                entries=market_state.entries_allowed,
                reduce_only=market_state.reduce_only,
                risk_score=market_state.risk_score,
                exposure=market_state.exposure_multiplier,
                breadth=market_state.breadth,
                momentum=market_state.benchmark_momentum,
                slow_gap=market_state.benchmark_slow_gap,
                spread="n/a" if market_state.spread_bps is None else f"{market_state.spread_bps:.2f}",
                funding="n/a" if market_state.funding_rate is None else f"{market_state.funding_rate:.4%}",
                oi="n/a" if market_state.open_interest_usd is None else str(market_state.open_interest_usd),
                reason=market_state.reason,
            )
        )
        return

    if args.command == "factors-once":
        factor_snapshot = factor_bot.run_once()
        print(
            "equity={equity} cash={cash} drawdown={drawdown} halted={halted} picks={picks} planned_orders={orders} executed_orders={executed} market_state={market_state}".format(
                equity=factor_snapshot.total_equity_quote,
                cash=factor_snapshot.available_quote,
                drawdown=factor_snapshot.drawdown,
                halted=factor_snapshot.trading_halted,
                picks=",".join(pick.inst_id for pick in factor_snapshot.picks) or "none",
                orders=len(factor_snapshot.planned_orders),
                executed=len(factor_snapshot.executed_orders),
                market_state=factor_snapshot.market_state.reason if factor_snapshot.market_state is not None else "n/a",
            )
        )
        for order in factor_snapshot.planned_orders:
            print(
                f"order side={order.side} inst={order.inst_id} size={order.size} est_quote={order.est_quote_value} reason={order.reason}"
            )
        return

    if args.command == "factors-run":
        factor_bot.serve_forever()
        return

    if args.command == "factors-guard":
        if args.once:
            guard_snapshot = guardian.run_once()
            print(
                "ts={ts} equity={equity} cash={cash} drawdown={drawdown} halted={halted} halt_reason={reason} holdings={holdings} event_log={event_log} daily_log={daily_log}".format(
                    ts=guard_snapshot.ts.isoformat(),
                    equity=guard_snapshot.total_equity_quote,
                    cash=guard_snapshot.available_quote,
                    drawdown=guard_snapshot.drawdown,
                    halted=guard_snapshot.trading_halted,
                    reason=guard_snapshot.halt_reason or "none",
                    holdings=",".join(sorted(guard_snapshot.holdings)) or "flat",
                    event_log=guardian.event_log_path,
                    daily_log=guardian.daily_log_path,
                )
            )
            return
        guardian.serve(max_iterations=args.max_loops)
        return

    if args.command == "factors-dashboard":
        dashboard.serve(host=args.host, port=args.port)
        return

    if args.command == "factors-backtest":
        years = args.years or list(settings.factor_backtest_years)
        reports = backtester.run_many(years)
        for report in reports:
            print(
                (
                    "years={years} start={start} end={end} total_return={total:.2%} cagr={cagr:.2%} "
                    "sharpe={sharpe:.2f} max_drawdown={mdd:.2%} benchmark={benchmark} trades={trades} "
                    "report={report_path} equity_curve={equity_curve_path}"
                ).format(
                    years=report.years,
                    start=report.start,
                    end=report.end,
                    total=report.total_return,
                    cagr=report.cagr,
                    sharpe=report.sharpe_ratio,
                    mdd=report.max_drawdown,
                    benchmark="n/a" if report.benchmark_return is None else f"{report.benchmark_return:.2%}",
                    trades=report.total_trades,
                    report_path=report.report_path,
                    equity_curve_path=report.equity_curve_path,
                )
            )
        return

    if args.command == "factors-walk-forward":
        wf_report = walk_forward.run(
            lookback_years=args.lookback_years,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            search_profile=args.search_profile,
            max_configs=args.max_configs,
        )
        print(
            f"lookback_years={wf_report.lookback_years} train_days={wf_report.train_days} test_days={wf_report.test_days} step_days={wf_report.step_days} "
            f"profile={wf_report.search_profile} configs={wf_report.config_count} splits={len(wf_report.splits)} oos_total_return={wf_report.out_of_sample_total_return:.2%} oos_max_drawdown={wf_report.out_of_sample_max_drawdown:.2%} "
            f"avg_test_return={wf_report.avg_test_return:.2%} avg_test_sharpe={wf_report.avg_test_sharpe:.2f} report={wf_report.report_path}"
        )
        return

    if args.command == "factors-risk-reset":
        factor_bot.reset_risk_state()
        print(f"risk_state_reset path={settings.factor_state_path}")
        return

    bot.serve_forever()


if __name__ == "__main__":
    main()
