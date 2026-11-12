import argparse
import logging
import math
import re
from pathlib import Path

from mutagen import MutagenError
from mutagen import File as MutagenFile
from pymongo import MongoClient

from app.config import Settings
from app.sd_path import parse_sd_path

logger = logging.getLogger(__name__)


def _tag_text(tags: dict, key: str) -> str | None:
    value = tags.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _song_document(path: Path, sd_root: Path) -> dict | None:
    relative = path.relative_to(sd_root).as_posix()
    sd_path = f"/{relative}"
    track = parse_sd_path(sd_path)
    if track is None:
        logger.warning("Ruta incompatible con DFPlayer, se omite: %s", sd_path)
        return None

    try:
        metadata = MutagenFile(path, easy=True)
    except (MutagenError, OSError) as exc:
        logger.warning("No se pudieron leer los metadatos de %s: %s", sd_path, exc)
        return None
    if not metadata or not metadata.tags:
        logger.warning("No hay etiquetas ID3 legibles, se omite: %s", sd_path)
        return None

    title = _tag_text(metadata.tags, "title")
    artist = _tag_text(metadata.tags, "artist")
    if not title or not artist:
        logger.warning("Falta título o artista ID3, se omite: %s", sd_path)
        return None

    album = _tag_text(metadata.tags, "album")
    date = _tag_text(metadata.tags, "date") or _tag_text(metadata.tags, "year")
    year_match = re.search(r"\b(?:19|20)\d{2}\b", date or "")
    genres = metadata.tags.get("genre", [])
    if isinstance(genres, str):
        genres = [genres]
    genres = [str(genre).strip() for genre in genres if str(genre).strip()]
    folder_number, track_number = track
    document = {
        "title": title,
        "artist": artist,
        "sd_path": sd_path,
        "dfplayer_folder": folder_number,
        "dfplayer_track": track_number,
    }
    unset_fields = []
    if album:
        document["album"] = album
    track_number = _tag_text(metadata.tags, "tracknumber")
    track_match = re.match(r"\s*(\d+)", track_number or "")
    if track_match:
        document["album_track_number"] = int(track_match.group(1))
    else:
        unset_fields.append("album_track_number")
    disc_number = _tag_text(metadata.tags, "discnumber")
    disc_match = re.match(r"\s*(\d+)", disc_number or "")
    if disc_match:
        document["album_disc_number"] = int(disc_match.group(1))
    else:
        unset_fields.append("album_disc_number")
    if genres:
        document["genre"] = genres
    duration = getattr(getattr(metadata, "info", None), "length", None)
    if isinstance(duration, (int, float)) and duration > 0:
        document["duration_sec"] = math.ceil(duration)
    if year_match and int(year_match.group()) != 1970:
        document["year"] = int(year_match.group())
        document["decade"] = (document["year"] // 10) * 10
    elif year_match:
        unset_fields.extend(["year", "decade"])
    if unset_fields:
        document["_unset_fields"] = unset_fields
    return document


def index_sd_card(sd_root: Path, collection) -> tuple[int, int]:
    root = sd_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"No se encontró la carpeta de la microSD: {root}")

    indexed = 0
    skipped = 0
    mp3_files = (
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() == ".mp3"
        and parse_sd_path(f"/{path.relative_to(root).as_posix()}") is not None
    )
    for path in sorted(mp3_files):
        document = _song_document(path, root)
        if not document:
            skipped += 1
            continue
        unset_fields = document.pop("_unset_fields", [])
        year_unset_fields = [
            field for field in unset_fields if field in {"year", "decade"}
        ]
        unset_fields = [
            field for field in unset_fields if field not in {"year", "decade"}
        ]
        update = {"$set": document}
        if unset_fields:
            update["$unset"] = {field: "" for field in unset_fields}

        identity_query = {
            "title": {
                "$regex": f"^{re.escape(document['title'])}$",
                "$options": "i",
            },
            "artist": {
                "$regex": f"^{re.escape(document['artist'])}$",
                "$options": "i",
            },
        }
        existing = collection.find_one(
            {"sd_path": document["sd_path"]}, {"_id": 1}
        )
        if existing:
            collection.update_one(
                {"_id": existing["_id"]},
                update,
            )
            if year_unset_fields:
                collection.update_one(
                    {
                        "_id": existing["_id"],
                        "$or": [
                            {"year_source": {"$exists": False}},
                            {"year_source": None},
                            {"year_source": ""},
                        ],
                    },
                    {"$unset": {field: "" for field in year_unset_fields}},
                )
        matching_songs = collection.update_many(
            identity_query,
            update,
        )
        if year_unset_fields:
            collection.update_many(
                {
                    **identity_query,
                    "$or": [
                        {"year_source": {"$exists": False}},
                        {"year_source": None},
                        {"year_source": ""},
                    ],
                },
                {"$unset": {field: "" for field in year_unset_fields}},
            )
        if not existing and not matching_songs.matched_count:
            folder, track = parse_sd_path(document["sd_path"])
            document_id = f"dfplayer-{folder:02d}-{track:03d}"
            id_conflict = collection.find_one({"_id": document_id}, {"_id": 1})
            if id_conflict:
                collection.update_one(
                    {"sd_path": document["sd_path"]},
                    update,
                    upsert=True,
                )
            else:
                collection.update_one(
                    {"_id": document_id},
                    update,
                    upsert=True,
                )
        indexed += 1

    return indexed, skipped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Indexa música etiquetada en formato de carpetas del DFPlayer."
    )
    parser.add_argument(
        "--sd-root",
        required=True,
        type=Path,
        help="Raíz de la microSD, por ejemplo unidad E:",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    client = MongoClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
    )
    try:
        client.admin.command("ping")
        collection = client[settings.mongo_db][settings.mongo_collection]
        indexed, skipped = index_sd_card(args.sd_root, collection)
        print(
            f"Indexación finalizada en {settings.mongo_db}.{settings.mongo_collection}: "
            f"{indexed} canciones asociadas a rutas SD, {skipped} archivos omitidos."
        )
    finally:
        client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
