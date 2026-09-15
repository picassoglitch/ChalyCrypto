from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FeeRow(BaseModel):
    """Single seed row for fee_schedules."""

    exchange: str
    vip_level: str = "regular"
    maker_bps: Decimal
    taker_bps: Decimal


class Settings(BaseSettings):
    """Typed app settings. Loaded from env (.env in dev, real env in prod).

    Env names are bare (no CHALYBCRYPTO_ prefix) to match .env.example, which is the seed
    source for all services. Extra vars from later phases are ignored here.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # App
    app_env: str = "local"
    app_default_locale: str = "es"
    log_level: str = "info"

    # Redis / Celery
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    # Supabase (used in Phase 1+)
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_role_key: str | None = None
    supabase_db_schema: str = "chalybcrypto"
    supabase_jwt_secret: str | None = None

    # Secrets envelope-encryption master key (Phase 1+).
    master_encryption_key: str | None = Field(
        default=None,
        description="32-byte base64. Sourced from KMS in prod. Never in DB plaintext.",
    )

    # Fee seeds — keyed bare to match .env.example. The runtime source of truth is the
    # fee_schedules table; these only seed it on bootstrap (CLAUDE.md rule 6).
    fee_binance_maker_bps: Decimal = Decimal("2.0")
    fee_binance_taker_bps: Decimal = Decimal("5.0")
    fee_lbank_maker_bps: Decimal = Decimal("2.0")
    fee_lbank_taker_bps: Decimal = Decimal("6.0")
    fee_bitunix_maker_bps: Decimal = Decimal("2.0")
    fee_bitunix_taker_bps: Decimal = Decimal("6.0")

    def fee_schedule_seed(self) -> list[FeeRow]:
        return [
            FeeRow(
                exchange="binance",
                vip_level="regular",
                maker_bps=self.fee_binance_maker_bps,
                taker_bps=self.fee_binance_taker_bps,
            ),
            FeeRow(
                exchange="lbank",
                vip_level="regular",
                maker_bps=self.fee_lbank_maker_bps,
                taker_bps=self.fee_lbank_taker_bps,
            ),
            FeeRow(
                exchange="bitunix",
                vip_level="VIP0",
                maker_bps=self.fee_bitunix_maker_bps,
                taker_bps=self.fee_bitunix_taker_bps,
            ),
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
