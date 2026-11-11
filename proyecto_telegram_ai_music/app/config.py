import os
from dataclasses import dataclass

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
    player_enabled: bool
    esp32_base_url: str
    esp32_api_token: str
    ai_transcription_model: str = "whisper-1"

    @classmethod
    def from_env(cls) -> "Settings":
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
            player_enabled=_env_bool("PLAYER_ENABLED"),
            esp32_base_url=os.getenv("ESP32_BASE_URL", "").rstrip("/"),
            esp32_api_token=os.getenv("ESP32_API_TOKEN", ""),
            ai_transcription_model=(
                os.getenv("AI_TRANSCRIPTION_MODEL", "").strip() or "whisper-1"
            ),
        )

    def validate(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.ai_api_key:
            missing.append("AI_API_KEY")
        if self.web_search_enabled and not self.web_search_api_key:
            missing.append("WEB_SEARCH_API_KEY (requerida con WEB_SEARCH_ENABLED=true)")
        if self.player_enabled:
            if not self.esp32_base_url:
                missing.append("ESP32_BASE_URL (requerida con PLAYER_ENABLED=true)")
            if not self.esp32_api_token:
                missing.append("ESP32_API_TOKEN (requerida con PLAYER_ENABLED=true)")
            elif len(self.esp32_api_token) < 24:
                missing.append("ESP32_API_TOKEN (usa al menos 24 caracteres)")
            if self.esp32_base_url and not self.esp32_base_url.startswith(
                ("http://", "https://")
            ):
                missing.append("ESP32_BASE_URL (debe comenzar con http:// o https://)")
        if missing:
            raise ValueError("Faltan variables de entorno: " + ", ".join(missing))
