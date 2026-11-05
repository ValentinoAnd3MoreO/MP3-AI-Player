from bson import ObjectId
from pymongo import MongoClient

from app.config import Settings


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

    def close(self) -> None:
        self.client.close()
