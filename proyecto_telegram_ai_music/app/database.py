import re

from bson import ObjectId
from pymongo import MongoClient

from app.config import Settings
from app.sd_path import parse_sd_path


class SongDatabase:
    def __init__(self, settings: Settings):
        self.client = MongoClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
        self.client.admin.command("ping")
        self.songs = self.client[settings.mongo_db][settings.mongo_collection]

    def find_candidates(self, query: dict, limit: int = 30) -> list[dict]:
        projection = {
            "_id": 1,
            "title": 1,
            "artist": 1,
            "album": 1,
            "genre": 1,
            "year": 1,
            "decade": 1,
            "mood": 1,
            "tags": 1,
            "bpm": 1,
            "duration_sec": 1,
            "sd_path": 1,
        }
        return list(self.songs.find(query, projection).limit(limit))

    def find_song_by_id(self, song_id: str) -> dict | None:
        identifiers = [song_id]
        if ObjectId.is_valid(song_id):
            identifiers.append(ObjectId(song_id))
        return self.songs.find_one({"_id": {"$in": identifiers}})

    def find_playable_song_by_title_artist(
        self, title: str, artist: str
    ) -> dict | None:
        query = {
            "title": {"$regex": f"^{re.escape(title)}$", "$options": "i"},
            "artist": {"$regex": f"^{re.escape(artist)}$", "$options": "i"},
        }
        for song in self.find_candidates(query, limit=20):
            if (
                str(song.get("title", "")).strip().casefold()
                == title.strip().casefold()
                and str(song.get("artist", "")).strip().casefold()
                == artist.strip().casefold()
                and parse_sd_path(song.get("sd_path"))
            ):
                return song
        return None

    def find_playable_song_mentioned_in(
        self, user_messages: list[str]
    ) -> dict | None:
        conversation = " ".join(user_messages[-6:])
        if not conversation:
            return None

        candidates = self.find_candidates(
            {"sd_path": {"$exists": True, "$nin": [None, ""]}},
            limit=500,
        )
        title_matches = []
        for song in candidates:
            title = str(song.get("title", "")).strip()
            artist = str(song.get("artist", "")).strip()
            if not title or not artist or not parse_sd_path(song.get("sd_path")):
                continue
            title_mentioned = re.search(
                rf"(?<!\w){re.escape(title)}(?!\w)", conversation, re.IGNORECASE
            )
            if title_mentioned:
                title_matches.append(song)

        if len(title_matches) == 1:
            return title_matches[0]

        artist_matches = [
            song
            for song in title_matches
            if re.search(
                rf"(?<!\w){re.escape(str(song['artist']).strip())}(?!\w)",
                conversation,
                re.IGNORECASE,
            )
        ]
        return artist_matches[0] if len(artist_matches) == 1 else None

    def close(self) -> None:
        self.client.close()
