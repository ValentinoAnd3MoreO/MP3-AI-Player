import logging

import requests

from app.config import Settings
from app.sd_path import parse_sd_path

logger = logging.getLogger(__name__)


class PlayerNotConfiguredError(RuntimeError):
    pass


class DeviceClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def play(self, sd_path: str) -> None:
        if parse_sd_path(sd_path) is None:
            raise ValueError("La ruta SD no es válida para DFPlayer Mini.")
        if not self.settings.player_enabled:
            raise PlayerNotConfiguredError(
                "La reproducción ESP32 está desactivada en la configuración."
            )

        response = requests.post(
            f"{self.settings.esp32_base_url}/api/play",
            json={"sd_path": sd_path},
            headers={"Authorization": f"Bearer {self.settings.esp32_api_token}"},
            timeout=self.settings.ai_timeout_seconds,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("accepted") is not True:
            raise RuntimeError("El ESP32 no confirmó la orden de reproducción.")
        logger.info("El ESP32 aceptó la reproducción de %s", sd_path)
