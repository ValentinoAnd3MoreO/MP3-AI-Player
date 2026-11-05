import hashlib
import re
import unicodedata
from pathlib import Path

from mutagen import File as MutagenFile
from pymongo import MongoClient

from app.config import Settings


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(
        char for char in normalized.casefold() if not unicodedata.combining(char)
    ).strip()


def _first_tag(tags, name: str, fallback: str = "") -> str:
    value = tags.get(name)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or fallback).strip()


def _year_from_tags(tags) -> int | None:
    date_text = _first_tag(tags, "date")
    match = re.search(r"\b(19|20)\d{2}\b", date_text)
    return int(match.group()) if match else None


def index_music(music_dir: Path, collection) -> tuple[int, int]:
    root = music_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing = list(collection.find({}, {"_id": 1, "title": 1, "artist": 1}))
    by_metadata = {}
    for song in existing:
        identity = (
            _normalize(str(song.get("title", ""))),
            _normalize(str(song.get("artist", ""))),
        )
        if all(identity):
            by_metadata.setdefault(identity, song["_id"])

    indexed = 0
    skipped = 0
    for file_path in root.rglob("*"):
        if not file_path.is_file() or file_path.suffix.lower() != ".mp3":
            continue
        audio = MutagenFile(file_path, easy=True)
        if audio is None:
            skipped += 1
            continue
        tags = audio.tags or {}
        title = _first_tag(tags, "title", file_path.stem)
        artist = _first_tag(tags, "artist", file_path.parent.name or "Artista desconocido")
        album = _first_tag(tags, "album")
        year = _year_from_tags(tags)
        genres = tags.get("genre", [])
        if isinstance(genres, str):
            genres = [genres]
        genre = str(genres[0]).strip() if genres else ""
        relative_path = file_path.relative_to(root).as_posix()
        identity = (_normalize(title), _normalize(artist))
        song_id = by_metadata.get(identity)
        if song_id is None:
            digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24]
            song_id = f"music-{digest}"

        update = {
            "title": title,
            "artist": artist,
            "path": relative_path,
        }
        if album:
            update["album"] = album
        if year:
            update["year"] = year
            update["decade"] = (year // 10) * 10
        if genre:
            update["genre"] = [genre]
        collection.update_one(
            {"_id": song_id},
            {
                "$set": update,
                "$setOnInsert": {"source": "local_mp3"},
            },
            upsert=True,
        )
        by_metadata[identity] = song_id
        indexed += 1
    return indexed, skipped


def main() -> None:
    settings = Settings.from_env()
    client = MongoClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
    )
    try:
        client.admin.command("ping")
        indexed, skipped = index_music(
            settings.music_dir,
            client[settings.mongo_db][settings.mongo_collection],
        )
        print(
            f"MP3 indexados: {indexed}; archivos no reconocidos: {skipped}; "
            f"carpeta: {settings.music_dir}"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
