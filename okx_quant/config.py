from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv


def _load_env_files() -> None:
    cwd = Path.cwd()
    load_dotenv(cwd / ".env", override=False)

    profile = os.getenv("OKX_ENV_PROFILE", "").strip().lower()
    explicit_file = os.getenv("OKX_ENV_FILE", "").strip()
    selected: Path | None = None
    if explicit_file:
        selected = Path(explicit_file).expanduser()
        if not selected.is_absolute():
            selected = cwd / selected
    elif profile in {"simulated", "demo", "paper"}:
        selected = cwd / ".env.simulated"
    elif profile in {"live", "real", "prod", "production"}:
        selected = cwd / ".env.live"

    if selected is not None:
        load_dotenv(selected, override=True)


_load_env_files()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value is not None else default


def _env_decimal(name: str, default: str) -> Decimal:
    value = os.getenv(name)
    return Decimal(value) if value is not None else Decimal(default)


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_int_list(name: str, default: str) -> list[int]:
    return [int(item) for item in _env_list(name, default)]


def _env_decimal_pairs(name: str, default: str) -> tuple[tuple[Decimal, Decimal], ...]:
    raw = os.getenv(name, default).strip()
    if not raw:
        return tuple()
    pairs: list[tuple[Decimal, Decimal]] = []
    for item in raw.split(","):
        left, right = item.split(":", 1)
        pairs.append((Decimal(left.strip()), Decimal(right.strip())))
    return tuple(pairs)


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("OKX_API_KEY", "")
    api_secret: str = os.getenv("OKX_API_SECRET", "")
    api_passphrase: str = os.getenv("OKX_API_PASSPHRASE", "")
    simulated: bool = _env_bool("OKX_SIMULATED", True)
    dry_run: bool = _env_bool("OKX_DRY_RUN", True)
    base_url: str = os.getenv("OKX_BASE_URL", "https://www.okx.com")
    instrument_id: str = os.getenv("OKX_INST_ID", "BTC-USDT")
    bar: str = os.getenv("OKX_BAR", "5m")
    fast_window: int = _env_int("OKX_FAST_WINDOW", 9)
    slow_window: int = _env_int("OKX_SLOW_WINDOW", 21)
    candles_limit: int = _env_int("OKX_CANDLES_LIMIT", 120)
    poll_interval_sec: int = _env_int("OKX_POLL_INTERVAL_SEC", 30)
    trade_amount_quote: Decimal = _env_decimal("OKX_TRADE_AMOUNT_QUOTE", "50")
    max_position_quote: Decimal = _env_decimal("OKX_MAX_POSITION_QUOTE", "200")
    min_cash_reserve_quote: Decimal = _env_decimal("OKX_MIN_CASH_RESERVE_QUOTE", "20")
    cooldown_sec: int = _env_int("OKX_COOLDOWN_SEC", 300)
    factor_quote_currency: str = os.getenv("OKX_FACTOR_QUOTE_CCY", "USDT")
    factor_bar: str = os.getenv("OKX_FACTOR_BAR", "1Dutc")
    factor_max_universe_size: int = _env_int("OKX_FACTOR_MAX_UNIVERSE_SIZE", 12)
    factor_universe_candidates: int = _env_int("OKX_FACTOR_UNIVERSE_CANDIDATES", 24)
    factor_top_n: int = _env_int("OKX_FACTOR_TOP_N", 1)
    factor_hold_buffer: int = _env_int("OKX_FACTOR_HOLD_BUFFER", 0)
    factor_min_24h_quote_volume: Decimal = _env_decimal("OKX_FACTOR_MIN_24H_QUOTE_VOLUME", "20000000")
    factor_min_last_price: Decimal = _env_decimal("OKX_FACTOR_MIN_LAST_PRICE", "0.05")
    factor_min_history: int = _env_int("OKX_FACTOR_MIN_HISTORY", 240)
    factor_liquidity_lookback: int = _env_int("OKX_FACTOR_LIQUIDITY_LOOKBACK", 60)
    factor_short_lookback: int = _env_int("OKX_FACTOR_SHORT_LOOKBACK", 7)
    factor_medium_lookback: int = _env_int("OKX_FACTOR_MEDIUM_LOOKBACK", 28)
    factor_long_lookback: int = _env_int("OKX_FACTOR_LONG_LOOKBACK", 60)
    factor_fast_ma: int = _env_int("OKX_FACTOR_FAST_MA", 20)
    factor_slow_ma: int = _env_int("OKX_FACTOR_SLOW_MA", 60)
    factor_volume_lookback: int = _env_int("OKX_FACTOR_VOLUME_LOOKBACK", 20)
    factor_volatility_lookback: int = _env_int("OKX_FACTOR_VOL_LOOKBACK", 20)
    factor_capital_fraction: Decimal = _env_decimal("OKX_FACTOR_CAPITAL_FRACTION", "0.90")
    factor_min_order_quote: Decimal = _env_decimal("OKX_FACTOR_MIN_ORDER_QUOTE", "25")
    factor_max_asset_weight: Decimal = _env_decimal("OKX_FACTOR_MAX_ASSET_WEIGHT", "0.45")
    factor_rebalance_interval_sec: int = _env_int("OKX_FACTOR_REBALANCE_INTERVAL_SEC", 3600)
    factor_rebalance_mode: str = os.getenv("OKX_FACTOR_REBALANCE_MODE", "weekly")
    factor_rebalance_weekday: int = _env_int("OKX_FACTOR_REBALANCE_WEEKDAY", 0)
    factor_max_turnover_per_rebalance: Decimal = _env_decimal("OKX_FACTOR_MAX_TURNOVER_PER_REBALANCE", "0.50")
    factor_regime_fast_ma: int = _env_int("OKX_FACTOR_REGIME_FAST_MA", 50)
    factor_regime_slow_ma: int = _env_int("OKX_FACTOR_REGIME_SLOW_MA", 200)
    factor_regime_momentum_lookback: int = _env_int("OKX_FACTOR_REGIME_MOMENTUM_LOOKBACK", 90)
    factor_regime_breadth_threshold: Decimal = _env_decimal("OKX_FACTOR_REGIME_BREADTH_THRESHOLD", "0.35")
    factor_regime_required_signals: int = _env_int("OKX_FACTOR_REGIME_REQUIRED_SIGNALS", 3)
    factor_benchmark_fallback: bool = _env_bool("OKX_FACTOR_BENCHMARK_FALLBACK", True)
    factor_dynamic_top_n_enabled: bool = _env_bool("OKX_FACTOR_DYNAMIC_TOP_N_ENABLED", True)
    factor_dynamic_top_n: int = _env_int("OKX_FACTOR_DYNAMIC_TOP_N", 2)
    factor_dynamic_top_n_required_signals: int = _env_int("OKX_FACTOR_DYNAMIC_TOP_N_REQUIRED_SIGNALS", 4)
    factor_dynamic_top_n_breadth_threshold: Decimal = _env_decimal("OKX_FACTOR_DYNAMIC_TOP_N_BREADTH_THRESHOLD", "0.55")
    factor_dynamic_top_n_benchmark_momentum: Decimal = _env_decimal("OKX_FACTOR_DYNAMIC_TOP_N_BENCHMARK_MOMENTUM", "0.08")
    factor_target_annual_vol: Decimal = _env_decimal("OKX_FACTOR_TARGET_ANNUAL_VOL", "0.45")
    factor_min_gross_exposure: Decimal = _env_decimal("OKX_FACTOR_MIN_GROSS_EXPOSURE", "0.25")
    factor_max_gross_exposure: Decimal = _env_decimal("OKX_FACTOR_MAX_GROSS_EXPOSURE", "1.00")
    factor_market_state_enabled: bool = _env_bool("OKX_FACTOR_MARKET_STATE_ENABLED", True)
    factor_market_state_swap_inst_id: str = os.getenv("OKX_FACTOR_MARKET_STATE_SWAP_INST_ID", "")
    factor_market_state_order_book_levels: int = _env_int("OKX_FACTOR_MARKET_STATE_ORDER_BOOK_LEVELS", 5)
    factor_market_state_min_breadth: Decimal = _env_decimal("OKX_FACTOR_MARKET_STATE_MIN_BREADTH", "0.25")
    factor_market_state_min_benchmark_momentum: Decimal = _env_decimal(
        "OKX_FACTOR_MARKET_STATE_MIN_BENCHMARK_MOMENTUM",
        "0.00",
    )
    factor_market_state_max_momentum_dispersion: Decimal = _env_decimal(
        "OKX_FACTOR_MARKET_STATE_MAX_MOMENTUM_DISPERSION",
        "0.35",
    )
    factor_market_state_min_volume_ratio: Decimal = _env_decimal("OKX_FACTOR_MARKET_STATE_MIN_VOLUME_RATIO", "0.80")
    factor_market_state_max_spread_bps: Decimal = _env_decimal("OKX_FACTOR_MARKET_STATE_MAX_SPREAD_BPS", "8")
    factor_market_state_min_depth_quote: Decimal = _env_decimal("OKX_FACTOR_MARKET_STATE_MIN_DEPTH_QUOTE", "25000")
    factor_market_state_max_abs_funding_rate: Decimal = _env_decimal(
        "OKX_FACTOR_MARKET_STATE_MAX_ABS_FUNDING_RATE",
        "0.0015",
    )
    factor_market_state_min_open_interest_usd: Decimal = _env_decimal(
        "OKX_FACTOR_MARKET_STATE_MIN_OPEN_INTEREST_USD",
        "200000000",
    )
    factor_market_state_min_exposure: Decimal = _env_decimal("OKX_FACTOR_MARKET_STATE_MIN_EXPOSURE", "0.35")
    factor_market_state_entry_gate: Decimal = _env_decimal("OKX_FACTOR_MARKET_STATE_ENTRY_GATE", "0.60")
    factor_drawdown_scale_tiers: tuple[tuple[Decimal, Decimal], ...] = field(
        default_factory=lambda: _env_decimal_pairs(
            "OKX_FACTOR_DRAWDOWN_SCALE_TIERS",
            "0.08:0.85,0.15:0.60,0.22:0.35,0.30:0.15",
        )
    )
    factor_universe: tuple[str, ...] = field(default_factory=lambda: tuple(_env_list("OKX_FACTOR_UNIVERSE")))
    factor_excluded_bases: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            item.upper()
            for item in _env_list(
                "OKX_FACTOR_EXCLUDED_BASES",
                "USDT,USDC,FDUSD,TUSD,DAI,USDE,EUR,USD",
            )
        )
    )
    factor_stop_loss_pct: Decimal = _env_decimal("OKX_FACTOR_STOP_LOSS_PCT", "0.12")
    factor_trailing_stop_pct: Decimal = _env_decimal("OKX_FACTOR_TRAILING_STOP_PCT", "0.18")
    factor_max_drawdown_pct: Decimal = _env_decimal("OKX_FACTOR_MAX_DRAWDOWN_PCT", "0.32")
    factor_halt_cooldown_days: int = _env_int("OKX_FACTOR_HALT_COOLDOWN_DAYS", 21)
    factor_halt_resume_requires_benchmark_trend: bool = _env_bool(
        "OKX_FACTOR_HALT_RESUME_REQUIRES_BENCHMARK_TREND",
        True,
    )
    factor_halt_resume_confirm_bars: int = _env_int("OKX_FACTOR_HALT_RESUME_CONFIRM_BARS", 5)
    factor_halt_resume_required_signals: int = _env_int("OKX_FACTOR_HALT_RESUME_REQUIRED_SIGNALS", 3)
    factor_liquidate_on_halt: bool = _env_bool("OKX_FACTOR_LIQUIDATE_ON_HALT", True)
    factor_state_path: str = os.getenv("OKX_FACTOR_STATE_PATH", "state/factor_bot_state.json")
    factor_backtest_initial_capital: Decimal = _env_decimal("OKX_FACTOR_BACKTEST_INITIAL_CAPITAL", "10000")
    factor_backtest_years: tuple[int, ...] = field(
        default_factory=lambda: tuple(_env_int_list("OKX_FACTOR_BACKTEST_YEARS", "1,2,3"))
    )
    factor_backtest_fee_rate: Decimal = _env_decimal("OKX_FACTOR_BACKTEST_FEE_RATE", "0.0010")
    factor_backtest_slippage_rate: Decimal = _env_decimal("OKX_FACTOR_BACKTEST_SLIPPAGE_RATE", "0.0005")
    factor_backtest_output_dir: str = os.getenv("OKX_FACTOR_BACKTEST_OUTPUT_DIR", "reports/backtests")
    factor_walk_forward_output_dir: str = os.getenv("OKX_FACTOR_WALK_FORWARD_OUTPUT_DIR", "reports/walk_forward")
    factor_backtest_rebalance_every_bars: int = _env_int("OKX_FACTOR_BACKTEST_REBALANCE_EVERY_BARS", 1)
    factor_benchmark_inst_id: str = os.getenv("OKX_FACTOR_BENCHMARK_INST_ID", "BTC-USDT")
    factor_guardian_output_dir: str = os.getenv("OKX_FACTOR_GUARDIAN_OUTPUT_DIR", "reports/guardian")
    factor_guardian_state_path: str = os.getenv("OKX_FACTOR_GUARDIAN_STATE_PATH", "state/factor_guardian_state.json")
    http_max_retries: int = _env_int("OKX_HTTP_MAX_RETRIES", 3)
    http_retry_backoff_sec: int = _env_int("OKX_HTTP_RETRY_BACKOFF_SEC", 1)
    log_path: str = os.getenv("OKX_LOG_PATH", "logs/okx_quant.log")
    log_max_bytes: int = _env_int("OKX_LOG_MAX_BYTES", 5_000_000)
    log_backup_count: int = _env_int("OKX_LOG_BACKUP_COUNT", 5)
    alert_webhook_url: str = os.getenv("OKX_ALERT_WEBHOOK_URL", "")
    alert_format: str = os.getenv("OKX_ALERT_FORMAT", "slack")
    alert_timeout_sec: int = _env_int("OKX_ALERT_TIMEOUT_SEC", 10)
    alert_error_every_n: int = _env_int("OKX_ALERT_ERROR_EVERY_N", 3)

    @property
    def base_currency(self) -> str:
        return self.instrument_id.split("-", 1)[0]

    @property
    def quote_currency(self) -> str:
        return self.instrument_id.split("-", 1)[1]

    def require_private_api(self) -> None:
        missing = [
            name
            for name, value in (
                ("OKX_API_KEY", self.api_key),
                ("OKX_API_SECRET", self.api_secret),
                ("OKX_API_PASSPHRASE", self.api_passphrase),
            )
            if not value
        ]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"Missing OKX credentials: {joined}")

    def validate(self) -> None:
        if self.fast_window >= self.slow_window:
            raise ValueError("OKX_FAST_WINDOW must be smaller than OKX_SLOW_WINDOW")
        if self.candles_limit < self.slow_window + 2:
            raise ValueError("OKX_CANDLES_LIMIT is too small for the chosen windows")
        if self.trade_amount_quote <= 0:
            raise ValueError("OKX_TRADE_AMOUNT_QUOTE must be positive")
        if self.max_position_quote <= 0:
            raise ValueError("OKX_MAX_POSITION_QUOTE must be positive")
        if self.factor_max_universe_size <= 0:
            raise ValueError("OKX_FACTOR_MAX_UNIVERSE_SIZE must be positive")
        if self.factor_universe_candidates < self.factor_max_universe_size:
            raise ValueError("OKX_FACTOR_UNIVERSE_CANDIDATES must be at least OKX_FACTOR_MAX_UNIVERSE_SIZE")
        if self.factor_top_n <= 0 or self.factor_top_n > self.factor_max_universe_size:
            raise ValueError("OKX_FACTOR_TOP_N must be between 1 and OKX_FACTOR_MAX_UNIVERSE_SIZE")
        if self.factor_hold_buffer < 0:
            raise ValueError("OKX_FACTOR_HOLD_BUFFER must be non-negative")
        if self.factor_min_last_price < 0:
            raise ValueError("OKX_FACTOR_MIN_LAST_PRICE must be non-negative")
        if self.factor_liquidity_lookback <= 0:
            raise ValueError("OKX_FACTOR_LIQUIDITY_LOOKBACK must be positive")
        if not (Decimal("0") < self.factor_capital_fraction <= Decimal("1")):
            raise ValueError("OKX_FACTOR_CAPITAL_FRACTION must be in (0, 1]")
        if not (Decimal("0") < self.factor_max_asset_weight <= Decimal("1")):
            raise ValueError("OKX_FACTOR_MAX_ASSET_WEIGHT must be in (0, 1]")
        if not (Decimal("0") < self.factor_max_turnover_per_rebalance <= Decimal("2")):
            raise ValueError("OKX_FACTOR_MAX_TURNOVER_PER_REBALANCE must be in (0, 2]")
        if not (Decimal("0") <= self.factor_target_annual_vol <= Decimal("3")):
            raise ValueError("OKX_FACTOR_TARGET_ANNUAL_VOL must be in [0, 3]")
        if not (Decimal("0") <= self.factor_min_gross_exposure <= Decimal("1")):
            raise ValueError("OKX_FACTOR_MIN_GROSS_EXPOSURE must be in [0, 1]")
        if not (Decimal("0") < self.factor_max_gross_exposure <= Decimal("1")):
            raise ValueError("OKX_FACTOR_MAX_GROSS_EXPOSURE must be in (0, 1]")
        if self.factor_min_gross_exposure > self.factor_max_gross_exposure:
            raise ValueError("OKX_FACTOR_MIN_GROSS_EXPOSURE must be <= OKX_FACTOR_MAX_GROSS_EXPOSURE")
        if self.factor_market_state_order_book_levels <= 0:
            raise ValueError("OKX_FACTOR_MARKET_STATE_ORDER_BOOK_LEVELS must be positive")
        if not (Decimal("0") <= self.factor_market_state_min_breadth <= Decimal("1")):
            raise ValueError("OKX_FACTOR_MARKET_STATE_MIN_BREADTH must be in [0, 1]")
        if not (Decimal("0") <= self.factor_market_state_max_momentum_dispersion <= Decimal("5")):
            raise ValueError("OKX_FACTOR_MARKET_STATE_MAX_MOMENTUM_DISPERSION must be in [0, 5]")
        if not (Decimal("0") <= self.factor_market_state_min_volume_ratio <= Decimal("10")):
            raise ValueError("OKX_FACTOR_MARKET_STATE_MIN_VOLUME_RATIO must be in [0, 10]")
        if self.factor_market_state_max_spread_bps < 0:
            raise ValueError("OKX_FACTOR_MARKET_STATE_MAX_SPREAD_BPS must be non-negative")
        if self.factor_market_state_min_depth_quote < 0:
            raise ValueError("OKX_FACTOR_MARKET_STATE_MIN_DEPTH_QUOTE must be non-negative")
        if not (Decimal("0") <= self.factor_market_state_max_abs_funding_rate < Decimal("1")):
            raise ValueError("OKX_FACTOR_MARKET_STATE_MAX_ABS_FUNDING_RATE must be in [0, 1)")
        if self.factor_market_state_min_open_interest_usd < 0:
            raise ValueError("OKX_FACTOR_MARKET_STATE_MIN_OPEN_INTEREST_USD must be non-negative")
        if not (Decimal("0") <= self.factor_market_state_min_exposure <= Decimal("1")):
            raise ValueError("OKX_FACTOR_MARKET_STATE_MIN_EXPOSURE must be in [0, 1]")
        if not (Decimal("0") <= self.factor_market_state_entry_gate <= Decimal("1")):
            raise ValueError("OKX_FACTOR_MARKET_STATE_ENTRY_GATE must be in [0, 1]")
        last_threshold = Decimal("0")
        for threshold, scale in self.factor_drawdown_scale_tiers:
            if threshold < last_threshold:
                raise ValueError("OKX_FACTOR_DRAWDOWN_SCALE_TIERS thresholds must be ascending")
            if not (Decimal("0") <= threshold < Decimal("1")):
                raise ValueError("OKX_FACTOR_DRAWDOWN_SCALE_TIERS thresholds must be in [0, 1)")
            if not (Decimal("0") <= scale <= Decimal("1")):
                raise ValueError("OKX_FACTOR_DRAWDOWN_SCALE_TIERS scales must be in [0, 1]")
            last_threshold = threshold
        if self.factor_short_lookback >= self.factor_medium_lookback:
            raise ValueError("OKX_FACTOR_SHORT_LOOKBACK must be smaller than OKX_FACTOR_MEDIUM_LOOKBACK")
        if self.factor_medium_lookback >= self.factor_long_lookback:
            raise ValueError("OKX_FACTOR_MEDIUM_LOOKBACK must be smaller than OKX_FACTOR_LONG_LOOKBACK")
        if self.factor_fast_ma >= self.factor_slow_ma:
            raise ValueError("OKX_FACTOR_FAST_MA must be smaller than OKX_FACTOR_SLOW_MA")
        if self.factor_regime_fast_ma >= self.factor_regime_slow_ma:
            raise ValueError("OKX_FACTOR_REGIME_FAST_MA must be smaller than OKX_FACTOR_REGIME_SLOW_MA")
        if self.factor_regime_required_signals <= 0 or self.factor_regime_required_signals > 4:
            raise ValueError("OKX_FACTOR_REGIME_REQUIRED_SIGNALS must be between 1 and 4")
        if not (Decimal("0") <= self.factor_regime_breadth_threshold <= Decimal("1")):
            raise ValueError("OKX_FACTOR_REGIME_BREADTH_THRESHOLD must be in [0, 1]")
        if self.factor_dynamic_top_n < self.factor_top_n or self.factor_dynamic_top_n > self.factor_max_universe_size:
            raise ValueError("OKX_FACTOR_DYNAMIC_TOP_N must be between OKX_FACTOR_TOP_N and OKX_FACTOR_MAX_UNIVERSE_SIZE")
        if self.factor_dynamic_top_n_required_signals <= 0 or self.factor_dynamic_top_n_required_signals > 4:
            raise ValueError("OKX_FACTOR_DYNAMIC_TOP_N_REQUIRED_SIGNALS must be between 1 and 4")
        if not (Decimal("0") <= self.factor_dynamic_top_n_breadth_threshold <= Decimal("1")):
            raise ValueError("OKX_FACTOR_DYNAMIC_TOP_N_BREADTH_THRESHOLD must be in [0, 1]")
        if self.factor_rebalance_mode not in {"daily", "weekly", "monthly"}:
            raise ValueError("OKX_FACTOR_REBALANCE_MODE must be daily, weekly, or monthly")
        if self.factor_rebalance_weekday < 0 or self.factor_rebalance_weekday > 6:
            raise ValueError("OKX_FACTOR_REBALANCE_WEEKDAY must be between 0 and 6")
        min_history_required = max(
            self.factor_long_lookback + 2,
            self.factor_slow_ma + 2,
            self.factor_regime_slow_ma + 2,
            self.factor_regime_momentum_lookback + 2,
        )
        if self.factor_min_history < min_history_required:
            raise ValueError("OKX_FACTOR_MIN_HISTORY is too small for the selected lookbacks")
        if not (Decimal("0") <= self.factor_stop_loss_pct < Decimal("1")):
            raise ValueError("OKX_FACTOR_STOP_LOSS_PCT must be in [0, 1)")
        if not (Decimal("0") <= self.factor_trailing_stop_pct < Decimal("1")):
            raise ValueError("OKX_FACTOR_TRAILING_STOP_PCT must be in [0, 1)")
        if not (Decimal("0") < self.factor_max_drawdown_pct < Decimal("1")):
            raise ValueError("OKX_FACTOR_MAX_DRAWDOWN_PCT must be in (0, 1)")
        if self.factor_halt_cooldown_days < 0:
            raise ValueError("OKX_FACTOR_HALT_COOLDOWN_DAYS must be non-negative")
        if self.factor_halt_resume_confirm_bars <= 0:
            raise ValueError("OKX_FACTOR_HALT_RESUME_CONFIRM_BARS must be positive")
        if self.factor_halt_resume_required_signals <= 0 or self.factor_halt_resume_required_signals > 4:
            raise ValueError("OKX_FACTOR_HALT_RESUME_REQUIRED_SIGNALS must be between 1 and 4")
        if self.factor_backtest_initial_capital <= 0:
            raise ValueError("OKX_FACTOR_BACKTEST_INITIAL_CAPITAL must be positive")
        if any(year <= 0 for year in self.factor_backtest_years):
            raise ValueError("OKX_FACTOR_BACKTEST_YEARS must contain positive integers")
        if self.factor_backtest_rebalance_every_bars <= 0:
            raise ValueError("OKX_FACTOR_BACKTEST_REBALANCE_EVERY_BARS must be positive")
        if not (Decimal("0") <= self.factor_backtest_fee_rate < Decimal("1")):
            raise ValueError("OKX_FACTOR_BACKTEST_FEE_RATE must be in [0, 1)")
        if not (Decimal("0") <= self.factor_backtest_slippage_rate < Decimal("1")):
            raise ValueError("OKX_FACTOR_BACKTEST_SLIPPAGE_RATE must be in [0, 1)")
        if self.http_max_retries <= 0:
            raise ValueError("OKX_HTTP_MAX_RETRIES must be positive")
        if self.log_max_bytes <= 0 or self.log_backup_count < 0:
            raise ValueError("Invalid log rotation settings")
        if self.alert_error_every_n <= 0:
            raise ValueError("OKX_ALERT_ERROR_EVERY_N must be positive")
