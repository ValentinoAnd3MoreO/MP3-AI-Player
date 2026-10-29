import logging
import re

import requests

from app.ai_service import AIService, WebSearch
from app.database import SongDatabase

logger = logging.getLogger(__name__)


def build_mongo_query(filters: dict) -> dict:
    clauses = []

    artist = filters.get("artist")
    if artist:
        clauses.append({"artist": {"$regex": re.escape(artist), "$options": "i"}})

    genre = filters.get("genre")
    if genre:
        clauses.append({"genre": {"$regex": re.escape(genre), "$options": "i"}})

    mood = filters.get("mood")
    if mood:
        clauses.append({"mood": {"$regex": re.escape(mood), "$options": "i"}})

    decade = filters.get("decade")
    if isinstance(decade, int):
        clauses.append(
            {
                "$or": [
                    {"year": {"$gte": decade, "$lt": decade + 10}},
                    {"decade": decade},
                ]
            }
        )

    keywords = filters.get("keywords", [])
    for keyword in keywords[:8]:
        if isinstance(keyword, str) and keyword.strip():
            pattern = {"$regex": re.escape(keyword.strip()), "$options": "i"}
            clauses.append(
                {
                    "$or": [
                        {"title": pattern},
                        {"artist": pattern},
                        {"album": pattern},
                        {"tags": pattern},
                    ]
                }
            )

    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class Recommender:
    def __init__(self, database: SongDatabase, ai: AIService, web: WebSearch):
        self.database = database
        self.ai = ai
        self.web = web

    def recommend(self, request_text: str) -> tuple[list[dict], bool]:
        filters = self.ai.extract_filters(request_text)
        candidates = self.database.find_candidates(
            build_mongo_query(filters), limit=100
        )
        if not candidates:
            return [], False

        web_failed = False
        web_context = ""
        try:
            web_context = self.web.search(request_text)
        except (requests.exceptions.RequestException, ValueError) as exc:
            logger.warning("Búsqueda web no disponible: %s", exc)
            web_failed = True

        ranked_ids = self.ai.rank_songs(request_text, candidates, web_context)

        by_id = {str(song["_id"]): song for song in candidates}
        selected = [by_id[song_id] for song_id in ranked_ids if song_id in by_id]
        selected_ids = {str(song["_id"]) for song in selected}
        fallback = [song for song in candidates if str(song["_id"]) not in selected_ids]
        return (selected + fallback)[: filters["limit"]], web_failed
