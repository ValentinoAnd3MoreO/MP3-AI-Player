import re
import unicodedata
from collections.abc import Callable


def _normalize_title(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[^\W_]+", without_accents, flags=re.UNICODE))


class PlaybackQueue:
    REPEAT_MODES = ("off", "all", "one")

    def __init__(self) -> None:
        self._songs: list[dict] = []
        self._current_index: int | None = None
        self.repeat_mode = "off"

    @property
    def current(self) -> dict | None:
        if self._current_index is None:
            return None
        return self._songs[self._current_index]

    @property
    def upcoming(self) -> list[dict]:
        if self._current_index is None:
            return []
        return self._songs[self._current_index + 1 :]

    @property
    def songs(self) -> list[dict]:
        if self._current_index is None:
            return []
        return self._songs[self._current_index :]

    def restore(self, songs: list[dict], repeat_mode: str = "off") -> None:
        if repeat_mode not in self.REPEAT_MODES:
            raise ValueError(f"Modo de repetición no válido: {repeat_mode}")
        self._songs = list(songs)
        self._current_index = 0 if self._songs else None
        self.repeat_mode = repeat_mode

    def enqueue(self, songs: list[dict]) -> dict | None:
        if not songs:
            return None
        was_empty = not self._songs or self._current_index is None
        if was_empty:
            self._songs = []
            self._current_index = None
        self._songs.extend(songs)
        if was_empty:
            self._current_index = 0
        return self.current

    def peek_next(self, manual: bool = True) -> dict | None:
        current = self.current
        if current is None:
            return None
        if self.repeat_mode == "one" and not manual:
            return current
        next_index = self._current_index + 1
        if next_index < len(self._songs):
            return self._songs[next_index]
        if self.repeat_mode == "all":
            return self._songs[0]
        return None

    def find_upcoming(self, title: str) -> list[dict]:
        requested = _normalize_title(title)
        if not requested or self._current_index is None:
            return []
        matches = []
        for song in self.upcoming:
            song_title = _normalize_title(str(song.get("title") or ""))
            if (
                song_title == requested
                or song_title.startswith(requested + " ")
                or f" {requested} " in f" {song_title} "
            ):
                matches.append(song)
        return matches

    def jump_to(self, song: dict) -> dict:
        if self._current_index is None:
            raise ValueError("No hay una canción actual en la cola.")
        target_index = next(
            (
                index
                for index in range(self._current_index + 1, len(self._songs))
                if self._songs[index] is song
            ),
            None,
        )
        if target_index is None:
            raise ValueError("La canción elegida ya no está en la cola futura.")
        self._songs = [self._songs[target_index]] + self._songs[target_index + 1 :]
        self._current_index = 0
        return self.current

    def retain_upcoming(
        self, predicate: Callable[[dict], bool]
    ) -> tuple[int, int]:
        current = self.current
        if current is None:
            return 0, 0
        upcoming = self.upcoming
        retained = [song for song in upcoming if predicate(song)]
        removed_count = len(upcoming) - len(retained)
        self._songs = [current] + retained
        self._current_index = 0
        return removed_count, len(retained)

    def next(self, manual: bool = True) -> dict | None:
        current = self.current
        if current is None:
            return None
        if self.repeat_mode == "one" and not manual:
            return current
        next_index = self._current_index + 1
        if next_index < len(self._songs):
            self._current_index = next_index
            return self.current
        if self.repeat_mode == "all":
            self._current_index = 0
            return self.current
        else:
            self.clear()
            return None

    def cycle_repeat_mode(self) -> str:
        current_index = self.REPEAT_MODES.index(self.repeat_mode)
        self.repeat_mode = self.REPEAT_MODES[
            (current_index + 1) % len(self.REPEAT_MODES)
        ]
        return self.repeat_mode

    def clear(self) -> None:
        self._songs.clear()
        self._current_index = None
