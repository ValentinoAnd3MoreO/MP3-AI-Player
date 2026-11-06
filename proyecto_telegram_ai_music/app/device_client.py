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

    def _send_command(self, command: str, payload: dict | None = None) -> None:
        if not self.settings.player_enabled:
            raise PlayerNotConfiguredError(
                "La reproducción ESP32 está desactivada en la configuración."
            )

        response = requests.post(
            f"{self.settings.esp32_base_url}/api/{command}",
            json=payload or {},
            headers={"Authorization": f"Bearer {self.settings.esp32_api_token}"},
            timeout=self.settings.ai_timeout_seconds,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("accepted") is not True:
            raise RuntimeError(f"El ESP32 no confirmó la orden {command}.")
        logger.info("El ESP32 aceptó la orden %s.", command)

    def play(self, sd_path: str) -> None:
        if parse_sd_path(sd_path) is None:
            raise ValueError("La ruta SD no es válida para DFPlayer Mini.")
        self._send_command("play", {"sd_path": sd_path})

    def pause(self) -> None:
        self._send_command("pause")

    def resume(self) -> None:
        self._send_command("resume")
