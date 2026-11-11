import argparse
import csv
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests
from pymongo import MongoClient

from app.config import Settings

logger = logging.getLogger(__name__)

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/release-group/"
MUSICBRAINZ_USER_AGENT = "MP3-AI-Player/1.0 (music metadata enrichment)"
GENRE_TAGS = {
    "alternative metal",
    "alternative rock",
    "art rock",
    "black metal",
    "blues",
    "classical",
    "country",
    "dance",
    "death metal",
    "dream pop",
    "emo",
    "electronic",
    "folk",
    "funk",
    "garage rock",
    "glam rock",
    "gothic rock",
    "grunge",
    "hard rock",
    "hardcore punk",
    "heavy metal",
    "hip hop",
    "indie pop",
    "indie rock",
    "j-pop",
    "j-rock",
    "jazz",
    "k-pop",
    "latin",
    "latin pop",
    "latin rock",
    "metal",
    "new wave",
    "nu metal",
    "pop",
    "pop rock",
    "post-hardcore",
    "post-punk",
    "post-rock",
    "power pop",
    "punk",
    "punk rock",
    "r&b",
    "rap",
    "reggae",
    "reggaeton",
    "rock",
    "rock argentino",
    "shoegaze",
    "singer-songwriter",
    "ska",
    "soul",
    "soundtrack",
    "surf rock",
    "synth-pop",
    "video game music",
}
_EDITION_SUFFIX = re.compile(
    r"\s*[\[(][^\])]*(?:deluxe|remaster(?:ed)?|anniversary|expanded|"
    r"super\s+deluxe|bonus\s+tracks?)[^\])]*[\])]\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AlbumEnrichment:
    status: str
    year: int | None = None
    genres: tuple[str, ...] = ()
    release_group_id: str | None = None
    reason: str = ""


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(
        character for character in normalized
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", normalized.casefold()).strip()


def _normalize_album(value: str) -> str:
    return _normalize(_EDITION_SUFFIX.sub("", value.strip()))


_NORMALIZED_GENRE_TAGS = {_normalize(genre) for genre in GENRE_TAGS}


def _primary_artist(value: str) -> str:
    return re.split(r"\s*/\s*|\s*;\s*|\s*,\s*(?:feat(?:uring)?\.?)\s+", value, maxsplit=1, flags=re.I)[0].strip()


def _release_artist(release_group: dict[str, Any]) -> str:
    credits = release_group.get("artist-credit", [])
    names = [str(credit.get("name", "")).strip() for credit in credits if isinstance(credit, dict)]
    return names[0] if names else ""


def _genre_tags(release_group: dict[str, Any]) -> tuple[str, ...]:
    tags = release_group.get("tags", [])
    selected: dict[str, int] = {}
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        name = str(tag.get("name", "")).strip()
        normalized = _normalize(name)
        try:
            count = int(tag.get("count", 0))
        except (TypeError, ValueError):
            continue
        if normalized in _NORMALIZED_GENRE_TAGS and count >= 2:
            selected[name] = max(count, selected.get(name, 0))
    return tuple(
        name
        for name, _ in sorted(
            selected.items(), key=lambda item: (-item[1], item[0].casefold())
        )[:4]
    )


def _release_year(value: Any) -> int | None:
    match = re.match(r"^(\d{4})", str(value or ""))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1900 <= year <= date.today().year + 1 else None


def match_release_group(
    artist: str, album: str, response_data: dict[str, Any]
) -> AlbumEnrichment:
    expected_artist = _normalize(_primary_artist(artist))
    expected_album = _normalize_album(album)
    exact_matches = []
    for candidate in response_data.get("release-groups", []):
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("primary-type", "")).casefold() != "album":
            continue
        if _normalize(_release_artist(candidate)) != expected_artist:
            continue
        if _normalize_album(str(candidate.get("title", ""))) != expected_album:
            continue
        score = candidate.get("score", 0)
        try:
            score = int(score)
        except (TypeError, ValueError):
            continue
        if score >= 90:
            exact_matches.append(candidate)

    if not exact_matches:
        return AlbumEnrichment("no_match", reason="No hubo coincidencia exacta")

    years = {
        year
        for candidate in exact_matches
        if (year := _release_year(candidate.get("first-release-date"))) is not None
    }
    year = min(years) if years else None
    original_album_matches = [
        candidate
        for candidate in exact_matches
        if _release_year(candidate.get("first-release-date")) == year
    ] if year else exact_matches

    selected = max(
        original_album_matches,
        key=lambda candidate: sum(
            int(tag.get("count", 0))
            for tag in candidate.get("tags", [])
            if isinstance(tag, dict)
            and _normalize(str(tag.get("name", ""))) in _NORMALIZED_GENRE_TAGS
        ),
    )
    return AlbumEnrichment(
        "matched",
        year=year,
        genres=_genre_tags(selected),
        release_group_id=str(selected.get("id", "")) or None,
        reason="Artista, álbum y puntaje de MusicBrainz verificados",
    )


def lookup_release_group(
    session: requests.Session, artist: str, album: str
) -> AlbumEnrichment:
    query_album = _EDITION_SUFFIX.sub("", album.strip())
    query = f'artist:"{_primary_artist(artist)}" AND releasegroup:"{query_album}"'
    params = {"query": query, "fmt": "json", "limit": 10, "inc": "genres"}
    for attempt in range(4):
        response = session.get(MUSICBRAINZ_URL, params=params, timeout=20)
        if response.status_code not in {429, 503} or attempt == 3:
            break
        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = min(60, max(1, int(retry_after)))
        except ValueError:
            delay = 5 * (2**attempt)
        logger.warning(
            "MusicBrainz respondió %s; reintento en %s segundos",
            response.status_code,
            delay,
        )
        time.sleep(delay)
    response.raise_for_status()
    return match_release_group(artist, album, response.json())


def _needs_year(document: dict) -> bool:
    value = document.get("year")
    try:
        year = int(value)
    except (TypeError, ValueError):
        return True
    return year == 1970


def _has_genre(document: dict) -> bool:
    value = document.get("genre")
    return bool(value) if isinstance(value, (str, list, tuple)) else False


def _album_groups(documents: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], dict] = {}
    for document in documents:
        artist = str(document.get("artist", "")).strip()
        album = str(document.get("album", "")).strip()
        if not artist or not album:
            continue
        primary_artist = _primary_artist(artist)
        key = (_normalize(primary_artist), _normalize_album(album))
        if not all(key):
            continue
        group = groups.setdefault(
            key,
            {
                "artist": primary_artist,
                "album": album,
                "documents": [],
            },
        )
        group["documents"].append(document)
    return list(groups.values())


def _updates_for_group(group: dict, enrichment: AlbumEnrichment) -> list[tuple[Any, dict]]:
    if enrichment.status != "matched":
        return []
    updates = []
    for document in group["documents"]:
        fields = {}
        if enrichment.year and _needs_year(document):
            fields.update(
                {
                    "year": enrichment.year,
                    "decade": (enrichment.year // 10) * 10,
                    "year_source": "MusicBrainz release-group first-release-date",
                }
            )
        if enrichment.genres and not _has_genre(document):
            fields.update(
                {
                    "genre": list(enrichment.genres),
                    "genre_source": "MusicBrainz community release-group tags",
                }
            )
        if fields:
            fields.update(
                {
                    "metadata_confidence": "high",
                    "musicbrainz_release_group_id": enrichment.release_group_id,
                }
            )
            updates.append((document["_id"], fields))
    return updates


def enrich_collection(collection, apply: bool = False) -> list[dict]:
    documents = list(
        collection.find(
            {
                "sd_path": {"$exists": True, "$nin": [None, ""]},
                "artist": {"$exists": True, "$nin": [None, ""]},
                "album": {"$exists": True, "$nin": [None, ""]},
            },
            {"_id": 1, "artist": 1, "album": 1, "year": 1, "genre": 1},
        )
    )
    groups = _album_groups(documents)
    results = []
    with requests.Session() as session:
        session.headers.update({"User-Agent": MUSICBRAINZ_USER_AGENT})
        for index, group in enumerate(groups):
            enrichment = lookup_release_group(
                session, group["artist"], group["album"]
            )
            updates = _updates_for_group(group, enrichment)
            year_updates = sum("year" in fields for _, fields in updates)
            genre_updates = sum("genre" in fields for _, fields in updates)
            applied = 0
            if apply:
                for document_id, fields in updates:
                    result = collection.update_one(
                        {"_id": document_id}, {"$set": fields}
                    )
                    applied += result.modified_count
            results.append(
                {
                    "artist": group["artist"],
                    "album": group["album"],
                    "songs": len(group["documents"]),
                    "status": enrichment.status,
                    "year": enrichment.year or "",
                    "genres": ", ".join(enrichment.genres),
                    "would_update": len(updates),
                    "would_set_year": year_updates,
                    "would_set_genre": genre_updates,
                    "updated": applied,
                    "reason": enrichment.reason,
                }
            )
            if index + 1 < len(groups):
                time.sleep(1.1)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Previsualiza metadatos de álbumes desde MusicBrainz. "
            "MongoDB solo se modifica con --apply."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Escribe coincidencias claras en MongoDB; por defecto solo previsualiza.",
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
        results = enrich_collection(collection, apply=args.apply)
    finally:
        client.close()

    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=[
            "artist",
            "album",
            "songs",
            "status",
            "year",
            "genres",
            "would_update",
            "would_set_year",
            "would_set_genre",
            "updated",
            "reason",
        ],
    )
    writer.writeheader()
    writer.writerows(results)
    matched = sum(result["status"] == "matched" for result in results)
    updated = sum(result["updated"] for result in results)
    year_updates = sum(result["would_set_year"] for result in results)
    genre_updates = sum(result["would_set_genre"] for result in results)
    mode = "aplicados" if args.apply else "propuestos"
    print(
        f"\nÁlbumes revisados: {len(results)}; coincidencias exactas: {matched}; "
        f"registros {mode}: {updated if args.apply else sum(r['would_update'] for r in results)} "
        f"(años: {year_updates}, géneros: {genre_updates}).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
