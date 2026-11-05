import logging
import re

import requests

from app.ai_service import AIService, WebSearch
from app.database import SongDatabase

logger = logging.getLogger(__name__)

_TITLE_FILLER = {
    "algo", "cancion", "canciones", "de", "el", "escucha", "escuchar",
    "favor", "la", "me", "mi", "musica", "pon", "poneme", "ponme",
    "por", "quiero", "reproduce", "tema", "temas", "una",
}


def build_mongo_query(filters: dict) -> dict:
    clauses = []

    artist = filters.get("artist")
    if artist:
        clauses.append({"artist": {"$regex": re.escape(artist), "$options": "i"}})

    title = filters.get("title")
    if title:
        clauses.append({"title": {"$regex": re.escape(title), "$options": "i"}})

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


def _title_from_request(request_text: str) -> str | None:
    words = re.findall(r"[^\W_]+", request_text.lower(), flags=re.UNICODE)
    title_words = [word for word in words if word not in _TITLE_FILLER]
    if not 1 <= len(title_words) <= 5:
        return None
    return " ".join(title_words)


def _unique_songs(songs: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for song in songs:
        title = str(song.get("title", "")).strip().casefold()
        artist = str(song.get("artist", "")).strip().casefold()
        identity = (title, artist) if title or artist else ("_id", str(song.get("_id")))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(song)
    return unique


class Recommender:
    def __init__(self, database: SongDatabase, ai: AIService, web: WebSearch):
        self.database = database
        self.ai = ai
        self.web = web

    def recommend(self, request_text: str) -> tuple[list[dict], bool]:
        filters = self.ai.extract_filters(request_text)
        candidates = self.database.find_candidates(build_mongo_query(filters), limit=100)
        if not candidates:
            relaxed_filters = {
                **filters,
                "genre": None,
                "mood": None,
                "decade": None,
            }
            relaxed_query = build_mongo_query(relaxed_filters)
            if relaxed_query != build_mongo_query(filters):
                candidates = self.database.find_candidates(relaxed_query, limit=100)
        if not candidates:
            title = filters.get("title") or _title_from_request(request_text)
            if title:
                candidates = self.database.find_candidates(
                    {"title": {"$regex": re.escape(title), "$options": "i"}},
                    limit=100,
                )
        if not candidates:
            return [], False
        candidates = _unique_songs(candidates)

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
