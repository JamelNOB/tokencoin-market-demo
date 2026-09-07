"""Runtime configuration for the TokenCoin gateway.

The gateway reads the repository-root ``.env`` file without mutating the
process environment. Real environment variables always take precedence.
Secret-bearing fields are excluded from dataclass representations.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = PROJECT_ROOT / ".env"


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read the small dotenv subset used by this project.

    Environment loading stays local to this module so importing the app never
    overwrites variables supplied by the shell, container, or hosting service.
    """

    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue

        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]

        values[name] = value
    return values


def _number(
    raw: str | None,
    default: float,
    *,
    minimum: float,
) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return max(float(raw), minimum)
    except ValueError:
        return default


def _integer(
    raw: str | None,
    default: int,
    *,
    minimum: int,
) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        return default


def _csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    app_name: str = "TokenCoin Gateway"
    version: str = "0.2.0"
    environment: str = "local"

    deepseek_api_key: str | None = field(default=None, repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_models: tuple[str, ...] = ("deepseek-v4-flash", "deepseek-v4-pro")
    additional_supplies_json: str | None = field(default=None, repr=False)
    buyer_api_key: str = field(default="tc_local_demo", repr=False)
    admin_api_key: str | None = field(default=None, repr=False)
    buyer_id: str = "local-buyer"
    database_path: Path = PROJECT_ROOT / "backend" / "data" / "tokencoin.db"
    buyer_starting_micro_cny: int = 10_000_000
    request_reserve_micro_cny: int = 100_000
    reservation_lease_seconds: float = 300.0

    buyer_input_cny_per_million: float = 2.40
    buyer_output_cny_per_million: float = 7.20
    buyer_cache_hit_cny_per_million: float = 0.08
    seller_input_cny_per_million: float = 2.22
    seller_output_cny_per_million: float = 6.66
    seller_cache_hit_cny_per_million: float = 0.074

    probe_interval_seconds: float = 900.0
    scheduled_probes_enabled: bool = True
    probe_on_startup: bool = True
    probe_model: str = "deepseek-v4-flash"
    manual_probe_cooldown_seconds: float = 30.0

    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 120.0
    first_byte_timeout_seconds: float = 20.0
    write_timeout_seconds: float = 30.0
    pool_timeout_seconds: float = 5.0
    max_connections: int = 100
    max_keepalive_connections: int = 20

    circuit_failure_threshold: int = 2
    circuit_cooldown_seconds: float = 30.0
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    dotenv = _read_dotenv(DOTENV_PATH)

    def env(name: str, default: str | None = None) -> str | None:
        return os.environ.get(name, dotenv.get(name, default))

    def boolean(name: str, default: bool) -> bool:
        raw = env(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    api_key = (env("DEEPSEEK_API_KEY") or "").strip() or None
    base_url = (env("DEEPSEEK_BASE_URL", "https://api.deepseek.com") or "").strip()
    if not base_url:
        base_url = "https://api.deepseek.com"

    environment = (env("TOKENCOIN_ENVIRONMENT", "local") or "local").strip()
    buyer_api_key = (env("TOKENCOIN_BUYER_API_KEY") or "tc_local_demo").strip()
    if environment.lower() != "local" and buyer_api_key == "tc_local_demo":
        raise ValueError(
            "TOKENCOIN_BUYER_API_KEY must be set outside the local environment"
        )

    return Settings(
        environment=environment,
        deepseek_api_key=api_key,
        deepseek_base_url=base_url.rstrip("/"),
        deepseek_models=_csv(
            env("DEEPSEEK_MODELS"),
            ("deepseek-v4-flash", "deepseek-v4-pro"),
        ),
        additional_supplies_json=(
            (env("TOKENCOIN_SUPPLIES_JSON") or "").strip() or None
        ),
        buyer_api_key=buyer_api_key,
        admin_api_key=(env("TOKENCOIN_ADMIN_API_KEY") or "").strip() or None,
        buyer_id=(env("TOKENCOIN_BUYER_ID") or "local-buyer").strip(),
        database_path=Path(
            env(
                "TOKENCOIN_DATABASE_PATH",
                str(PROJECT_ROOT / "backend" / "data" / "tokencoin.db"),
            )
            or str(PROJECT_ROOT / "backend" / "data" / "tokencoin.db")
        ),
        buyer_starting_micro_cny=_integer(
            env("TOKENCOIN_BUYER_STARTING_MICRO_CNY"), 10_000_000, minimum=1
        ),
        request_reserve_micro_cny=_integer(
            env("TOKENCOIN_REQUEST_RESERVE_MICRO_CNY"), 100_000, minimum=1
        ),
        reservation_lease_seconds=_number(
            env("TOKENCOIN_RESERVATION_LEASE_SECONDS"), 300.0, minimum=30.0
        ),
        buyer_input_cny_per_million=_number(
            env("TOKENCOIN_BUYER_INPUT_CNY_PER_MILLION"), 2.40, minimum=0
        ),
        buyer_output_cny_per_million=_number(
            env("TOKENCOIN_BUYER_OUTPUT_CNY_PER_MILLION"), 7.20, minimum=0
        ),
        buyer_cache_hit_cny_per_million=_number(
            env("TOKENCOIN_BUYER_CACHE_HIT_CNY_PER_MILLION"), 0.08, minimum=0
        ),
        seller_input_cny_per_million=_number(
            env("TOKENCOIN_SELLER_INPUT_CNY_PER_MILLION"), 2.22, minimum=0
        ),
        seller_output_cny_per_million=_number(
            env("TOKENCOIN_SELLER_OUTPUT_CNY_PER_MILLION"), 6.66, minimum=0
        ),
        seller_cache_hit_cny_per_million=_number(
            env("TOKENCOIN_SELLER_CACHE_HIT_CNY_PER_MILLION"), 0.074, minimum=0
        ),
        probe_interval_seconds=_number(
            env("TOKENCOIN_PROBE_INTERVAL_SECONDS"), 900.0, minimum=10.0
        ),
        scheduled_probes_enabled=boolean("TOKENCOIN_SCHEDULED_PROBES_ENABLED", True),
        probe_on_startup=boolean("TOKENCOIN_PROBE_ON_STARTUP", True),
        probe_model=(
            env("TOKENCOIN_PROBE_MODEL", "deepseek-v4-flash")
            or "deepseek-v4-flash"
        ).strip(),
        manual_probe_cooldown_seconds=_number(
            env("TOKENCOIN_MANUAL_PROBE_COOLDOWN_SECONDS"), 30.0, minimum=1.0
        ),
        connect_timeout_seconds=_number(
            env("TOKENCOIN_CONNECT_TIMEOUT_SECONDS"), 5.0, minimum=0.1
        ),
        read_timeout_seconds=_number(
            env("TOKENCOIN_READ_TIMEOUT_SECONDS"), 120.0, minimum=1.0
        ),
        first_byte_timeout_seconds=_number(
            env("TOKENCOIN_FIRST_BYTE_TIMEOUT_SECONDS"), 20.0, minimum=0.1
        ),
        write_timeout_seconds=_number(
            env("TOKENCOIN_WRITE_TIMEOUT_SECONDS"), 30.0, minimum=1.0
        ),
        pool_timeout_seconds=_number(
            env("TOKENCOIN_POOL_TIMEOUT_SECONDS"), 5.0, minimum=0.1
        ),
        max_connections=_integer(
            env("TOKENCOIN_MAX_CONNECTIONS"), 100, minimum=1
        ),
        max_keepalive_connections=_integer(
            env("TOKENCOIN_MAX_KEEPALIVE_CONNECTIONS"), 20, minimum=0
        ),
        circuit_failure_threshold=_integer(
            env("TOKENCOIN_CIRCUIT_FAILURE_THRESHOLD"), 2, minimum=1
        ),
        circuit_cooldown_seconds=_number(
            env("TOKENCOIN_CIRCUIT_COOLDOWN_SECONDS"), 30.0, minimum=1.0
        ),
        cors_origins=_csv(
            env("TOKENCOIN_CORS_ORIGINS"),
            ("http://127.0.0.1:5173", "http://localhost:5173"),
        ),
    )
