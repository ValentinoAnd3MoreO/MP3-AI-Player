import argparse
import csv
import logging
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError
from pymongo import MongoClient

from app.config import Settings
from app.sd_path import parse_sd_path
from scripts.index_sd_card import index_sd_card

logger = logging.getLogger(__name__)


@dataclass
class PlannedTrack:
    source: Path
    title: str
    artist: str
    album: str | None
    group_artist: str
    persona: bool
    from_sd: bool = False
    track_number: int | None = None
    disc_number: int | None = None
    infer_tags: bool = False
    clear_album: bool = False
    folder_number: int = 0
    file_number: int = 0

    @property
    def sd_path(self) -> str:
        return f"/{self.folder_number:02d}/{self.file_number:03d}.mp3"

    @property
    def relative_path(self) -> Path:
        return Path(f"{self.folder_number:02d}") / f"{self.file_number:03d}.mp3"


def _tag_text(tags, key: str) -> str | None:
    value = tags.get(key) if tags else None
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _tag_number(tags, key: str) -> int | None:
    value = _tag_text(tags, key)
    if not value:
        return None
    try:
        number = int(value.split("/", 1)[0].strip())
    except ValueError:
        return None
    return number if number > 0 else None


def _identity(title: str, artist: str) -> tuple[str, str]:
    return title.strip().casefold(), artist.strip().casefold()


def _infer_deftones_title(filename: str) -> str:
    title = Path(filename).stem.strip()
    title = re.sub(r"^Deftones\s*[–—-]\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+-\s*Deftones$", "", title, flags=re.IGNORECASE)
    title = re.sub(
        r"\s+-\s*My Music Harbourage$", "", title, flags=re.IGNORECASE
    )
    return title.strip()


def _read_track(path: Path, source_root: Path, from_sd: bool = False) -> PlannedTrack:
    audio = MutagenFile(path, easy=True)
    tags = audio.tags if audio else None
    title = _tag_text(tags, "title")
    artist = _tag_text(tags, "artist")
    album = _tag_text(tags, "album")
    inferred = False

    relative = path.relative_to(source_root)
    source_artist = relative.parts[0] if relative.parts else path.parent.name
    persona = (
        source_artist.casefold() == "persona"
        or (from_sd and source_artist == "02")
    )

    if not title or not artist:
        if source_artist.casefold() != "deftones":
            raise ValueError(
                f"Faltan etiquetas de título/artista y no se puede inferir con "
                f"seguridad: {path}"
            )
        title = title or _infer_deftones_title(path.name)
        artist = artist or "Deftones"
        album = None
        inferred = True

    if not title or not artist:
        raise ValueError(f"No se pudo determinar título y artista: {path}")

    clear_album = persona and album and album.casefold() == title.casefold()
    if clear_album:
        album = None

    if from_sd and persona and title.casefold() == "school days":
        artist = "ATLUS GAME MUSIC"

    group_artist = "Persona" if persona else source_artist
    if from_sd and not persona:
        group_artist = artist

    return PlannedTrack(
        source=path,
        title=title,
        artist=artist,
        album=album,
        group_artist=group_artist,
        persona=persona,
        from_sd=from_sd,
        track_number=_tag_number(tags, "tracknumber"),
        disc_number=_tag_number(tags, "discnumber"),
        infer_tags=inferred,
        clear_album=bool(clear_album),
    )


def build_plan(source_root: Path, sd_root: Path) -> list[PlannedTrack]:
    source_root = source_root.resolve()
    sd_root = sd_root.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"No se encontró la carpeta de música: {source_root}")
    if not sd_root.is_dir():
        raise FileNotFoundError(f"No se encontró la raíz de la microSD: {sd_root}")

    source_files = sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".mp3"
    )
    if not source_files:
        raise ValueError(f"No hay MP3 en {source_root}")

    source_tracks = [_read_track(path, source_root) for path in source_files]
    source_identities = {_identity(track.title, track.artist) for track in source_tracks}
    current_files = sorted(
        path
        for path in sd_root.rglob("*")
        if path.is_file()
        and path.suffix.casefold() == ".mp3"
        and len(path.relative_to(sd_root).parts) == 2
        and re.fullmatch(r"[0-9]{2}", path.relative_to(sd_root).parts[0])
        and re.fullmatch(r"[0-9]{3}\.mp3", path.name, re.IGNORECASE)
    )
    current_tracks = [_read_track(path, sd_root, from_sd=True) for path in current_files]
    current_identities = {_identity(track.title, track.artist) for track in current_tracks}
    current_persona_titles = {
        track.title.casefold() for track in current_tracks if track.persona
    }

    tracks = []
    for track in source_tracks:
        if track.persona and track.title.casefold() in current_persona_titles:
            continue
        tracks.append(track)

    for track in current_tracks:
        identity = _identity(track.title, track.artist)
        if identity in source_identities and not track.persona:
            continue
        tracks.append(track)

    persona_tracks = [track for track in tracks if track.persona]
    existing_persona = [
        track
        for track in persona_tracks
        if track.source.is_relative_to(sd_root)
    ]
    used_persona_numbers = set()
    for track in existing_persona:
        relative = track.source.relative_to(sd_root).as_posix()
        match = re.fullmatch(r"02/([0-9]{3})\.mp3", relative, re.IGNORECASE)
        if not match:
            raise ValueError(f"Ruta Persona actual incompatible con DFPlayer: {relative}")
        track.folder_number = 2
        track.file_number = int(match.group(1))
        if not 1 <= track.file_number <= 255 or track.file_number in used_persona_numbers:
            raise ValueError(f"Número de pista Persona duplicado o fuera de rango: {relative}")
        used_persona_numbers.add(track.file_number)

    next_persona_number = 1
    new_persona = sorted(
        (track for track in persona_tracks if track not in existing_persona),
        key=lambda track: (track.title.casefold(), track.source.name.casefold()),
    )
    for track in new_persona:
        while next_persona_number in used_persona_numbers:
            next_persona_number += 1
        if next_persona_number > 255:
            raise ValueError("La carpeta Persona supera el límite de 255 canciones.")
        track.folder_number = 2
        track.file_number = next_persona_number
        used_persona_numbers.add(next_persona_number)
        next_persona_number += 1

    groups: dict[tuple[str, str], list[PlannedTrack]] = {}
    for track in tracks:
        if track.persona:
            continue
        key = (track.group_artist.strip(), (track.album or "").strip())
        groups.setdefault(key, []).append(track)

    available_folders = [number for number in range(1, 100) if number != 2]
    if len(groups) > len(available_folders):
        raise ValueError(
            f"Hay {len(groups)} grupos no-Persona, más que las carpetas DFPlayer disponibles."
        )
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (item[0][0].casefold(), item[0][1].casefold()),
    )
    for folder_number, (_, album_tracks) in zip(available_folders, ordered_groups):
        if len(album_tracks) > 255:
            raise ValueError(
                f"El grupo {album_tracks[0].group_artist} / "
                f"{album_tracks[0].album or 'Sin álbum'} supera 255 pistas."
            )
        ordered_tracks = sorted(
            album_tracks,
            key=lambda track: (
                track.disc_number or 1,
                track.track_number or 9999,
                track.title.casefold(),
                track.source.name.casefold(),
            ),
        )
        for file_number, track in enumerate(ordered_tracks, 1):
            track.folder_number = folder_number
            track.file_number = file_number

    paths = [track.sd_path.casefold() for track in tracks]
    if len(paths) != len(set(paths)):
        raise ValueError("El plan asigna más de una canción a la misma ruta DFPlayer.")
    if any(parse_sd_path(track.sd_path) is None for track in tracks):
        raise ValueError("El plan contiene una ruta no válida para DFPlayer.")
    return sorted(tracks, key=lambda track: (track.folder_number, track.file_number))


def _write_tag_overrides(path: Path, track: PlannedTrack) -> None:
    normalize_school_days = (
        track.from_sd and track.persona and track.title.casefold() == "school days"
    )
    if not track.infer_tags and not track.clear_album and not normalize_school_days:
        return
    try:
        tags = EasyID3(str(path))
    except ID3NoHeaderError:
        tags = EasyID3()

    if track.infer_tags or normalize_school_days:
        tags["title"] = [track.title]
        tags["artist"] = [track.artist]
    if track.clear_album and "album" in tags:
        del tags["album"]
    tags.save(path)


def _write_manifest(sd_root: Path, tracks: list[PlannedTrack]) -> None:
    groups: dict[int, list[PlannedTrack]] = {}
    for track in tracks:
        groups.setdefault(track.folder_number, []).append(track)
    manifest_path = sd_root / "MP3-library.csv"
    with manifest_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["folder", "group", "album", "track_count", "example_tracks"])
        for folder_number, items in sorted(groups.items()):
            first = items[0]
            group = "Persona" if first.persona else first.group_artist
            album = "" if first.persona else (first.album or "Sin álbum")
            examples = "; ".join(track.title for track in items[:8])
            writer.writerow(
                [f"{folder_number:02d}", group, album, len(items), examples]
            )


def apply_plan(
    sd_root: Path,
    tracks: list[PlannedTrack],
    settings: Settings,
) -> tuple[int, int]:
    sd_root = sd_root.resolve()
    numbered_directories = [
        path
        for path in sd_root.iterdir()
        if path.is_dir() and re.fullmatch(r"[0-9]{2}", path.name)
    ]
    unexpected_files = [
        path
        for directory in numbered_directories
        for path in directory.rglob("*")
        if path.is_file()
        and (
            path.parent != directory
            or not re.fullmatch(r"[0-9]{3}\.mp3", path.name, re.IGNORECASE)
        )
    ]
    if unexpected_files:
        raise ValueError(
            "Se encontraron archivos fuera del formato DD/DDD.mp3 en carpetas "
            f"numéricas; no se tocaron: {unexpected_files[0]}"
        )

    required_bytes = sum(track.source.stat().st_size for track in tracks)
    free_bytes = shutil.disk_usage(sd_root).free
    if required_bytes > free_bytes:
        raise OSError(
            f"No hay espacio suficiente: se necesitan {required_bytes} bytes "
            f"y hay {free_bytes} bytes libres."
        )

    client = MongoClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
    )
    suffix = uuid.uuid4().hex[:12]
    stage_root = sd_root / f".__mp3ai_stage_{suffix}"
    backup_root = sd_root / f".__mp3ai_backup_{suffix}"
    moved_old_directories = []
    installed_new_directories = []
    database_reindexed = False
    try:
        client.admin.command("ping")
        collection = client[settings.mongo_db][settings.mongo_collection]
        identities = {_identity(track.title, track.artist) for track in tracks}
        snapshot_query = {
            "$or": [
                {
                    "title": {"$regex": f"^{re.escape(title)}$", "$options": "i"},
                    "artist": {"$regex": f"^{re.escape(artist)}$", "$options": "i"},
                }
                for title, artist in identities
            ]
            + [
                {
                    "sd_path": {
                        "$regex": r"^/[0-9]{2}/[0-9]{3}\.mp3$",
                        "$options": "i",
                    }
                }
            ]
        }
        original_documents = list(collection.find(snapshot_query))

        stage_root.mkdir()
        for track in tracks:
            destination = stage_root / track.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(track.source, destination)
            _write_tag_overrides(destination, track)
            if not destination.is_file() or destination.stat().st_size == 0:
                raise OSError(f"No se pudo verificar el archivo copiado: {destination}")

        staged_count = sum(
            1 for path in stage_root.rglob("*.mp3") if path.is_file()
        )
        if staged_count != len(tracks):
            raise OSError(
                f"La cantidad de MP3 copiados ({staged_count}) no coincide "
                f"con el plan ({len(tracks)})."
            )

        backup_root.mkdir()
        for directory in numbered_directories:
            backup = backup_root / directory.name
            directory.rename(backup)
            moved_old_directories.append((backup, directory))

        for directory in sorted(path for path in stage_root.iterdir() if path.is_dir()):
            destination = sd_root / directory.name
            directory.rename(destination)
            installed_new_directories.append(destination)

        collection.update_many(
            {"sd_path": {"$regex": r"^/[0-9]{2}/[0-9]{3}\.mp3$", "$options": "i"}},
            {"$unset": {"sd_path": "", "dfplayer_folder": "", "dfplayer_track": ""}},
        )
        indexed, skipped = index_sd_card(sd_root, collection)
        if indexed != len(tracks) or skipped:
            raise RuntimeError(
                f"Indexación incompleta: indexadas={indexed}, omitidas={skipped}, "
                f"esperadas={len(tracks)}."
            )

        expected_paths = [track.sd_path for track in tracks]
        documents = list(
            collection.find(
                {"sd_path": {"$in": expected_paths}},
                {"title": 1, "artist": 1, "sd_path": 1},
            )
        )
        by_path: dict[str, list[dict]] = {}
        for document in documents:
            by_path.setdefault(document["sd_path"], []).append(document)
        for track in tracks:
            matches = by_path.get(track.sd_path, [])
            if not any(
                str(document.get("title", "")).strip().casefold()
                == track.title.casefold()
                and str(document.get("artist", "")).strip().casefold()
                == track.artist.casefold()
                for document in matches
            ):
                raise RuntimeError(
                    f"No se verificó en MongoDB la ruta {track.sd_path} "
                    f"({track.title} — {track.artist})."
                )

        _write_manifest(sd_root, tracks)
        database_reindexed = True
    except Exception:
        if "collection" in locals() and "original_documents" in locals():
            original_ids = [document["_id"] for document in original_documents]
            collection.delete_many(
                {
                    "_id": {"$nin": original_ids},
                    "sd_path": {
                        "$regex": r"^/[0-9]{2}/[0-9]{3}\.mp3$",
                        "$options": "i",
                    },
                }
            )
            for document in original_documents:
                collection.replace_one(
                    {"_id": document["_id"]}, document, upsert=True
                )
        if not database_reindexed:
            for directory in installed_new_directories:
                if directory.exists():
                    shutil.rmtree(directory)
            for backup, original in moved_old_directories:
                if backup.exists():
                    backup.rename(original)
        raise
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root)
        if database_reindexed and backup_root.exists():
            shutil.rmtree(backup_root)
        client.close()

    return indexed, skipped


def prepare_output(output_root: Path, tracks: list[PlannedTrack]) -> None:
    output_root = output_root.expanduser().resolve()
    if output_root.exists():
        raise FileExistsError(
            f"La carpeta de salida ya existe; no se sobrescribió: {output_root}"
        )
    output_root.parent.mkdir(parents=True, exist_ok=True)

    stage_root = output_root.with_name(
        f".{output_root.name}.staging-{uuid.uuid4().hex[:12]}"
    )
    try:
        stage_root.mkdir()
        for track in tracks:
            destination = stage_root / track.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(track.source, destination)
            _write_tag_overrides(destination, track)
            if not destination.is_file() or destination.stat().st_size == 0:
                raise OSError(f"No se pudo verificar el archivo preparado: {destination}")

            audio = MutagenFile(destination, easy=True)
            tags = audio.tags if audio else None
            if (
                _tag_text(tags, "title") != track.title
                or _tag_text(tags, "artist") != track.artist
            ):
                raise OSError(
                    f"Las etiquetas de la copia no coinciden con el plan: {destination}"
                )

        prepared_count = sum(
            1 for path in stage_root.rglob("*.mp3") if path.is_file()
        )
        if prepared_count != len(tracks):
            raise OSError(
                f"Se prepararon {prepared_count} MP3; se esperaban {len(tracks)}."
            )

        _write_manifest(stage_root, tracks)
        instructions = (
            "COPIAR A LA MICROSD\n"
            "====================\n\n"
            "1. Conecta la microSD y abre su unidad en el Explorador de archivos.\n"
            "2. En la raiz de la microSD, elimina solo las carpetas numericas "
            "01, 02 y 03 que ya existen. No borres 'System Volume Information' "
            "ni otros archivos.\n"
            "3. Abre esta carpeta, selecciona las carpetas numericas 01 a 37 "
            "y copialas a la raiz de la microSD (no copies esta carpeta contenedora).\n"
            "4. Copia MP3-library.csv si quieres tener el indice en la tarjeta; "
            "no es necesario para reproducir.\n"
            "5. Espera que termine la copia y expulsa la tarjeta con seguridad.\n\n"
            f"Contenido: {len(tracks)} canciones en "
            f"{len({track.folder_number for track in tracks})} carpetas numericas.\n"
            "Los originales de Descargas no se modificaron.\n"
            "Antes de copiar, comprueba que Windows detecte la tarjeta sin errores. "
            "Si aparece un aviso de reparacion o error de hardware, no copies aun.\n\n"
            "PARA ACTUALIZAR MONGODB DESPUES DE COPIAR:\n"
            "Abre PowerShell y ejecuta:\n"
            r"Set-Location 'C:\Users\Admin\Documents\GitHub\MP3-AI-Player\proyecto_telegram_ai_music'"
            "\n"
            ".\\.venv\\Scripts\\python.exe -m scripts.index_sd_card --sd-root D:\\"
            "\n"
            "Si la microSD tiene otra letra, reemplaza D:\\ por la letra correcta.\n"
        )
        (stage_root / "LEEME.txt").write_text(instructions, encoding="utf-8")
        stage_root.rename(output_root)
    except Exception:
        if stage_root.exists():
            shutil.rmtree(stage_root)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Organiza una biblioteca MP3 en carpetas DFPlayer por álbum y actualiza MongoDB."
        )
    )
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--sd-root", required=True, type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--apply",
        action="store_true",
        help="Copia la biblioteca y reemplaza las carpetas numéricas existentes.",
    )
    action.add_argument(
        "--prepare-output",
        type=Path,
        metavar="CARPETA",
        help="Prepara las carpetas DFPlayer en una carpeta local sin modificar la microSD.",
    )
    args = parser.parse_args()

    tracks = build_plan(args.source_root, args.sd_root)
    groups: dict[int, list[PlannedTrack]] = {}
    for track in tracks:
        groups.setdefault(track.folder_number, []).append(track)
    print(
        f"Plan: {len(tracks)} canciones, {len(groups)} carpetas, "
        f"{sum(track.source.stat().st_size for track in tracks) / 1024**3:.2f} GiB."
    )
    for folder_number, items in sorted(groups.items()):
        first = items[0]
        label = "Persona" if first.persona else first.group_artist
        album = "" if first.persona else f" — {first.album or 'Sin álbum'}"
        print(f"{folder_number:02d}: {label}{album} ({len(items)} canciones)")

    if args.prepare_output:
        prepare_output(args.prepare_output, tracks)
        print(f"Carpeta lista para copiar: {args.prepare_output.resolve()}")
        return

    if not args.apply:
        print("Vista previa solamente; no se modificó la tarjeta ni MongoDB.")
        return

    settings = Settings.from_env()
    indexed, skipped = apply_plan(args.sd_root, tracks, settings)
    print(
        f"Importación finalizada en {settings.mongo_db}.{settings.mongo_collection}: "
        f"{indexed} canciones indexadas, {skipped} omitidas."
    )
    print(f"Manifiesto: {args.sd_root.resolve() / 'MP3-library.csv'}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
