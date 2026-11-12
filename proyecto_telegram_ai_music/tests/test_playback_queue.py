import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import requests

from app.ai_service import AIService
from app.bot import (
    _ALBUM_PLAY_REQUEST,
    _queue_keep_label,
    _song_matches_queue_label,
    _advance_playlist,
    album_play_request,
    clear_upcoming_queue_request,
    keep_queue_matching_request,
    jump_to_queued_song,
    play_album_suggestion_callback,
    process_text_message,
    stop_playback_request,
    voice_message,
)
from app.database import SongDatabase
from app.playback_queue import PlaybackQueue
from app.playback_state import PlaybackStateStore


class RecommendationQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.songs = [
            {
                "_id": str(index),
                "title": f"Song {index}",
                "artist": "Charly García",
                "sd_path": f"/01/{index:03d}.mp3",
            }
            for index in range(1, 11)
        ]
        self.queue = PlaybackQueue()
        self.device = Mock()
        self.collection = Mock()
        self.user_data = {"autoplay_enabled": True}
        self.message = SimpleNamespace(
            chat=SimpleNamespace(send_action=AsyncMock()),
            chat_id=100,
            reply_text=AsyncMock(),
        )
        self.update = SimpleNamespace(
            message=self.message,
            effective_message=self.message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100),
        )
        self.context = SimpleNamespace(
            application=SimpleNamespace(
                user_data={42: self.user_data},
                bot_data={
                    "recommender": SimpleNamespace(
                        recommend=Mock(return_value=(self.songs, False))
                    ),
                    "database": SimpleNamespace(
                        find_songs_by_ids=Mock(return_value=self.songs)
                    ),
                    "playback_queue": self.queue,
                    "playback_state": PlaybackStateStore(self.collection),
                    "playback_history": Mock(),
                    "player_settings": SimpleNamespace(player_enabled=True),
                    "device": self.device,
                },
            ),
            bot=SimpleNamespace(send_message=AsyncMock()),
            user_data=self.user_data,
        )
        self.request = (
            "Ahora quiero escuchar canciones de Charlie García, poneme 10 canciones "
            "de Charlie García en la cola de reproducción."
        )

    async def test_voice_request_enqueues_all_ten_and_skip_plays_second(self):
        self.message.voice = SimpleNamespace(file_id="voice", file_size=100)
        self.context.bot.get_file = AsyncMock(
            return_value=SimpleNamespace(
                download_as_bytearray=AsyncMock(return_value=bytearray(b"ogg"))
            )
        )
        self.context.application.bot_data["ai"] = SimpleNamespace(
            transcribe_audio=Mock(return_value=self.request)
        )

        await voice_message(self.update, self.context)

        self.assertEqual(self.queue.songs, self.songs)
        self.assertEqual(len(self.queue.upcoming), 9)
        self.device.play.assert_called_once_with(self.songs[0]["sd_path"])
        saved = self.collection.update_one.call_args.args[1]["$set"]["song_ids"]
        self.assertEqual(saved, [song["_id"] for song in self.songs])

        await _advance_playlist(self.context, chat_id=100, user_id=42)

        self.assertEqual(self.queue.current, self.songs[1])
        self.device.play.assert_called_with(self.songs[1]["sd_path"])

    async def test_queue_request_appends_without_interrupting_existing_song(self):
        current = {"_id": "current", "sd_path": "/02/001.mp3"}
        future = {"_id": "future", "sd_path": "/02/002.mp3"}
        self.queue.enqueue([current, future])

        await process_text_message(self.update, self.context, self.request)

        self.assertIs(self.queue.current, current)
        self.assertEqual(self.queue.upcoming, [future] + self.songs)
        self.device.play.assert_not_called()
        self.assertIn("Añadí 10", self.message.reply_text.await_args.args[0])

    async def test_autoplay_off_offers_whole_selection_and_confirmation_enqueues_it(self):
        self.user_data["autoplay_enabled"] = False
        for _ in range(2):
            await process_text_message(self.update, self.context, self.request)

        self.assertIsNone(self.queue.current)
        self.device.play.assert_not_called()
        keyboard = self.message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(len(keyboard.inline_keyboard), 1)
        button = keyboard.inline_keyboard[0][0]
        token = button.callback_data.partition(":")[2]
        self.assertEqual(
            self.user_data["pending_album"][token], [s["_id"] for s in self.songs]
        )
        self.update.callback_query = SimpleNamespace(
            data=button.callback_data, answer=AsyncMock(), message=self.message
        )

        await play_album_suggestion_callback(self.update, self.context)

        self.assertEqual(self.queue.songs, self.songs)
        self.device.play.assert_called_once_with(self.songs[0]["sd_path"])

    async def test_reports_shortfall_and_only_enqueues_playable_results(self):
        self.songs[0].pop("sd_path")
        self.songs[1]["sd_path"] = "/01/999.mp3"

        await process_text_message(self.update, self.context, self.request)

        self.assertEqual(self.queue.songs, self.songs[2:])
        messages = "\n".join(
            call.args[0] for call in self.message.reply_text.await_args_list
        )
        self.assertIn("8 de las 10", messages)

    async def test_numbered_playback_request_without_queue_word_enqueues_all(self):
        await process_text_message(
            self.update, self.context, "quiero escuchar 10 canciones de Queen"
        )
        self.assertEqual(self.queue.songs, self.songs)

    async def test_recommendation_only_or_disabled_player_does_not_enqueue(self):
        await process_text_message(
            self.update, self.context, "dame 10 canciones de Queen"
        )
        self.assertIsNone(self.queue.current)
        self.context.application.bot_data["player_settings"].player_enabled = False
        await process_text_message(self.update, self.context, self.request)
        self.assertIsNone(self.queue.current)
        self.device.play.assert_not_called()

    async def test_explicit_add_request_works_without_song_or_playback_words(self):
        await process_text_message(
            self.update, self.context, "agregá Queen a la cola"
        )
        self.assertEqual(self.queue.songs, self.songs)

    async def test_negated_add_request_leaves_queue_unchanged(self):
        await process_text_message(
            self.update, self.context, "no agregues canciones de Queen a la cola"
        )
        self.assertIsNone(self.queue.current)
        self.device.play.assert_not_called()
        self.context.application.bot_data["recommender"].recommend.assert_not_called()


class PlaybackQueueTests(unittest.TestCase):
    def setUp(self):
        self.queue = PlaybackQueue()

    def test_enqueue_starts_queue_and_advances_in_order(self):
        songs = [{"title": "First"}, {"title": "Second"}]

        self.assertEqual(self.queue.enqueue(songs), songs[0])
        self.assertEqual(self.queue.current, songs[0])
        self.assertEqual(self.queue.upcoming, [songs[1]])
        self.assertEqual(self.queue.next(), songs[1])
        self.assertIsNone(self.queue.next())
        self.assertIsNone(self.queue.current)

    def test_repeat_all_wraps_and_repeat_one_only_repeats_automatically(self):
        songs = [{"title": "First"}, {"title": "Second"}]
        self.queue.enqueue(songs)
        self.assertEqual(self.queue.cycle_repeat_mode(), "all")
        self.assertEqual(self.queue.next(), songs[1])
        self.assertEqual(self.queue.next(), songs[0])

        self.assertEqual(self.queue.cycle_repeat_mode(), "one")
        self.assertEqual(self.queue.next(manual=False), songs[0])
        self.assertEqual(self.queue.next(manual=True), songs[1])

    def test_enqueue_after_queue_ends_starts_a_fresh_queue(self):
        self.queue.enqueue([{"title": "Finished"}])
        self.assertIsNone(self.queue.next())

        next_song = {"title": "New"}
        self.assertEqual(self.queue.enqueue([next_song]), next_song)

    def test_peek_next_does_not_change_the_current_song(self):
        songs = [{"title": "Current"}, {"title": "Next"}]
        self.queue.enqueue(songs)

        self.assertEqual(self.queue.peek_next(), songs[1])
        self.assertEqual(self.queue.current, songs[0])

    def test_retain_upcoming_keeps_current_and_filters_only_future_tracks(self):
        songs = [
            {"title": "Played", "folder": 1},
            {"title": "Current", "folder": 1},
            {"title": "Persona 1", "folder": 2},
            {"title": "Other", "folder": 3},
            {"title": "Persona 2", "folder": 2},
        ]
        self.queue.enqueue(songs)
        self.queue.next()
        removed, retained = self.queue.retain_upcoming(
            lambda song: song["folder"] == 2
        )

        self.assertEqual((removed, retained), (1, 2))
        self.assertEqual(self.queue.current, songs[1])
        self.assertEqual(self.queue.upcoming, [songs[2], songs[4]])

    def test_find_upcoming_is_accent_insensitive_and_jump_discards_skipped_tracks(self):
        current = {"title": "Current"}
        skipped = {"title": "Interlude"}
        target = {"title": "Canción elegida"}
        following = {"title": "After"}
        self.queue.enqueue([current, skipped, target, following])

        matches = self.queue.find_upcoming("cancion elegida")
        self.assertEqual(matches, [target])
        self.assertEqual(self.queue.current, current)

        self.assertEqual(self.queue.jump_to(target), target)
        self.assertEqual(self.queue.current, target)
        self.assertEqual(self.queue.upcoming, [following])

    def test_queue_keep_label_extracts_arbitrary_artist_album_and_genre(self):
        self.assertEqual(
            _queue_keep_label(
                "borra todas las canciones de la cola menos las de Queen"
            ),
            "Queen",
        )
        self.assertEqual(
            _queue_keep_label(
                "elimina el resto de la cola excepto el álbum Purple Rain"
            ),
            "Purple Rain",
        )
        self.assertEqual(
            _queue_keep_label("deja solo el álbum Purple Rain en la cola"),
            "Purple Rain",
        )
        self.assertEqual(
            _queue_keep_label("limpia la cola salvo las canciones de rock"),
            "rock",
        )
        self.assertIsNone(_queue_keep_label("háblame de Queen"))
        self.assertIsNone(_queue_keep_label("limpia la cola y deja solo"))

    def test_queue_keep_filter_matches_metadata_or_persona_folder(self):
        self.assertTrue(
            _song_matches_queue_label(
                {"artist": "Queen"}, "Queen"
            )
        )
        self.assertTrue(
            _song_matches_queue_label(
                {"album": "Purple Rain"}, "Purple Rain"
            )
        )
        self.assertTrue(
            _song_matches_queue_label(
                {"genre": ["rock", "pop rock"]}, "rock"
            )
        )
        self.assertTrue(
            _song_matches_queue_label(
                {"sd_path": "/02/001.mp3", "artist": "ATLUS GAME MUSIC"},
                "Persona",
            )
        )
        self.assertFalse(
            _song_matches_queue_label(
                {"artist": "Deftones", "genre": ["alternative rock"]}, "Queen"
            )
        )


class AlbumSuggestionLookupTests(unittest.TestCase):
    def test_song_ids_lookup_preserves_album_order_and_ignores_invalid_paths(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {"_id": "second", "sd_path": "/01/002.mp3"},
                {"_id": "unplayable"},
                {"_id": "first", "sd_path": "/01/001.mp3"},
            ]
        )

        songs = database.find_songs_by_ids(["first", "unplayable", "second"])

        self.assertEqual([song["_id"] for song in songs], ["first", "second"])
        self.assertEqual(database.find_candidates.call_args.kwargs["limit"], 3)


class AlbumQueueRequestTests(unittest.TestCase):
    def test_detects_album_playback_phrases(self):
        self.assertTrue(_ALBUM_PLAY_REQUEST.search("poneme el álbum Abbey Road"))
        self.assertTrue(
            _ALBUM_PLAY_REQUEST.search("quiero que me pongas el disco de Charly")
        )
        self.assertTrue(
            _ALBUM_PLAY_REQUEST.search("quiero escuchar el disco de Charly")
        )
        self.assertFalse(_ALBUM_PLAY_REQUEST.search("qué buen álbum"))

    def test_ai_filter_extraction_returns_album_name(self):
        ai = AIService.__new__(AIService)
        ai.settings = type("Settings", (), {"ai_top_results": 5})()
        ai._complete = Mock(
            return_value=(
                '{"artist":"Queen","title":null,"album":"A Night at the Opera",'
                '"start_track":null,"genre":null,"decade":null,"mood":null,'
                '"keywords":[],"limit":5}'
            )
        )

        filters = ai.extract_filters("poneme el álbum de Queen")

        self.assertEqual(filters["album"], "A Night at the Opera")
        self.assertIsNone(filters["start_track"])

    def test_ai_filter_extraction_recognizes_requested_start_track(self):
        ai = AIService.__new__(AIService)
        ai.settings = type("Settings", (), {"ai_top_results": 5})()
        ai._complete = Mock(
            return_value=(
                '{"artist":"Deftones","title":null,"album":"White Pony",'
                '"start_track":"Change","genre":null,"decade":null,"mood":null,'
                '"keywords":[],"limit":5}'
            )
        )

        filters = ai.extract_filters(
            "Quiero que reproduzcas el álbum de White Pony, empezando por Change."
        )

        self.assertEqual(filters["album"], "White Pony")
        self.assertEqual(filters["start_track"], "Change")

    def test_album_lookup_uses_exact_metadata_and_orders_by_track_number(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "second",
                    "title": "Second",
                    "sd_path": "/01/002.mp3",
                    "album_disc_number": 2,
                    "album_track_number": 2,
                },
                {
                    "_id": "unplayable",
                    "title": "No path",
                    "album_track_number": 1,
                },
                {
                    "_id": "first",
                    "title": "First",
                    "sd_path": "/01/001.mp3",
                    "album_disc_number": 1,
                    "album_track_number": 1,
                },
            ]
        )

        songs = database.find_playable_songs_by_album("A Kind of Magic", "Queen")

        self.assertEqual([song["_id"] for song in songs], ["first", "second"])
        query = database.find_candidates.call_args.args[0]
        self.assertEqual(
            query["album"],
            {"$regex": "^A\\ Kind\\ of\\ Magic$", "$options": "i"},
        )
        self.assertEqual(query["artist"]["$regex"], "^Queen$")

    def test_album_queue_rotates_to_requested_track_and_keeps_all_songs(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "first",
                    "title": "Back to School (Mini Maggit)",
                    "sd_path": "/16/001.mp3",
                    "album_track_number": 1,
                },
                {
                    "_id": "change",
                    "title": "Change (In the House of Flies)",
                    "sd_path": "/16/002.mp3",
                    "album_track_number": 2,
                },
                {
                    "_id": "last",
                    "title": "Pink Maggit",
                    "sd_path": "/16/003.mp3",
                    "album_track_number": 3,
                },
            ]
        )

        songs = database.find_playable_songs_by_album(
            "White Pony", "Deftones", "Change"
        )

        self.assertEqual(
            [song["_id"] for song in songs],
            ["change", "last", "first"],
        )

    def test_album_start_track_must_match_one_track(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "first",
                    "title": "First",
                    "sd_path": "/01/001.mp3",
                    "album_track_number": 1,
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "una única pista"):
            database.find_playable_songs_by_album("Album", "Artist", "Missing")

    def test_persona_folder_lookup_filters_paths_and_orders_by_track(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {"_id": "second", "title": "Second", "sd_path": "/02/002.mp3", "dfplayer_track": 2},
                {"_id": "invalid", "title": "Invalid", "sd_path": "/02/256.mp3", "dfplayer_track": 3},
                {"_id": "first", "title": "First", "sd_path": "/02/001.mp3", "dfplayer_track": 1},
            ]
        )

        songs = database.find_playable_songs_by_folder(2)

        self.assertEqual([song["_id"] for song in songs], ["first", "second"])
        self.assertEqual(
            database.find_candidates.call_args.args[0],
            {
                "dfplayer_folder": 2,
                "sd_path": {"$exists": True, "$nin": [None, ""]},
            },
        )

    def test_persona_folder_lookup_rotates_to_requested_track(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {"_id": "first", "title": "First", "sd_path": "/02/001.mp3", "dfplayer_track": 1},
                {"_id": "school-days", "title": "School Days", "sd_path": "/02/002.mp3", "dfplayer_track": 2},
                {"_id": "last", "title": "Last", "sd_path": "/02/003.mp3", "dfplayer_track": 3},
            ]
        )

        songs = database.find_playable_songs_by_folder(2, "School Days")

        self.assertEqual(
            [song["_id"] for song in songs],
            ["school-days", "last", "first"],
        )


class QueueAdvanceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_finish_event_advances_the_current_track(self):
        queue = PlaybackQueue()
        current = {"_id": "current", "title": "Current", "sd_path": "/01/001.mp3"}
        following = {"_id": "next", "title": "Next", "sd_path": "/01/002.mp3"}
        queue.enqueue([current, following])
        device = Mock()
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"playback_queue": queue, "device": device}
            )
        )

        with patch("app.bot._start_queued_song", new_callable=AsyncMock) as start:
            advanced = await _advance_playlist(
                context,
                chat_id=100,
                user_id=42,
                automatic=True,
                expected_song=current,
            )

        device.play.assert_called_once_with(following["sd_path"])
        self.assertTrue(advanced)
        self.assertIs(queue.current, following)
        start.assert_awaited_once()
        self.assertTrue(start.await_args.kwargs["device_already_started"])

    async def test_stale_device_finish_event_does_not_skip_current_track(self):
        queue = PlaybackQueue()
        previous = {"_id": "previous", "title": "Previous"}
        current = {"_id": "current", "title": "Current"}
        following = {"_id": "next", "title": "Next", "sd_path": "/01/003.mp3"}
        queue.enqueue([previous, current, following])
        queue.next()
        device = Mock()
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"playback_queue": queue, "device": device}
            )
        )

        advanced = await _advance_playlist(
            context,
            chat_id=100,
            user_id=42,
            automatic=True,
            expected_song=previous,
        )

        self.assertIs(queue.current, current)
        self.assertFalse(advanced)
        device.play.assert_not_called()

    async def test_concurrent_skip_does_not_send_another_device_command(self):
        queue = PlaybackQueue()
        songs = [
            {"title": "Current", "sd_path": "/01/001.mp3"},
            {"title": "Next", "sd_path": "/01/002.mp3"},
        ]
        queue.enqueue(songs)
        device = Mock()
        send_message = AsyncMock()
        lock = asyncio.Lock()
        await lock.acquire()
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "playback_queue": queue,
                    "device": device,
                    "queue_advance_lock": lock,
                }
            ),
            bot=SimpleNamespace(send_message=send_message),
        )

        await _advance_playlist(context, chat_id=100, user_id=42)

        device.play.assert_not_called()
        send_message.assert_awaited_once()
        self.assertEqual(queue.current, songs[0])

    async def test_device_connection_failure_keeps_current_song_and_queue_position(self):
        queue = PlaybackQueue()
        songs = [
            {"title": "Current", "sd_path": "/01/001.mp3"},
            {"title": "Next", "sd_path": "/01/002.mp3"},
        ]
        queue.enqueue(songs)
        device = Mock()
        device.play.side_effect = requests.exceptions.ConnectionError("offline")
        send_message = AsyncMock()
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"playback_queue": queue, "device": device}
            ),
            bot=SimpleNamespace(send_message=send_message),
        )

        advanced = await _advance_playlist(context, chat_id=100, user_id=42)

        self.assertFalse(advanced)
        self.assertEqual(queue.current, songs[0])
        self.assertEqual(queue.upcoming, [songs[1]])
        self.assertIn("La canción actual y la cola no cambiaron", send_message.await_args.args[1])


class JumpToQueuedSongTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_stops_device_and_clears_entire_queue_and_timer(self):
        queue = PlaybackQueue()
        queue.enqueue([{"title": "Current"}, {"title": "Next"}])
        device = Mock()
        history = Mock()
        message = SimpleNamespace(reply_text=AsyncMock())
        bot_data = {
            "player_settings": SimpleNamespace(player_enabled=True),
            "device": device,
            "playback_history": history,
            "playback_queue": queue,
            "queue_timer_remaining": 30,
            "queue_paused": True,
        }
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data=bot_data)
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100),
        )

        await stop_playback_request(update, context)

        device.stop.assert_called_once_with()
        history.stop.assert_called_once_with()
        self.assertIsNone(queue.current)
        self.assertEqual(queue.upcoming, [])
        self.assertNotIn("queue_timer_remaining", bot_data)
        self.assertFalse(bot_data["queue_paused"])
        self.assertIn("Detuve la reproducción", message.reply_text.await_args.args[0])

    async def test_stop_preserves_queue_if_device_rejects_order(self):
        queue = PlaybackQueue()
        songs = [{"title": "Current"}, {"title": "Next"}]
        queue.enqueue(songs)
        device = Mock()
        device.stop.side_effect = requests.exceptions.ConnectionError("offline")
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "player_settings": SimpleNamespace(player_enabled=True),
                    "device": device,
                    "playback_history": Mock(),
                    "playback_queue": queue,
                }
            )
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100),
        )

        await stop_playback_request(update, context)

        self.assertEqual(queue.current, songs[0])
        self.assertEqual(queue.upcoming, [songs[1]])
        self.assertIn("La cola no cambió", message.reply_text.await_args.args[0])

    async def test_clear_queue_keeps_current_song_and_removes_future_tracks(self):
        queue = PlaybackQueue()
        current, future = {"title": "Current"}, {"title": "Future"}
        queue.enqueue([current, future])
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"playback_queue": queue})
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100),
        )

        await clear_upcoming_queue_request(update, context)

        self.assertEqual(queue.current, current)
        self.assertEqual(queue.upcoming, [])
        self.assertIn("Quité 1", message.reply_text.await_args.args[0])

    async def test_keep_filter_preserves_current_song_and_matches_future_metadata(self):
        queue = PlaybackQueue()
        current = {
            "title": "Current",
            "sd_path": "/01/001.mp3",
            "dfplayer_folder": 1,
            "artist": "Deftones",
        }
        queen = {
            "title": "Song by Queen",
            "sd_path": "/02/001.mp3",
            "artist": "Queen",
        }
        other = {
            "title": "Other",
            "sd_path": "/03/001.mp3",
            "artist": "Other Artist",
        }
        queue.enqueue([current, other, queen])
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"playback_queue": queue})
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100),
        )

        await keep_queue_matching_request(update, context, "Queen")

        self.assertEqual(queue.current, current)
        self.assertEqual(queue.upcoming, [queen])
        self.assertIn("Quité 1", message.reply_text.await_args.args[0])
        self.assertIn("podrá terminar", message.reply_text.await_args.args[0])
        self.assertIn("Queen", message.reply_text.await_args.args[0])

    async def test_jump_starts_selected_song_and_removes_preceding_upcoming_tracks(self):
        queue = PlaybackQueue()
        current = {"title": "Current", "sd_path": "/01/001.mp3"}
        skipped = {"title": "Skipped", "sd_path": "/01/002.mp3"}
        target = {"title": "Target Song", "sd_path": "/01/003.mp3"}
        following = {"title": "Following", "sd_path": "/01/004.mp3"}
        queue.enqueue([current, skipped, target, following])
        device = Mock()
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"playback_queue": queue, "device": device}
            )
        )

        with patch("app.bot._start_queued_song", new_callable=AsyncMock) as start:
            await jump_to_queued_song(
                context, "Target Song", message, chat_id=100, user_id=42
            )

        device.play.assert_called_once_with(target["sd_path"])
        self.assertEqual(queue.current, target)
        self.assertEqual(queue.upcoming, [following])
        start.assert_awaited_once()
        self.assertTrue(start.await_args.kwargs["device_already_started"])

    async def test_failed_jump_keeps_queue_unchanged(self):
        queue = PlaybackQueue()
        current = {"title": "Current", "sd_path": "/01/001.mp3"}
        target = {"title": "Target Song", "sd_path": "/01/002.mp3"}
        queue.enqueue([current, target])
        device = Mock()
        device.play.side_effect = requests.exceptions.ConnectionError("offline")
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"playback_queue": queue, "device": device}
            )
        )

        await jump_to_queued_song(
            context, "Target Song", message, chat_id=100, user_id=42
        )

        self.assertEqual(queue.current, current)
        self.assertEqual(queue.upcoming, [target])
        self.assertIn("La cola no cambió", message.reply_text.await_args.args[0])


class AlbumQueueUIIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unmatched_persona_game_album_offers_available_persona_folder(self):
        songs = [
            {
                "_id": "school-days",
                "title": "School Days",
                "artist": "ATLUS GAME MUSIC",
                "sd_path": "/02/001.mp3",
                "dfplayer_folder": 2,
            }
        ]
        ai = SimpleNamespace(
            extract_filters=Mock(
                return_value={
                    "album": "Persona 3 Reload",
                    "artist": None,
                    "start_track": None,
                }
            )
        )
        database = SimpleNamespace(
            find_playable_songs_by_album=Mock(return_value=[]),
            find_playable_songs_by_folder=Mock(return_value=songs),
        )
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100),
            effective_message=message,
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "ai": ai,
                    "database": database,
                    "playback_queue": PlaybackQueue(),
                    "player_settings": SimpleNamespace(player_enabled=True),
                },
                user_data={42: {}},
            )
        )
        keyboard = object()

        with patch("app.bot._play_context_keyboard", return_value=keyboard) as build_keyboard:
            await album_play_request(
                update, context, "poneme el álbum Persona 3 Reload"
            )

        database.find_playable_songs_by_folder.assert_called_once_with(2, None)
        build_keyboard.assert_called_once_with(
            None, songs, "la carpeta Persona", context, 42
        )
        self.assertIn("No puedo confirmar que todas sean de esa", message.reply_text.await_args.args[0])
        self.assertIs(message.reply_text.await_args.kwargs["reply_markup"], keyboard)

    async def test_idle_album_starts_with_one_player_control_message(self):
        user_id = 42
        songs = [
            {"_id": "change", "title": "Change", "artist": "Deftones"},
            {"_id": "pink", "title": "Pink Maggit", "artist": "Deftones"},
        ]
        ai = SimpleNamespace(
            extract_filters=Mock(
                return_value={
                    "album": "White Pony",
                    "artist": "Deftones",
                    "start_track": "Change",
                }
            )
        )
        database = SimpleNamespace(find_playable_songs_by_album=Mock(return_value=songs))
        queue = PlaybackQueue()
        effective_message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=100),
            effective_message=effective_message,
        )
        application = SimpleNamespace(
            bot_data={
                "ai": ai,
                "database": database,
                "playback_queue": queue,
                "player_settings": SimpleNamespace(player_enabled=True),
            },
            user_data={user_id: {}},
        )
        context = SimpleNamespace(application=application)

        with patch("app.bot._start_queued_song", new_callable=AsyncMock) as start:
            await album_play_request(
                update,
                context,
                "poné White Pony empezando por Change",
            )

        self.assertEqual(queue.current, songs[0])
        start.assert_awaited_once()
        self.assertIn("Añadí 2 canciones", start.await_args.kwargs["status_note"])
        effective_message.reply_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
