import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.ai_service import AIService, _is_quota_error
from app.bot import (
    _CHAT_WEB_LOOKUP_REQUEST,
    _MUSIC_WANT_REQUEST,
    _PLAYBACK_REQUEST,
    _RECOMMENDATION_REQUEST,
    _needs_chat_web_lookup,
)
from app.config import Settings
from app.device_client import DeviceClient
from app.recommender import Recommender, build_mongo_query
from app.sd_path import parse_sd_path
from scripts.index_sd_card import index_sd_card
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

    def test_music_question_is_routed_to_conversation(self):
        message = "¿Qué hace especial a Prince?"
        self.assertIsNone(_MUSIC_WANT_REQUEST.search(message))
        self.assertIsNone(_RECOMMENDATION_REQUEST.search(message))

    def test_playback_request_is_not_routed_as_a_recommendation(self):
        message = "puedes ponerme esa canción en el reproductor?"
        self.assertTrue(_PLAYBACK_REQUEST.search(message))

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

    def test_follow_up_playback_request_is_detected(self):
        self.assertTrue(_PLAYBACK_REQUEST.search("me gustaría que me la reproduzcas"))


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
                return_value=SimpleNamespace(tags=tags),
            ):
                indexed, skipped = index_sd_card(root, collection)

        self.assertEqual((indexed, skipped), (1, 0))
        update = collection.update_one.call_args.args[1]["$set"]
        self.assertEqual(update["sd_path"], "/01/003.mp3")
        self.assertEqual(update["dfplayer_folder"], 1)
        self.assertEqual(update["dfplayer_track"], 3)
        self.assertEqual(update["year"], 1984)
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
