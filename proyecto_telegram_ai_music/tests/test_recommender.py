import unittest

from app.ai_service import _is_quota_error
from app.recommender import Recommender, build_mongo_query


class AIQuotaDetectionTests(unittest.TestCase):
    def test_detects_openai_insufficient_quota_code(self):
        error = Exception("API request failed")
        error.code = "insufficient_quota"
        self.assertTrue(_is_quota_error(error))

    def test_detects_credit_exhaustion_message(self):
        self.assertTrue(_is_quota_error(Exception("You have no credits remaining")))

    def test_does_not_mistake_temporary_rate_limit_for_empty_credits(self):
        self.assertFalse(_is_quota_error(Exception("Too many requests")))


class BuildMongoQueryTests(unittest.TestCase):
    def test_builds_artist_and_decade_filters(self):
        query = build_mongo_query(
            {
                "artist": "Queen",
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


if __name__ == "__main__":
    unittest.main()
