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
            "dfplayer_folder": 1,
            "dfplayer_track": 1,
            "album_track_number": 1,
            "album_disc_number": 1,
        }
        return list(self.songs.find(query, projection).limit(limit))

    def find_song_by_id(self, song_id: str) -> dict | None:
        identifiers = [song_id]
        if ObjectId.is_valid(song_id):
            identifiers.append(ObjectId(song_id))
        return self.songs.find_one({"_id": {"$in": identifiers}})

    def find_songs_by_ids(self, song_ids: list[str]) -> list[dict]:
        identifiers = []
        for song_id in song_ids[:1000]:
            identifiers.append(song_id)
            if ObjectId.is_valid(song_id):
                identifiers.append(ObjectId(song_id))
        if not identifiers:
            return []
        songs = self.find_candidates(
            {"_id": {"$in": identifiers}},
            limit=len(song_ids[:1000]),
        )
        by_id = {str(song["_id"]): song for song in songs}
        return [
            by_id[song_id]
            for song_id in song_ids[:1000]
            if song_id in by_id and parse_sd_path(by_id[song_id].get("sd_path"))
        ]

    def find_playable_songs_by_folder(
        self, folder: int, start_track: str | None = None
    ) -> list[dict]:
        if (
            not isinstance(folder, int)
            or isinstance(folder, bool)
            or not 1 <= folder <= 99
        ):
            raise ValueError("La carpeta DFPlayer debe ser un entero del 1 al 99.")
        songs = [
            song
            for song in self.find_candidates(
                {
                    "dfplayer_folder": folder,
                    "sd_path": {"$exists": True, "$nin": [None, ""]},
                },
                limit=1000,
            )
            if parse_sd_path(song.get("sd_path"))
        ]
        songs.sort(
            key=lambda song: (
                song.get("dfplayer_track") or 0,
                str(song.get("title", "")).casefold(),
            )
        )
        if start_track:
            requested = re.sub(
                r"[^\w]+", " ", start_track.casefold(), flags=re.UNICODE
            ).strip()
            matches = []
            for index, song in enumerate(songs):
                title = re.sub(
                    r"[^\w]+",
                    " ",
                    str(song.get("title", "")).casefold(),
                    flags=re.UNICODE,
                ).strip()
                if title == requested or title.startswith(requested + " "):
                    matches.append(index)
            if len(matches) != 1:
                raise ValueError(
                    f"No se pudo identificar una única pista '{start_track}' "
                    f"en la carpeta DFPlayer {folder:02d}."
                )
            start_index = matches[0]
            songs = songs[start_index:] + songs[:start_index]
        return songs

    def find_playable_songs_by_album(
        self,
        album: str,
        artist: str | None = None,
        start_track: str | None = None,
    ) -> list[dict]:
        album = album.strip()
        if not album:
            return []
        query = {
            "album": {"$regex": f"^{re.escape(album)}$", "$options": "i"},
            "sd_path": {"$exists": True, "$nin": [None, ""]},
        }
        if artist:
            query["artist"] = {
                "$regex": f"^{re.escape(artist.strip())}$",
                "$options": "i",
            }
        songs = [
            song
            for song in self.find_candidates(query, limit=1000)
            if parse_sd_path(song.get("sd_path"))
        ]
        songs.sort(
            key=lambda song: (
                song.get("album_disc_number") or 1,
                song.get("album_track_number") is None,
                song.get("album_track_number") or 0,
                song.get("dfplayer_folder") or 0,
                song.get("dfplayer_track") or 0,
                str(song.get("title", "")).casefold(),
            )
        )
        if start_track:
            requested = re.sub(
                r"[^\w]+", " ", start_track.casefold(), flags=re.UNICODE
            ).strip()
            matches = []
            for index, song in enumerate(songs):
                title = re.sub(
                    r"[^\w]+",
                    " ",
                    str(song.get("title", "")).casefold(),
                    flags=re.UNICODE,
                ).strip()
                if title == requested or title.startswith(requested + " "):
                    matches.append(index)
            if len(matches) != 1:
                raise ValueError(
                    f"No se pudo identificar una única pista '{start_track}' "
                    f"en el álbum '{album}'."
                )
            start_index = matches[0]
            songs = songs[start_index:] + songs[:start_index]
        return songs

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
                artist_mentioned = re.search(
                    rf"(?<!\w){re.escape(artist)}(?!\w)",
                    conversation,
                    re.IGNORECASE,
                )
                title_matches.append((song, bool(artist_mentioned)))

        artist_matches = [
            song for song, artist_mentioned in title_matches if artist_mentioned
        ]
        if len(artist_matches) == 1:
            return artist_matches[0]

        distinctive_title_matches = [
            song
            for song, artist_mentioned in title_matches
            if not artist_mentioned
            and len(re.findall(r"[^\W_]+", str(song["title"]), re.UNICODE)) >= 2
        ]
        return (
            distinctive_title_matches[0]
            if len(distinctive_title_matches) == 1
            and len(title_matches) == 1
            else None
        )

    def close(self) -> None:
        self.client.close()
