"""Settings, all read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Read KEY=value lines from .env; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Claude
    model: str = field(default_factory=lambda: _env("AGENT_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: _env("AGENT_EFFORT", "low"))

    # WhatsApp Cloud API (Meta)
    wa_token: str = field(default_factory=lambda: _env("WHATSAPP_TOKEN"))
    wa_phone_number_id: str = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    wa_verify_token: str = field(default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN"))
    wa_app_secret: str = field(default_factory=lambda: _env("WHATSAPP_APP_SECRET"))
    graph_api_version: str = field(default_factory=lambda: _env("GRAPH_API_VERSION", "v23.0"))

    # Staff dashboard
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN"))

    # Data
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", str(BASE_DIR / "data" / "agent.db"))))
    units_path: Path = field(
        default_factory=lambda: Path(_env("UNITS_PATH", str(BASE_DIR / "data" / "units.json")))
    )
    policies_path: Path = field(
        default_factory=lambda: Path(_env("POLICIES_PATH", str(BASE_DIR / "knowledge" / "policies.md")))
    )

    # A customer's conversation starts fresh after this many hours of silence.
    session_gap_hours: int = field(default_factory=lambda: int(_env("SESSION_GAP_HOURS", "24")))


def get_settings() -> Settings:
    return Settings()
