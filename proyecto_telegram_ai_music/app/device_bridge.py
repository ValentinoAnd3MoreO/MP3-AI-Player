import asyncio
import hmac
import logging
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from app.config import Settings
from app.database import SongDatabase
from app.music_library import MusicLibrary

logger = logging.getLogger(__name__)


class DeviceBridge:
    def __init__(
        self,
        settings: Settings,
        database: SongDatabase,
        music: MusicLibrary,
    ):
        self.settings = settings
        self.database = database
        self.music = music
        self._commands: deque[dict] = deque()
        self._lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None

        application = web.Application()
        application.add_routes(
            [
                web.get("/api/device/command", self.get_command),
                web.post("/api/device/ack/{command_id}", self.ack_command),
                web.get("/api/music/{song_id}", self.stream_song),
            ]
        )
        self.web_application = application

    async def start(self, _: object) -> None:
        if not self.settings.device_enabled:
            logger.info("Puente ESP32 desactivado; configura DEVICE_ENABLED=true para activarlo.")
            return
        if not self.settings.device_api_token or not self.settings.device_public_url:
            raise RuntimeError(
                "DEVICE_API_TOKEN y DEVICE_PUBLIC_URL son obligatorios al activar DEVICE_ENABLED."
            )

        self._runner = web.AppRunner(self.web_application)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, self.settings.device_host, self.settings.device_port
        )
        await site.start()
        logger.info(
            "Puente ESP32 escuchando en %s:%s",
            self.settings.device_host,
            self.settings.device_port,
        )

    async def stop(self, _: object) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def has_audio(self, song: dict) -> bool:
        return self.music.has_audio(song)

    async def enqueue(self, song: dict) -> None:
        if not self.settings.device_enabled:
            raise RuntimeError("La reproducción ESP32 está desactivada.")
        file_path = self.music.resolve(song)
        if file_path is None:
            raise FileNotFoundError(
                "No hay un MP3 local indexado para esta canción."
            )

        song_id = str(song["_id"])
        command = {
            "command_id": uuid.uuid4().hex,
            "song_id": song_id,
            "title": str(song.get("title") or "Sin título"),
            "artist": str(song.get("artist") or "Artista desconocido"),
            "stream_url": (
                f"{self.settings.device_public_url}/api/music/{quote(song_id, safe='')}"
            ),
        }
        async with self._lock:
            self._commands.append(command)

    def _authorize(self, request: web.Request) -> None:
        expected = f"Bearer {self.settings.device_api_token}"
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, expected):
            raise web.HTTPUnauthorized(text="Unauthorized")

    async def get_command(self, request: web.Request) -> web.Response:
        self._authorize(request)
        async with self._lock:
            command = dict(self._commands[0]) if self._commands else None
        return web.json_response({"command": command})

    async def ack_command(
        self, request: web.Request
    ) -> web.Response:
        self._authorize(request)
        command_id = request.match_info["command_id"]
        async with self._lock:
            if not self._commands or self._commands[0]["command_id"] != command_id:
                raise web.HTTPConflict(text="Command is no longer pending")
            self._commands.popleft()
        return web.json_response({"acknowledged": True})

    async def stream_song(self, request: web.Request) -> web.FileResponse:
        self._authorize(request)
        song = self.database.find_song_by_id(request.match_info["song_id"])
        if not song:
            raise web.HTTPNotFound(text="Song not found")
        file_path: Path | None = self.music.resolve(song)
        if file_path is None:
            raise web.HTTPNotFound(text="Local MP3 not found")
        return web.FileResponse(
            file_path,
            headers={"Content-Type": "audio/mpeg"},
        )
