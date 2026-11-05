import unittest
import re

from app.ai_service import _is_quota_error
from app.bot import (
    _CHAT_WEB_LOOKUP_REQUEST,
    _MUSIC_PREFERENCE_REQUEST,
    _MUSIC_WANT_REQUEST,
    _PLAYBACK_REQUEST,
    _RECOMMENDATION_REQUEST,
    _needs_chat_web_lookup,
)
from app.recommender import Recommender, build_mongo_query
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

    def test_music_question_is_routed_to_conversation(self):
        message = "¿Qué hace especial a Prince?"
        self.assertIsNone(_MUSIC_WANT_REQUEST.search(message))
        self.assertIsNone(_RECOMMENDATION_REQUEST.search(message))

    def test_playback_request_is_not_routed_as_a_recommendation(self):
        message = "puedes ponerme esa canción en el reproductor?"
        self.assertTrue(_PLAYBACK_REQUEST.search(message))

    def test_subjunctive_playback_request_is_recognized(self):
        self.assertTrue(
            _PLAYBACK_REQUEST.search("me gustaría que me la reproduzcas")
        )

    def test_request_to_hear_a_song_is_recognized_for_playback(self):
        self.assertTrue(_PLAYBACK_REQUEST.search("quiero escuchar Purple Rain"))

    def test_song_recommendation_is_not_mistaken_for_playback(self):
        message = "poneme hits movidos de los 80"
        self.assertIsNone(_PLAYBACK_REQUEST.search(message))
        self.assertTrue(_RECOMMENDATION_REQUEST.search(message))

    def test_favorite_artist_preference_is_recognized(self):
        match = _MUSIC_PREFERENCE_REQUEST.search("me gusta más Queen")
        self.assertIsNotNone(match)
        self.assertEqual(match.group("artist").strip(), "Queen")

    def test_greeting_does_not_trigger_web_search(self):
        self.assertFalse(_needs_chat_web_lookup("hola"))

    def test_current_music_question_can_trigger_web_search(self):
        self.assertTrue(_needs_chat_web_lookup("¿Cuándo es la próxima gira de Prince?"))

    def test_static_music_question_does_not_trigger_web_search(self):
        self.assertFalse(_needs_chat_web_lookup("¿Qué hace especial a Prince?"))


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
            def extract_filters(self, request_text):
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
            def extract_filters(self, request_text):
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
            def extract_filters(self, request_text):
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
