from datetime import datetime


class PlaybackStateStore:
    DOCUMENT_ID = "telegram_playback"

    def __init__(self, collection):
        self.collection = collection

    def load(self) -> dict | None:
        return self.collection.find_one({"_id": self.DOCUMENT_ID})

    def save_queue(
        self,
        songs: list[dict],
        repeat_mode: str,
        started_at: datetime | None,
        paused: bool,
        user_id: int | None,
        chat_id: int | None,
    ) -> None:
        document = {
            "song_ids": [str(song["_id"]) for song in songs if song.get("_id")],
            "repeat_mode": repeat_mode,
            "started_at": started_at,
            "paused": paused,
            "user_id": user_id,
            "chat_id": chat_id,
        }
        self.collection.update_one(
            {"_id": self.DOCUMENT_ID},
            {"$set": document},
            upsert=True,
        )

    def save_autoplay(self, user_id: int, enabled: bool) -> None:
        self.collection.update_one(
            {"_id": self.DOCUMENT_ID},
            {"$set": {f"autoplay_users.{user_id}": enabled}},
            upsert=True,
        )
