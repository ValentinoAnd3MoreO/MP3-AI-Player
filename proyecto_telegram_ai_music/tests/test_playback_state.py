import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from app.bot import _restore_application_state, _stop_background_tasks
from app.playback_queue import PlaybackQueue
from app.playback_state import PlaybackStateStore


class PlaybackStateStoreTests(unittest.IsolatedAsyncioTestCase):
    def test_saves_queue_snapshot_without_played_tracks(self):
        collection = Mock()
        store = PlaybackStateStore(collection)
        queue = PlaybackQueue()
        songs = [
            {"_id": "played"},
            {"_id": "current"},
            {"_id": "next"},
        ]
        queue.enqueue(songs)
        queue.next()
        started_at = datetime(2026, 11, 11, tzinfo=timezone.utc)

        store.save_queue(
            queue.songs,
            "all",
            started_at,
            True,
            user_id=42,
            chat_id=100,
        )

        collection.update_one.assert_called_once_with(
            {"_id": "telegram_playback"},
            {
                "$set": {
                    "song_ids": ["current", "next"],
                    "repeat_mode": "all",
                    "started_at": started_at,
                    "paused": True,
                    "user_id": 42,
                    "chat_id": 100,
                }
            },
            upsert=True,
        )

    def test_saves_per_user_autoplay_without_overwriting_queue(self):
        collection = Mock()
        store = PlaybackStateStore(collection)

        store.save_autoplay(42, True)

        collection.update_one.assert_called_once_with(
            {"_id": "telegram_playback"},
            {"$set": {"autoplay_users.42": True}},
            upsert=True,
        )

    def test_queue_restore_rejects_unknown_repeat_mode(self):
        queue = PlaybackQueue()

        with self.assertRaisesRegex(ValueError, "Modo de repetición"):
            queue.restore([{"_id": "track"}], "invalid")

    async def test_startup_restores_queue_mode_and_user_autoplay_preferences(self):
        songs = [
            {"_id": "current", "title": "Current", "sd_path": "/01/001.mp3"},
            {"_id": "next", "title": "Next", "sd_path": "/01/002.mp3"},
        ]
        state = {
            "song_ids": ["current", "next"],
            "repeat_mode": "all",
            "autoplay_users": {"42": True},
            "user_id": 42,
            "chat_id": 100,
        }
        bot_data = {
            "playback_state": SimpleNamespace(load=Mock(return_value=state)),
            "database": SimpleNamespace(
                find_songs_by_ids=Mock(return_value=songs)
            ),
            "playback_queue": PlaybackQueue(),
            "player_settings": SimpleNamespace(player_enabled=False),
            "autoplay_users": {},
        }
        application = SimpleNamespace(
            bot_data=bot_data,
            user_data={},
            bot=Mock(),
        )

        await _restore_application_state(application)

        self.assertEqual(bot_data["playback_queue"].songs, songs)
        self.assertEqual(bot_data["playback_queue"].repeat_mode, "all")
        self.assertEqual(bot_data["autoplay_users"], {42: True})
        await _stop_background_tasks(application)
