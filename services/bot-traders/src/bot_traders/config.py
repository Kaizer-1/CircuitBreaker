"""Configuration from environment variables (see .env.example)."""

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from bot_traders.runtime.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    # `make bots` passes BOT_RANDOM_SEED= when no seed is given: empty means "not set".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    service_name: str = "bot-traders"

    kafka_bootstrap_servers: str = "localhost:19092"

    # The public API, exactly as humans use it. Inside Compose: http://api-gateway:8000
    gateway_url: str = "http://localhost:8080"
    bot_secret: SecretStr  # X-Bot-Secret for A-03; no default, a secret never lives in code

    bots_enabled: bool = (
        False  # off by default: `make bots` turns them on (and `make bots-stop` off)
    )
    bot_random_seed: int | None = None  # set it for repeatable rehearsals
    bot_activity: float = 1.0  # scales how often and how much every bot trades
    candle_interval_seconds: float = 5.0  # the 5s candles drive decisions
    start_consumers: bool = True  # False in unit tests: no Kafka or gateway at start-up

    # A-23/A-24, chaos-service's panic-sell hook: on in staging and the demo prod only; off (404)
    # in any real deployment (chaos scenarios section 3.3, ADR-0020).
    chaos_hooks_enabled: bool = False
