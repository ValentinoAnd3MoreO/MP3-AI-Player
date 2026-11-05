from pathlib import Path

from app.config import Settings


class MusicLibrary:
    def __init__(self, settings: Settings):
        self.root = settings.music_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, song: dict) -> Path | None:
        relative_path = song.get("path")
        if not isinstance(relative_path, str) or not relative_path.strip():
            return None

        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        if not candidate.is_file() or candidate.suffix.lower() != ".mp3":
            return None
        return candidate

    def has_audio(self, song: dict) -> bool:
        return self.resolve(song) is not None
