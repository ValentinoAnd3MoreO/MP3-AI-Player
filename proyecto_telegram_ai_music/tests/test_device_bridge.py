import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.device_bridge import DeviceBridge
from app.music_library import MusicLibrary
from scripts.index_music import index_music


class FakeDatabase:
    def __init__(self, song):
        self.song = song

    def find_song_by_id(self, song_id):
        if str(self.song["_id"]) == song_id:
            return self.song
        return None


class DeviceBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        audio_file = self.root / "purple-rain.mp3"
        audio_file.write_bytes(b"fake-mp3-audio")
        self.song = {
            "_id": "song-1",
            "title": "Purple Rain",
            "artist": "Prince",
            "path": "purple-rain.mp3",
        }
        settings = replace(
            Settings.from_env(),
            music_dir=self.root,
            device_enabled=True,
            device_api_token="test-device-token",
            device_public_url="http://127.0.0.1:8765",
        )
        self.database = FakeDatabase(self.song)
        self.music = MusicLibrary(settings)
        self.bridge = DeviceBridge(settings, self.database, self.music)
        self.client = TestClient(TestServer(self.bridge.web_application))
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer test-device-token"}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_device_can_poll_ack_and_stream_queued_song(self):
        await self.bridge.enqueue(self.song)

        response = await self.client.get(
            "/api/device/command", headers=self.headers
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        command = payload["command"]
        self.assertEqual(command["song_id"], "song-1")
        self.assertEqual(command["title"], "Purple Rain")

        response = await self.client.get(
            "/api/music/song-1", headers=self.headers
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"fake-mp3-audio")

        response = await self.client.post(
            f"/api/device/ack/{command['command_id']}", headers=self.headers
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["acknowledged"], True)

        response = await self.client.get(
            "/api/device/command", headers=self.headers
        )
        self.assertIsNone((await response.json())["command"])

    async def test_endpoints_reject_requests_without_device_token(self):
        response = await self.client.get("/api/device/command")
        self.assertEqual(response.status, 401)

    async def test_music_library_rejects_paths_outside_music_folder(self):
        outside = self.root.parent / "outside.mp3"
        self.song["path"] = str(outside)
        self.assertIsNone(self.music.resolve(self.song))


class Mp3IndexTests(unittest.TestCase):
    def test_reads_id3_metadata_and_stores_relative_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "album").mkdir()
            mp3 = root / "album" / "track.mp3"
            mp3.write_bytes(b"mp3-placeholder")

            class FakeCollection:
                def __init__(self):
                    self.documents = []

                def find(self, query, projection):
                    return []

                def update_one(self, query, update, upsert):
                    document = {
                        "_id": query["_id"],
                        **update["$set"],
                        **update.get("$setOnInsert", {}),
                    }
                    self.documents.append(document)

            collection = FakeCollection()
            audio = SimpleNamespace(
                tags={
                    "title": ["Purple Rain"],
                    "artist": ["Prince"],
                    "album": ["Purple Rain"],
                    "date": ["1984-06-25"],
                    "genre": ["Rock"],
                }
            )
            with patch("scripts.index_music.MutagenFile", return_value=audio):
                indexed, skipped = index_music(root, collection)

        self.assertEqual((indexed, skipped), (1, 0))
        self.assertEqual(collection.documents[0]["title"], "Purple Rain")
        self.assertEqual(collection.documents[0]["artist"], "Prince")
        self.assertEqual(collection.documents[0]["year"], 1984)
        self.assertEqual(collection.documents[0]["path"], "album/track.mp3")


if __name__ == "__main__":
    unittest.main()
