import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí"}


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    mongo_uri: str
    mongo_db: str
    mongo_collection: str
    ai_model: str
    ai_api_key: str
    ai_base_url: str | None
    ai_timeout_seconds: int
    ai_top_results: int
    web_search_enabled: bool
    web_search_api_key: str
    web_search_max_results: int
    music_dir: Path
    device_enabled: bool
    device_api_token: str
    device_host: str
    device_port: int
    device_public_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        project_dir = Path(__file__).resolve().parents[1]
        music_dir = Path(os.getenv("MUSIC_DIR", str(project_dir / "music"))).expanduser()
        if not music_dir.is_absolute():
            music_dir = project_dir / music_dir
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            mongo_uri=os.getenv("MONGO_URI", "mongodb://localhost:27017"),
            mongo_db=os.getenv("MONGO_DB", "mp3_ai_player"),
            mongo_collection=os.getenv("MONGO_COLLECTION", "songs"),
            ai_model=os.getenv("AI_MODEL", "gpt-4o-mini"),
            ai_api_key=os.getenv("AI_API_KEY", ""),
            ai_base_url=os.getenv("AI_BASE_URL") or None,
            ai_timeout_seconds=int(os.getenv("AI_TIMEOUT_SECONDS", "30")),
            ai_top_results=max(1, min(int(os.getenv("AI_TOP_RESULTS", "5")), 10)),
            web_search_enabled=_env_bool("WEB_SEARCH_ENABLED"),
            web_search_api_key=os.getenv("WEB_SEARCH_API_KEY", ""),
            web_search_max_results=max(
                1, min(int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5")), 10)
            ),
            music_dir=music_dir,
            device_enabled=_env_bool("DEVICE_ENABLED"),
            device_api_token=os.getenv("DEVICE_API_TOKEN", ""),
            device_host=os.getenv("DEVICE_HOST", "0.0.0.0"),
            device_port=int(os.getenv("DEVICE_PORT", "8765")),
            device_public_url=os.getenv("DEVICE_PUBLIC_URL", "").rstrip("/"),
        )

    def validate(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.ai_api_key:
            missing.append("AI_API_KEY")
        if self.web_search_enabled and not self.web_search_api_key:
            missing.append("WEB_SEARCH_API_KEY (requerida con WEB_SEARCH_ENABLED=true)")
        if self.device_enabled:
            if not self.device_api_token:
                missing.append("DEVICE_API_TOKEN (requerida con DEVICE_ENABLED=true)")
            if not self.device_public_url:
                missing.append(
                    "DEVICE_PUBLIC_URL (IP local del equipo en la red Wi-Fi)"
                )
            elif not self.device_public_url.startswith("http://"):
                missing.append(
                    "DEVICE_PUBLIC_URL debe empezar con http:// para el firmware MicroPython"
                )
            if not 1 <= self.device_port <= 65535:
                missing.append("DEVICE_PORT debe estar entre 1 y 65535")
        if missing:
            raise ValueError("Faltan variables de entorno: " + ", ".join(missing))
