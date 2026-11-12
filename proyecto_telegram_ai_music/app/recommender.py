import re
import unicodedata
from difflib import SequenceMatcher
import logging

import requests

from app.ai_service import AIService, WebSearch
from app.database import SongDatabase

logger = logging.getLogger(__name__)

_PERSONA_GAME_REQUEST = re.compile(r"\bpersona\b", re.IGNORECASE)


def requested_song_count(request_text: str) -> int | None:
    match = re.search(
        r"\b(\d{1,3})\s+(?:canciones?|temas?|pistas?)\b",
        request_text,
        re.IGNORECASE,
    )
    if not match:
        return None
    count = int(match.group(1))
    return min(count, 100) if count > 0 else None


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


def _corrected_title_from_request(request_text: str) -> str | None:
    marker = re.search(
        r"\b(?:quise\s+decir|quer[ií]a\s+decir|me\s+refer[ií]a\s+a)\b",
        request_text,
        re.IGNORECASE,
    )
    if not marker:
        return None

    before_marker = request_text[: marker.start()]
    after_marker = request_text[marker.end() :]
    candidates = [
        re.split(r"[,.;:!?¿¡]\s*", before_marker)[-1].strip(" \t\"'«»"),
        after_marker.strip(" \t,.;:!?¿¡\"'«»"),
    ]
    for candidate in candidates:
        words = re.findall(r"[^\W_]+", candidate, flags=re.UNICODE)
        if 2 <= len(words) <= 8:
            return candidate
    return None


def _unique_songs(songs: list[dict]) -> list[dict]:
    unique = []
    positions = {}
    for song in songs:
        title = str(song.get("title", "")).strip().casefold()
        artist = str(song.get("artist", "")).strip().casefold()
        identity = (title, artist) if title or artist else ("_id", str(song.get("_id")))
        if identity in positions:
            index = positions[identity]
            if not unique[index].get("sd_path") and song.get("sd_path"):
                unique[index] = song
            continue
        positions[identity] = len(unique)
        unique.append(song)
    return unique


def _normalized_artist(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[^\W_]+", without_accents, flags=re.UNICODE))


def _best_matching_artist(
    requested_artist: str, songs: list[dict]
) -> str | None:
    requested = _normalized_artist(requested_artist)
    if len(requested.replace(" ", "")) < 8:
        return None

    artist_names = {}
    for song in songs:
        for artist in re.split(r"\s*/\s*|\s*;\s*", str(song.get("artist", ""))):
            normalized = _normalized_artist(artist)
            if normalized:
                artist_names.setdefault(normalized, artist.strip())

    matches = sorted(
        (
            SequenceMatcher(None, requested, normalized).ratio(),
            normalized,
        )
        for normalized in artist_names
    )
    if not matches:
        return None
    best_score, best_normalized = matches[-1]
    second_score = matches[-2][0] if len(matches) > 1 else 0
    if best_score < 0.84 or best_score - second_score < 0.06:
        return None
    return artist_names[best_normalized]


class Recommender:
    def __init__(self, database: SongDatabase, ai: AIService, web: WebSearch):
        self.database = database
        self.ai = ai
        self.web = web

    def recommend(
        self,
        request_text: str,
        history: list[dict[str, str]] | None = None,
        excluded_ids: list[str] | None = None,
    ) -> tuple[list[dict], bool]:
        filters = self.ai.extract_filters(request_text, history or [])
        requested_count = requested_song_count(request_text)
        if requested_count:
            filters["limit"] = requested_count
        corrected_title = _corrected_title_from_request(request_text)
        candidates = []
        if _PERSONA_GAME_REQUEST.search(request_text):
            candidates = self.database.find_playable_songs_by_folder(2)
        elif corrected_title:
            candidates = self.database.find_candidates(
                {"title": {"$regex": re.escape(corrected_title), "$options": "i"}},
                limit=100,
            )
        if not candidates:
            candidates = self.database.find_candidates(
                build_mongo_query(filters), limit=100
            )
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
        if not candidates and filters.get("artist"):
            library_songs = self.database.find_candidates({}, limit=1000)
            matched_artist = _best_matching_artist(
                filters["artist"], library_songs
            )
            if matched_artist:
                matched_artist_name = _normalized_artist(matched_artist)
                candidates = [
                    song
                    for song in library_songs
                    if any(
                        _normalized_artist(artist) == matched_artist_name
                        for artist in re.split(
                            r"\s*/\s*|\s*;\s*",
                            str(song.get("artist", "")),
                        )
                    )
                ]
        excluded = set(excluded_ids or [])
        if excluded:
            candidates = [
                song for song in candidates if str(song.get("_id")) not in excluded
            ]
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
