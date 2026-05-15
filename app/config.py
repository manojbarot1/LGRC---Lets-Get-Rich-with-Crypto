from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"

    # Simulator
    starting_capital: float = Field(1000.0, description="Starting USD for the simulation")
    target_weekly_pct: float = Field(25.0, description="Target weekly return %")

    # Two-speed scheduler
    fast_interval_seconds: int = Field(60, description="Price check + stop-loss cycle (seconds)")
    claude_interval_seconds: int = Field(300, description="AI analysis + trade execution cycle (seconds)")

    # Risk management
    max_positions: int = Field(3, description="Max simultaneous open positions")
    max_position_pct: float = Field(0.40, description="Max % of portfolio per position")
    stop_loss_pct: float = Field(0.05, description="Auto stop-loss % below avg cost")
    take_profit_pct: float = Field(0.12, description="Auto take-profit % above avg cost")
    min_cash_reserve_pct: float = Field(0.10, description="Minimum cash reserve %")

    # DB
    database_url: str = "sqlite+aiosqlite:///./data/sim.db"

    # Server
    host: str = "0.0.0.0"
    port: int = 8100


@lru_cache
def get_settings() -> Settings:
    return Settings()
