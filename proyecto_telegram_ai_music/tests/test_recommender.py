import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pymongo.errors import PyMongoError

from app.ai_service import AIService, _is_quota_error
from app.bot import (
    _CHAT_WEB_LOOKUP_REQUEST,
    _LIBRARY_AVAILABILITY_QUESTION,
    _MUSIC_WANT_REQUEST,
    _PLAYBACK_REQUEST,
    _REPEAT_PLAYBACK_REQUEST,
    _RECOMMENDATION_REQUEST,
    _REPORT_CONTEXT_REQUEST,
    _REPORT_REQUEST,
    _format_playback_report,
    send_playback_report,
    _try_natural_language_report,
    _needs_chat_web_lookup,
    reports_command,
    voice_message,
)
from app.config import Settings
from app.database import SongDatabase
from app.device_client import DeviceClient
from app.playback_history import (
    PlaybackHistory,
    build_playback_report,
    report_period_bounds,
)
from app.recommender import Recommender, build_mongo_query
from app.sd_path import parse_sd_path
from scripts.index_sd_card import index_sd_card
from scripts.organize_sd_library import _infer_deftones_title
from esp32_firmware.protocol import build_frame
from scripts.seed_alternative_rock import (
    ALTERNATIVE_ROCK,
    build_documents as build_alternative_rock_documents,
)
from scripts.seed_80s import build_documents


class AIQuotaDetectionTests(unittest.TestCase):
    def test_detects_openai_insufficient_quota_code(self):
        error = Exception("API request failed")
        error.code = "insufficient_quota"
        self.assertTrue(_is_quota_error(error))

    def test_detects_credit_exhaustion_message(self):
        self.assertTrue(_is_quota_error(Exception("You have no credits remaining")))

    def test_does_not_mistake_temporary_rate_limit_for_empty_credits(self):
        self.assertFalse(_is_quota_error(Exception("Too many requests")))

    def test_transcribes_voice_in_spanish_with_configured_model(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_transcription_model="whisper-test")
        create = Mock(return_value=SimpleNamespace(text="  hits de los 80  "))
        ai.client = SimpleNamespace(
            audio=SimpleNamespace(
                transcriptions=SimpleNamespace(create=create)
            )
        )

        transcript = ai.transcribe_audio(b"ogg-data")

        self.assertEqual(transcript, "hits de los 80")
        create.assert_called_once_with(
            model="whisper-test",
            file=("voice.ogg", b"ogg-data", "audio/ogg"),
            language="es",
        )

    def test_rejects_empty_audio_and_empty_transcriptions(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_transcription_model="whisper-test")
        create = Mock(return_value=SimpleNamespace(text="  "))
        ai.client = SimpleNamespace(
            audio=SimpleNamespace(
                transcriptions=SimpleNamespace(create=create)
            )
        )

        with self.assertRaises(ValueError):
            ai.transcribe_audio(b"")
        with self.assertRaises(ValueError):
            ai.transcribe_audio(b"ogg-data")


class MessageRoutingTests(unittest.TestCase):
    def test_title_request_is_routed_to_library_search(self):
        message = "quiero purple rain"
        self.assertTrue(_MUSIC_WANT_REQUEST.search(message))
        self.assertIsNone(_RECOMMENDATION_REQUEST.search(message))

    def test_filter_extraction_receives_recent_context_for_playback_references(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_top_results=5)
        ai._complete = Mock(
            return_value=(
                '{"artist":"Queen","title":"Bohemian Rhapsody","genre":null,'
                '"decade":null,"mood":null,"keywords":[],"limit":1}'
            )
        )
        history = [
            {"role": "user", "content": "Vamos con Bohemian Rhapsody"},
            {"role": "assistant", "content": "Una gran canción de Queen."},
        ]
        result = ai.extract_filters(
            "me gustaría que me reproduzcas esa canción", history
        )
        prompt = ai._complete.call_args.args[1]
        self.assertIn("Bohemian Rhapsody", prompt)
        self.assertEqual(result["title"], "Bohemian Rhapsody")

    def test_chat_extracts_only_an_explicit_song_reference(self):
        ai = AIService.__new__(AIService)
        ai.settings = SimpleNamespace(ai_model="test")
        ai.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(
                        return_value=SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    message=SimpleNamespace(
                                        content=(
                                            '{"reply":"Es un tema muy conocido.",'
                                            '"mentioned_song_title":"Purple Rain",'
                                            '"mentioned_song_artist":"Prince"}'
                                        )
                                    )
                                )
                            ]
                        )
                    )
                )
            )
        )

        reply, song = ai.chat_music("¿Qué tiene de especial Purple Rain?", [])
        self.assertEqual(reply, "Es un tema muy conocido.")
        self.assertEqual(song, {"title": "Purple Rain", "artist": "Prince"})

    def test_music_question_is_routed_to_conversation(self):
        message = "¿Qué hace especial a Prince?"
        self.assertIsNone(_MUSIC_WANT_REQUEST.search(message))
        self.assertIsNone(_RECOMMENDATION_REQUEST.search(message))

    def test_internet_access_question_is_routed_to_conversation(self):
        message = "tienes acceso a internet?"
        self.assertIsNone(_RECOMMENDATION_REQUEST.search(message))

    def test_song_availability_question_still_routes_to_library(self):
        message = "tienes canciones de Queen?"
        self.assertTrue(_RECOMMENDATION_REQUEST.search(message))

    def test_playback_request_is_not_routed_as_a_recommendation(self):
        message = "puedes ponerme esa canción en el reproductor?"
        self.assertTrue(_PLAYBACK_REQUEST.search(message))

    def test_library_availability_question_stays_in_conversation(self):
        message = "no la tienes para escucharla?"
        self.assertTrue(_LIBRARY_AVAILABILITY_QUESTION.search(message))

    def test_song_recommendation_is_not_mistaken_for_playback(self):
        message = "poneme hits movidos de los 80"
        self.assertIsNone(_PLAYBACK_REQUEST.search(message))
        self.assertTrue(_RECOMMENDATION_REQUEST.search(message))

    def test_greeting_does_not_trigger_web_search(self):
        self.assertFalse(_needs_chat_web_lookup("hola"))

    def test_current_music_question_can_trigger_web_search(self):
        self.assertTrue(_needs_chat_web_lookup("¿Cuándo es la próxima gira de Prince?"))

    def test_static_music_question_does_not_trigger_web_search(self):
        self.assertFalse(_needs_chat_web_lookup("¿Qué hace especial a Prince?"))

    def test_natural_language_playback_report_request_is_detected(self):
        self.assertTrue(_REPORT_REQUEST.search("¿Qué escuché esta semana?"))
        self.assertTrue(_REPORT_REQUEST.search("¿Cuánto tiempo escuché hoy?"))
        self.assertTrue(_REPORT_CONTEXT_REQUEST.search("mis canciones más escuchadas"))
        self.assertFalse(_REPORT_REQUEST.search("¿Qué canciones movidas recomiendas?"))

    def test_follow_up_playback_request_is_detected(self):
        self.assertTrue(_PLAYBACK_REQUEST.search("me gustaría que me la reproduzcas"))

    def test_repeat_request_is_not_sent_as_an_unqualified_search(self):
        self.assertTrue(_REPEAT_PLAYBACK_REQUEST.search("quiero escucharla otra vez"))


class BuildMongoQueryTests(unittest.TestCase):
    def test_builds_artist_and_decade_filters(self):
        query = build_mongo_query(
            {
                "artist": "Queen",
                "title": None,
                "decade": 1980,
                "genre": None,
                "mood": None,
                "keywords": [],
            }
        )
        self.assertEqual(
            query,
            {
                "$and": [
                    {"artist": {"$regex": "Queen", "$options": "i"}},
                    {
                        "$or": [
                            {"year": {"$gte": 1980, "$lt": 1990}},
                            {"decade": 1980},
                        ]
                    },
                ]
            },
        )

    def test_escapes_regex_metacharacters(self):
        query = build_mongo_query(
            {
                "artist": "A+B",
                "title": None,
                "genre": None,
                "mood": None,
                "decade": None,
                "keywords": [],
            }
        )
        self.assertEqual(query["artist"]["$regex"], r"A\+B")

    def test_empty_filters_return_all_songs(self):
        self.assertEqual(
            build_mongo_query(
                {
                    "artist": None,
                    "title": None,
                    "genre": None,
                    "mood": None,
                    "decade": None,
                    "keywords": [],
                }
            ),
            {},
        )


class VoiceMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribes_voice_and_routes_transcript_through_text_flow(self):
        message = Mock()
        message.voice.file_id = "telegram-file"
        message.voice.file_size = 100
        message.chat.send_action = AsyncMock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message)
        audio_file = Mock()
        audio_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"ogg-data")
        )
        context = Mock()
        context.bot.get_file = AsyncMock(return_value=audio_file)
        ai = Mock()
        ai.transcribe_audio.return_value = "buenos hits de los 80"
        context.application.bot_data = {"ai": ai}

        with patch("app.bot.process_text_message", new_callable=AsyncMock) as route:
            await voice_message(update, context)

        ai.transcribe_audio.assert_called_once_with(b"ogg-data", "voice.ogg")
        message.reply_text.assert_awaited_once_with(
            "Entendí: «buenos hits de los 80»"
        )
        route.assert_awaited_once_with(
            update, context, "buenos hits de los 80"
        )

    async def test_rejects_voice_larger_than_telegram_download_limit(self):
        message = Mock()
        message.voice.file_id = "telegram-file"
        message.voice.file_size = 21 * 1024 * 1024
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message)
        context = Mock()
        context.bot.get_file = AsyncMock()

        await voice_message(update, context)

        message.reply_text.assert_awaited_once()
        context.bot.get_file.assert_not_awaited()


class RecommenderTests(unittest.TestCase):
    def test_only_returns_songs_from_database_candidates(self):
        songs = [
            {"_id": "local-1", "title": "Local song 1"},
            {"_id": "local-2", "title": "Local song 2"},
        ]

        class FakeDatabase:
            def find_candidates(self, query, limit):
                return songs

        class FakeAI:
            def extract_filters(self, request_text, history=None):
                return {"limit": 2}

            def rank_songs(self, request_text, candidates, web_context):
                return ["not-in-library", "local-2"]

        class FakeWeb:
            def search(self, query):
                return ""

        results, web_failed = Recommender(
            FakeDatabase(), FakeAI(), FakeWeb()
        ).recommend("algo")
        self.assertEqual([song["_id"] for song in results], ["local-2", "local-1"])
        self.assertFalse(web_failed)

    def test_deduplicates_same_track_by_title_and_artist(self):
        songs = [
            {"_id": "first", "title": "Purple Rain", "artist": "Prince"},
            {"_id": "duplicate", "title": "purple rain ", "artist": " prince "},
        ]

        class FakeDatabase:
            def find_candidates(self, query, limit):
                return songs

        class FakeAI:
            def extract_filters(self, request_text, history=None):
                return {"limit": 5}

            def rank_songs(self, request_text, candidates, web_context):
                self.candidate_count = len(candidates)
                return ["first"]

        class FakeWeb:
            def search(self, query):
                return ""

        ai = FakeAI()
        results, _ = Recommender(FakeDatabase(), ai, FakeWeb()).recommend("Purple Rain")
        self.assertEqual(len(results), 1)
        self.assertEqual(ai.candidate_count, 1)

    def test_title_search_finds_song_after_strict_filters_miss(self):
        purple_rain = {
            "_id": "purple-rain",
            "title": "Purple Rain",
            "artist": "Prince",
        }

        class FakeDatabase:
            def find_candidates(self, query, limit):
                pattern = query.get("title", {}).get("$regex")
                if pattern and re.fullmatch(pattern, "Purple Rain", re.IGNORECASE):
                    return [purple_rain]
                return []

        class FakeAI:
            def extract_filters(self, request_text, history=None):
                self.history = history
                return {
                    "artist": None,
                    "title": None,
                    "genre": "alternative rock",
                    "decade": 1990,
                    "mood": None,
                    "keywords": [],
                    "limit": 1,
                }

            def rank_songs(self, request_text, candidates, web_context):
                return ["purple-rain"]

        class FakeWeb:
            def search(self, query):
                return ""

        songs, _ = Recommender(FakeDatabase(), FakeAI(), FakeWeb()).recommend(
            "quiero Purple Rain"
        )
        self.assertEqual(songs, [purple_rain])


class SDPathTests(unittest.TestCase):
    def test_parses_valid_dfplayer_sd_path(self):
        self.assertEqual(parse_sd_path("/01/003.mp3"), (1, 3))

    def test_rejects_paths_outside_the_dfplayer_card_layout(self):
        for path in ("../01/003.mp3", "/100/003.mp3", "/01/256.mp3", "/MP3/song.mp3"):
            with self.subTest(path=path):
                self.assertIsNone(parse_sd_path(path))

    def test_chat_song_lookup_requires_exact_names_and_a_valid_sd_path(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "invalid-path",
                    "title": "Fine",
                    "artist": "Lemon Demon",
                    "sd_path": None,
                },
                {
                    "_id": "playable",
                    "title": "Fine",
                    "artist": "Lemon Demon",
                    "sd_path": "/01/002.mp3",
                },
            ]
        )

        result = database.find_playable_song_by_title_artist(
            "Fine", "Lemon Demon"
        )

        self.assertEqual(result["_id"], "playable")
        query = database.find_candidates.call_args.args[0]
        self.assertEqual(query["title"]["$regex"], r"^Fine$")
        self.assertEqual(query["artist"]["$regex"], r"^Lemon\ Demon$")

    def test_chat_song_lookup_matches_title_and_artist_across_user_messages(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "fine",
                    "title": "Fine",
                    "artist": "Lemon Demon",
                    "sd_path": "/01/002.mp3",
                }
            ]
        )

        result = database.find_playable_song_mentioned_in(
            ["Me gusta mucho Lemon Demon", "Sii, Fine"]
        )

        self.assertEqual(result["_id"], "fine")
        database.find_candidates.assert_called_once_with(
            {"sd_path": {"$exists": True, "$nin": [None, ""]}},
            limit=500,
        )

    def test_chat_song_lookup_suggests_unique_title_without_artist_mention(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "school-days",
                    "title": "School Days",
                    "artist": "ATLUS GAME MUSIC",
                    "sd_path": "/01/003.mp3",
                }
            ]
        )

        result = database.find_playable_song_mentioned_in(
            [
                "Te gusta la música del videojuego persona?",
                "Tenés alguna de persona 1?",
                "Me gusta school days",
            ]
        )

        self.assertEqual(result["_id"], "school-days")

    def test_chat_song_lookup_requires_artist_to_disambiguate_duplicate_titles(self):
        database = SongDatabase.__new__(SongDatabase)
        database.find_candidates = Mock(
            return_value=[
                {
                    "_id": "school-days-atlus",
                    "title": "School Days",
                    "artist": "ATLUS GAME MUSIC",
                    "sd_path": "/01/003.mp3",
                },
                {
                    "_id": "school-days-other",
                    "title": "School Days",
                    "artist": "Other Artist",
                    "sd_path": "/02/001.mp3",
                },
            ]
        )

        without_artist = database.find_playable_song_mentioned_in(
            ["Me gusta School Days"]
        )
        with_artist = database.find_playable_song_mentioned_in(
            ["Me gusta School Days de ATLUS GAME MUSIC"]
        )

        self.assertIsNone(without_artist)
        self.assertEqual(with_artist["_id"], "school-days-atlus")

    def test_device_client_sends_only_a_valid_sd_path_to_esp32(self):
        settings = Settings(
            telegram_bot_token="telegram",
            mongo_uri="mongodb://localhost:27017",
            mongo_db="music",
            mongo_collection="songs",
            ai_model="test",
            ai_api_key="test",
            ai_base_url=None,
            ai_timeout_seconds=3,
            ai_top_results=5,
            web_search_enabled=False,
            web_search_api_key="",
            web_search_max_results=5,
            player_enabled=True,
            esp32_base_url="http://192.168.1.50:8080",
            esp32_api_token="a" * 24,
        )
        response = Mock()
        response.json.return_value = {"accepted": True}
        with patch("app.device_client.requests.post", return_value=response) as post:
            DeviceClient(settings).play("/01/003.mp3")
        post.assert_called_once_with(
            "http://192.168.1.50:8080/api/play",
            json={"sd_path": "/01/003.mp3"},
            headers={"Authorization": f"Bearer {'a' * 24}"},
            timeout=3,
        )

    def test_device_client_rejects_invalid_sd_path_without_network_request(self):
        settings = Settings(
            telegram_bot_token="telegram",
            mongo_uri="mongodb://localhost:27017",
            mongo_db="music",
            mongo_collection="songs",
            ai_model="test",
            ai_api_key="test",
            ai_base_url=None,
            ai_timeout_seconds=3,
            ai_top_results=5,
            web_search_enabled=False,
            web_search_api_key="",
            web_search_max_results=5,
            player_enabled=True,
            esp32_base_url="http://192.168.1.50:8080",
            esp32_api_token="a" * 24,
        )
        with patch("app.device_client.requests.post") as post:
            with self.assertRaises(ValueError):
                DeviceClient(settings).play("../../secret.mp3")
        post.assert_not_called()

    def test_device_client_sends_pause_and_resume_commands(self):
        settings = Settings(
            telegram_bot_token="telegram",
            mongo_uri="mongodb://localhost:27017",
            mongo_db="music",
            mongo_collection="songs",
            ai_model="test",
            ai_api_key="test",
            ai_base_url=None,
            ai_timeout_seconds=3,
            ai_top_results=5,
            web_search_enabled=False,
            web_search_api_key="",
            web_search_max_results=5,
            player_enabled=True,
            esp32_base_url="http://192.168.1.50:8080",
            esp32_api_token="a" * 24,
        )
        response = Mock()
        response.json.return_value = {"accepted": True}
        with patch("app.device_client.requests.post", return_value=response) as post:
            device = DeviceClient(settings)
            device.pause()
            device.resume()

        self.assertEqual(
            [call.args[0] for call in post.call_args_list],
            [
                "http://192.168.1.50:8080/api/pause",
                "http://192.168.1.50:8080/api/resume",
            ],
        )
        self.assertEqual([call.kwargs["json"] for call in post.call_args_list], [{}, {}])

    def test_sd_indexer_adds_route_to_matching_mongodb_song(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mp3 = root / "01" / "003.mp3"
            mp3.parent.mkdir()
            mp3.write_bytes(b"test")
            collection = Mock()
            collection.find_one.return_value = {"_id": "existing-song"}
            tags = {
                "title": ["Purple Rain"],
                "artist": ["Prince"],
                "album": ["Purple Rain"],
                "date": ["1984"],
                "genre": ["Rock"],
            }
            with patch(
                "scripts.index_sd_card.MutagenFile",
                return_value=SimpleNamespace(
                    tags=tags, info=SimpleNamespace(length=227.2)
                ),
            ):
                indexed, skipped = index_sd_card(root, collection)

        self.assertEqual((indexed, skipped), (1, 0))
        update = collection.update_one.call_args.args[1]["$set"]
        self.assertEqual(update["sd_path"], "/01/003.mp3")
        self.assertEqual(update["dfplayer_folder"], 1)
        self.assertEqual(update["dfplayer_track"], 3)
        self.assertEqual(update["year"], 1984)
        self.assertEqual(update["duration_sec"], 228)
        self.assertEqual(collection.update_one.call_args.args[0], {"_id": "existing-song"})

    def test_sd_indexer_adds_route_to_duplicate_matching_songs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mp3 = root / "01" / "001.mp3"
            mp3.parent.mkdir()
            mp3.write_bytes(b"test")
            collection = Mock()
            collection.find_one.return_value = None
            collection.update_many.return_value.matched_count = 2
            tags = {"title": ["Purple Rain"], "artist": ["Prince"]}
            with patch(
                "scripts.index_sd_card.MutagenFile",
                return_value=SimpleNamespace(tags=tags),
            ):
                indexed, skipped = index_sd_card(root, collection)

        self.assertEqual((indexed, skipped), (1, 0))
        query = collection.update_many.call_args.args[0]
        self.assertEqual(query["title"]["$regex"], r"^Purple\ Rain$")
        self.assertEqual(query["artist"]["$regex"], r"^Prince$")
        update = collection.update_many.call_args.args[1]["$set"]
        self.assertEqual(update["sd_path"], "/01/001.mp3")
        collection.update_one.assert_not_called()

    def test_sd_indexer_unsets_unreliable_1970_year(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mp3 = root / "01" / "001.mp3"
            mp3.parent.mkdir()
            mp3.write_bytes(b"test")
            collection = Mock()
            collection.find_one.return_value = {"_id": "existing-song"}
            tags = {
                "title": ["Old Song"],
                "artist": ["Example Artist"],
                "date": ["1970"],
            }
            with patch(
                "scripts.index_sd_card.MutagenFile",
                return_value=SimpleNamespace(tags=tags),
            ):
                index_sd_card(root, collection)

        update = collection.update_one.call_args.args[1]
        self.assertNotIn("year", update["$set"])
        self.assertEqual(update["$unset"], {"year": "", "decade": ""})

    def test_sd_indexer_ignores_non_dfplayer_filenames(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            invalid_mp3 = root / "01" / "song.mp3"
            invalid_mp3.parent.mkdir()
            invalid_mp3.write_bytes(b"test")
            collection = Mock()
            with patch("scripts.index_sd_card.MutagenFile") as read_metadata:
                indexed, skipped = index_sd_card(root, collection)

        self.assertEqual((indexed, skipped), (0, 0))
        read_metadata.assert_not_called()

    def test_sd_indexer_avoids_deterministic_id_collision(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mp3 = root / "01" / "001.mp3"
            mp3.parent.mkdir()
            mp3.write_bytes(b"test")
            collection = Mock()
            collection.find_one.side_effect = [None, {"_id": "another-song"}]
            collection.update_many.return_value.matched_count = 0
            tags = {"title": ["Purple Rain"], "artist": ["Prince"]}
            with patch(
                "scripts.index_sd_card.MutagenFile",
                return_value=SimpleNamespace(tags=tags),
            ):
                indexed, skipped = index_sd_card(root, collection)

        self.assertEqual((indexed, skipped), (1, 0))
        query, update = collection.update_one.call_args.args
        self.assertEqual(query, {"sd_path": "/01/001.mp3"})
        self.assertTrue(collection.update_one.call_args.kwargs["upsert"])
        self.assertEqual(update["$set"]["title"], "Purple Rain")


class LibraryOrganizationTests(unittest.TestCase):
    def test_infers_deftones_title_from_filename(self):
        self.assertEqual(
            _infer_deftones_title("Deftones - Be Quiet and Drive - My Music Harbourage.mp3"),
            "Be Quiet and Drive",
        )


class PlaybackHistoryTests(unittest.TestCase):
    def test_daily_and_weekly_bounds_use_argentina_local_calendar(self):
        now = datetime(2026, 11, 11, 2, tzinfo=timezone.utc)

        daily_start, daily_end = report_period_bounds("daily", now)
        weekly_start, weekly_end = report_period_bounds("weekly", now)

        self.assertEqual(daily_start, datetime(2026, 11, 10, 3, tzinfo=timezone.utc))
        self.assertEqual(daily_end, datetime(2026, 11, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(weekly_start, datetime(2026, 11, 9, 3, tzinfo=timezone.utc))
        self.assertEqual(weekly_end, datetime(2026, 11, 16, 3, tzinfo=timezone.utc))

    def test_rejects_unknown_report_period(self):
        with self.assertRaises(ValueError):
            report_period_bounds("monthly")

    def test_report_sums_play_time_minus_pauses_and_ranks_songs(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        start, _ = report_period_bounds("daily", now)
        first_start = start + timedelta(hours=10)
        sessions = [
            {
                "song_id": "song-a",
                "sd_path": "/01/001.mp3",
                "title": "First Song",
                "artist": "Artist",
                "genre": ["Rock"],
                "duration_sec": 3600,
                "started_at": first_start,
                "ended_at": first_start + timedelta(minutes=60),
                "pause_intervals": [
                    {
                        "started_at": first_start + timedelta(minutes=20),
                        "ended_at": first_start + timedelta(minutes=30),
                    }
                ],
            },
            {
                "song_id": "song-a",
                "sd_path": "/01/001.mp3",
                "title": "First Song",
                "artist": "Artist",
                "genre": ["Rock"],
                "duration_sec": 3600,
                "started_at": first_start + timedelta(hours=2),
                "ended_at": first_start + timedelta(hours=2, minutes=30),
                "pause_intervals": [],
            },
            {
                "song_id": "song-b",
                "sd_path": "/02/001.mp3",
                "title": "Second Song",
                "artist": "Other Artist",
                "genre": ["Pop"],
                "duration_sec": 2400,
                "started_at": first_start + timedelta(hours=3),
                "ended_at": first_start + timedelta(hours=3, minutes=40),
                "pause_intervals": [],
            },
        ]

        report = build_playback_report(sessions, "daily", now)

        self.assertEqual(report["play_count"], 3)
        self.assertEqual(report["total_seconds"], 7200)
        self.assertEqual(report["top_songs"][0]["title"], "First Song")
        self.assertEqual(report["top_songs"][0]["plays"], 2)
        self.assertEqual(report["top_genres"], [("Rock", 4800.0), ("Pop", 2400.0)])

    def test_report_counts_only_the_overlap_with_period(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        start, _ = report_period_bounds("daily", now)
        report = build_playback_report(
            [
                {
                    "song_id": "crossing",
                    "sd_path": "/01/001.mp3",
                    "title": "Crossing Song",
                    "artist": "Artist",
                    "genre": ["Rock"],
                    "started_at": start - timedelta(minutes=10),
                    "ended_at": start + timedelta(minutes=20),
                    "pause_intervals": [],
                },
                {
                    "song_id": "old",
                    "sd_path": "/01/002.mp3",
                    "title": "Old Song",
                    "artist": "Artist",
                    "started_at": start - timedelta(hours=2),
                    "ended_at": start - timedelta(hours=1),
                    "pause_intervals": [],
                },
            ],
            "daily",
            now,
        )

        self.assertEqual(report["play_count"], 1)
        self.assertEqual(report["total_seconds"], 1200)

    def test_report_excludes_time_after_the_current_open_pause(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        start, _ = report_period_bounds("daily", now)
        session_start = start - timedelta(minutes=20)
        report = build_playback_report(
            [
                {
                    "song_id": "paused",
                    "sd_path": "/01/001.mp3",
                    "title": "Paused Song",
                    "artist": "Artist",
                    "started_at": session_start,
                    "pause_started_at": start - timedelta(minutes=10),
                    "pause_intervals": [],
                }
            ],
            "daily",
            now,
        )

        self.assertEqual(report["play_count"], 0)
        self.assertEqual(report["total_seconds"], 0)

    def test_report_caps_an_active_session_at_track_duration(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        start, _ = report_period_bounds("daily", now)
        session_start = start + timedelta(hours=1)
        report = build_playback_report(
            [
                {
                    "song_id": "song-a",
                    "sd_path": "/01/001.mp3",
                    "title": "First Song",
                    "artist": "Artist",
                    "started_at": session_start,
                    "duration_sec": 180,
                    "pause_intervals": [],
                }
            ],
            "daily",
            now,
        )

        self.assertEqual(report["total_seconds"], 180)

    def test_report_caps_each_song_and_genre_lists_to_requested_top_counts(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        start, _ = report_period_bounds("daily", now)
        sessions = [
            {
                "song_id": str(index),
                "sd_path": f"/01/{index + 1:03}.mp3",
                "title": f"Song {index}",
                "artist": "Artist",
                "genre": [f"Genre {index}"],
                "started_at": start + timedelta(hours=index),
                "ended_at": start + timedelta(hours=index, minutes=1),
                "pause_intervals": [],
            }
            for index in range(8)
        ]

        report = build_playback_report(sessions, "daily", now)

        self.assertEqual(len(report["top_songs"]), 5)
        self.assertEqual(len(report["top_genres"]), 3)

    def test_report_returns_zero_totals_when_no_session_matches(self):
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        report = build_playback_report([], "weekly", now)

        self.assertEqual(report["play_count"], 0)
        self.assertEqual(report["total_seconds"], 0)
        self.assertEqual(report["top_songs"], [])
        self.assertEqual(report["top_genres"], [])

    def test_start_closes_previous_session_and_saves_song_metadata(self):
        collection = Mock()
        collection.find_one.return_value = {"_id": "previous"}
        history = PlaybackHistory(collection)

        history.start(
            {
                "_id": "song-1",
                "title": "New Song",
                "artist": "Artist",
                "album": "Album",
                "genre": ["Rock"],
                "sd_path": "/01/001.mp3",
                "duration_sec": 200,
            },
            user_id=42,
        )

        self.assertEqual(
            collection.update_one.call_args.args[0],
            {"_id": "previous", "ended_at": {"$exists": False}},
        )
        document = collection.insert_one.call_args.args[0]
        self.assertEqual(document["song_id"], "song-1")
        self.assertEqual(document["user_id"], 42)
        self.assertEqual(document["duration_sec"], 200)
        self.assertEqual(document["genre"], ["Rock"])
        self.assertIsNotNone(document["started_at"].tzinfo)

    def test_pause_and_resume_update_the_active_session(self):
        collection = Mock()
        collection.find_one.side_effect = [
            {"_id": "session-1"},
            {
                "_id": "session-1",
                "pause_started_at": datetime(2026, 11, 10, 12, tzinfo=timezone.utc),
            },
        ]
        collection.update_one.return_value.modified_count = 1
        history = PlaybackHistory(collection)

        self.assertTrue(history.pause())
        self.assertIn("pause_started_at", collection.update_one.call_args.args[1]["$set"])
        self.assertTrue(history.resume())
        self.assertIn("$push", collection.update_one.call_args.args[1])
        self.assertEqual(
            collection.update_one.call_args.args[1]["$unset"],
            {"pause_started_at": ""},
        )

    def test_pause_and_resume_return_false_without_matching_session(self):
        collection = Mock()
        collection.find_one.return_value = None
        history = PlaybackHistory(collection)

        self.assertFalse(history.pause())
        self.assertFalse(history.resume())
        collection.update_one.assert_not_called()

    def test_start_handles_string_genre_and_missing_duration(self):
        collection = Mock()
        collection.find_one.return_value = None
        history = PlaybackHistory(collection)

        history.start(
            {
                "_id": "song-1",
                "title": "New Song",
                "artist": "Artist",
                "genre": "Pop",
                "sd_path": "/01/001.mp3",
            },
            user_id=42,
        )

        document = collection.insert_one.call_args.args[0]
        self.assertEqual(document["genre"], ["Pop"])
        self.assertIsNone(document["duration_sec"])

    def test_report_queries_only_sessions_overlapping_period(self):
        collection = Mock()
        collection.find.return_value = []
        history = PlaybackHistory(collection)
        now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)

        history.report("daily", now)

        query = collection.find.call_args.args[0]
        self.assertEqual(
            query["started_at"]["$lt"],
            datetime(2026, 11, 11, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(
            query["$or"][0]["ended_at"]["$gt"],
            datetime(2026, 11, 10, 3, tzinfo=timezone.utc),
        )

    def test_report_period_classifier_validates_model_output(self):
        ai = AIService.__new__(AIService)
        ai._complete = Mock(return_value='{"period":"weekly"}')

        self.assertEqual(
            ai.classify_report_period("¿Qué escuché esta semana?"), "weekly"
        )

        ai._complete.return_value = '{"period":"all_time"}'
        self.assertIsNone(ai.classify_report_period("muéstrame mis estadísticas"))
        ai._complete.return_value = '{"period":true}'
        self.assertIsNone(ai.classify_report_period("mis canciones más escuchadas"))


class PlaybackReportCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_language_report_uses_ai_period_and_sends_report(self):
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
        ai = SimpleNamespace(classify_report_period=Mock(return_value="weekly"))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"ai": ai}))

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            handled = await _try_natural_language_report(
                update, context, "¿Qué escuché esta semana?"
            )

        self.assertTrue(handled)
        send.assert_awaited_once_with(update, context, "weekly")
        ai.classify_report_period.assert_called_once_with("¿Qué escuché esta semana?")

    async def test_non_report_text_skips_ai_classification(self):
        ai = SimpleNamespace(classify_report_period=Mock())
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"ai": ai}))
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))

        handled = await _try_natural_language_report(
            update, context, "Recomiéndame canciones movidas."
        )

        self.assertFalse(handled)
        ai.classify_report_period.assert_not_called()

    async def test_report_command_defaults_to_daily_period(self):
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=AsyncMock())
        )
        context = SimpleNamespace(args=[])

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            await reports_command(update, context)

        send.assert_awaited_once_with(update, context, "daily")

    async def test_report_command_rejects_unknown_period(self):
        reply = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))
        context = SimpleNamespace(args=["mensual"])

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            await reports_command(update, context)

        reply.assert_awaited_once()
        self.assertIn("Uso:", reply.await_args.args[0])
        send.assert_not_awaited()

    async def test_report_command_accepts_weekly_alias(self):
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=AsyncMock())
        )
        context = SimpleNamespace(args=["semana"])

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            await reports_command(update, context)

        send.assert_awaited_once_with(update, context, "weekly")

    async def test_report_command_accepts_daily_alias(self):
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=AsyncMock())
        )
        context = SimpleNamespace(args=["hoy"])

        with patch("app.bot.send_playback_report", new_callable=AsyncMock) as send:
            await reports_command(update, context)

        send.assert_awaited_once_with(update, context, "daily")

    async def test_natural_language_request_without_report_intent_falls_through(self):
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
        ai = SimpleNamespace(classify_report_period=Mock(return_value=None))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"ai": ai}))

        handled = await _try_natural_language_report(
            update, context, "Muéstrame mis canciones más escuchadas."
        )

        self.assertFalse(handled)
        ai.classify_report_period.assert_called_once()

    async def test_natural_language_request_reports_ai_failure_with_command_fallback(self):
        reply = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))
        ai = SimpleNamespace(classify_report_period=Mock(side_effect=RuntimeError("AI down")))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"ai": ai}))

        with self.assertLogs("app.bot", level="ERROR"):
            handled = await _try_natural_language_report(
                update, context, "¿Qué escuché esta semana?"
            )

        self.assertTrue(handled)
        self.assertIn("/reportes diario", reply.await_args.args[0])

    async def test_send_report_surfaces_database_failure(self):
        reply = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))
        history = SimpleNamespace(report=Mock(side_effect=PyMongoError("database down")))
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"playback_history": history})
        )

        with self.assertLogs("app.bot", level="ERROR"):
            await send_playback_report(update, context, "daily")

        self.assertIn("MongoDB", reply.await_args.args[0])

    def test_report_format_escapes_song_metadata(self):
        start, end = report_period_bounds(
            "daily", datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        )
        text = _format_playback_report(
            {
                "period": "daily",
                "start": start,
                "end": end,
                "play_count": 1,
                "total_seconds": 65,
                "top_songs": [
                    {
                        "title": "<Song>",
                        "artist": "Artist & Co",
                        "plays": 1,
                        "seconds": 65,
                    }
                ],
                "top_genres": [("<Rock>", 65)],
            }
        )

        self.assertIn("&lt;Song&gt;", text)
        self.assertIn("Artist &amp; Co", text)
        self.assertIn("&lt;Rock&gt;", text)
        self.assertNotIn("<Song>", text)

    def test_empty_report_format_explains_that_no_plays_are_available(self):
        start, end = report_period_bounds(
            "weekly", datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        )
        text = _format_playback_report(
            {
                "period": "weekly",
                "start": start,
                "end": end,
                "play_count": 0,
                "total_seconds": 0,
                "top_songs": [],
                "top_genres": [],
            }
        )

        self.assertIn("Informe semanal", text)
        self.assertIn("Todavía no hay reproducciones", text)


class DFPlayerProtocolTests(unittest.TestCase):
    def test_builds_play_folder_command_frame(self):
        frame = build_frame(0x0F, (1 << 8) | 3)
        self.assertEqual(len(frame), 10)
        self.assertEqual(frame[:7], bytes((0x7E, 0xFF, 0x06, 0x0F, 0, 1, 3)))
        self.assertEqual(frame[-1], 0xEF)
        checksum = (frame[7] << 8) | frame[8]
        self.assertEqual(checksum, (-(0xFF + 0x06 + 0x0F + 1 + 3)) & 0xFFFF)

    def test_requests_feedback_for_dfplayer_command(self):
        frame = build_frame(0x0F, (1 << 8) | 1, feedback=1)
        self.assertEqual(frame[4], 1)
        checksum = (frame[7] << 8) | frame[8]
        self.assertEqual(
            checksum,
            (-(0xFF + 0x06 + 0x0F + 1 + 1 + 1)) & 0xFFFF,
        )

    def test_pause_and_resume_commands_use_dfplayer_protocol_codes(self):
        self.assertEqual(build_frame(0x0E)[3], 0x0E)
        self.assertEqual(build_frame(0x0D)[3], 0x0D)

    def test_dfplayer_bass_equalizer_command_uses_supported_mode(self):
        frame = build_frame(0x07, 5)
        self.assertEqual(frame[3], 0x07)
        self.assertEqual(frame[6], 5)


class SeedCatalogTests(unittest.TestCase):
    def test_seed_catalog_has_100_unique_songs_and_includes_prince(self):
        songs = build_documents()
        self.assertEqual(len(songs), 100)
        self.assertEqual(len({song["_id"] for song in songs}), 100)
        self.assertTrue(any(song["artist"] == "Prince" for song in songs))
        self.assertTrue(all(song["decade"] == 1980 for song in songs))

    def test_alternative_catalog_has_unique_tracks_and_keeps_purple_rain_accurate(self):
        songs = build_alternative_rock_documents()
        self.assertEqual(len(songs), len(ALTERNATIVE_ROCK) + 1)
        self.assertEqual(len({song["_id"] for song in songs}), len(songs))
        purple_rain = next(song for song in songs if song["title"] == "Purple Rain")
        self.assertEqual(purple_rain["artist"], "Prince")
        self.assertEqual(purple_rain["genre"], ["rock"])
        self.assertEqual(purple_rain["year"], 1984)
        self.assertTrue(
            all(
                song["genre"] == ["alternative rock"]
                for song in songs
                if song["title"] != "Purple Rain"
            )
        )


if __name__ == "__main__":
    unittest.main()
