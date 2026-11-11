from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

REPORT_TIMEZONE = timezone(timedelta(hours=-3), "ART")


def report_period_bounds(period: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    if period not in {"daily", "weekly"}:
        raise ValueError(f"Período de reporte no válido: {period}")
    local_now = (now or datetime.now(timezone.utc)).astimezone(REPORT_TIMEZONE)
    today = local_now.date()
    start_date = today if period == "daily" else today - timedelta(days=today.weekday())
    end_date = start_date + timedelta(days=1 if period == "daily" else 7)
    start = datetime.combine(start_date, time.min, REPORT_TIMEZONE).astimezone(timezone.utc)
    end = datetime.combine(end_date, time.min, REPORT_TIMEZONE).astimezone(timezone.utc)
    return start, end


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _overlap_seconds(
    interval_start: datetime,
    interval_end: datetime,
    window_start: datetime,
    window_end: datetime,
) -> float:
    start = max(interval_start, window_start)
    end = min(interval_end, window_end)
    return max(0.0, (end - start).total_seconds())


def build_playback_report(
    sessions: list[dict],
    period: str,
    now: datetime | None = None,
) -> dict:
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    start, end = report_period_bounds(period, now_utc)
    window_end = min(end, now_utc)
    song_totals: dict[tuple[str, str], dict] = {}
    genre_seconds: dict[str, float] = defaultdict(float)
    total_seconds = 0.0
    play_count = 0

    for session in sessions:
        started = _as_utc(session["started_at"])
        ended = _as_utc(session["ended_at"]) if session.get("ended_at") else now_utc
        if ended <= start or started >= window_end:
            continue
        interval_start = max(started, start)
        interval_end = min(ended, window_end)
        if interval_end <= interval_start:
            continue

        listened = _overlap_seconds(interval_start, interval_end, start, window_end)
        pause_intervals = list(session.get("pause_intervals") or [])
        if session.get("pause_started_at"):
            pause_intervals.append(
                {"started_at": session["pause_started_at"], "ended_at": now_utc}
            )
        for pause in pause_intervals:
            pause_start = _as_utc(pause["started_at"])
            pause_end = _as_utc(pause["ended_at"])
            listened -= _overlap_seconds(
                max(interval_start, pause_start),
                min(interval_end, pause_end),
                start,
                window_end,
            )
        listened = max(0.0, listened)

        duration = session.get("duration_sec")
        if isinstance(duration, (int, float)) and duration > 0:
            elapsed_before_window = max(
                0.0, (interval_start - started).total_seconds()
            )
            pauses_before_window = sum(
                _overlap_seconds(
                    started,
                    interval_start,
                    _as_utc(pause["started_at"]),
                    _as_utc(pause["ended_at"]),
                )
                for pause in pause_intervals
            )
            already_listened = max(0.0, elapsed_before_window - pauses_before_window)
            listened = min(listened, max(0.0, duration - already_listened))

        if listened <= 0:
            continue

        play_count += 1
        total_seconds += listened
        identity = (str(session.get("song_id", "")), str(session.get("sd_path", "")))
        song = song_totals.setdefault(
            identity,
            {
                "title": session.get("title") or "Sin título",
                "artist": session.get("artist") or "Artista desconocido",
                "plays": 0,
                "seconds": 0.0,
            },
        )
        song["plays"] += 1
        song["seconds"] += listened

        genres = session.get("genre") or []
        if isinstance(genres, str):
            genres = [genres]
        elif not isinstance(genres, (list, tuple, set)):
            genres = []
        for genre in {str(value).strip() for value in genres if str(value).strip()}:
            genre_seconds[genre] += listened

    top_songs = sorted(
        song_totals.values(),
        key=lambda song: (-song["plays"], -song["seconds"], song["title"].casefold()),
    )[:5]
    top_genres = sorted(
        genre_seconds.items(), key=lambda item: (-item[1], item[0].casefold())
    )[:3]
    return {
        "period": period,
        "start": start,
        "end": end,
        "play_count": play_count,
        "total_seconds": total_seconds,
        "top_songs": top_songs,
        "top_genres": top_genres,
    }


class PlaybackHistory:
    def __init__(self, collection):
        self.collection = collection

    def start(self, song: dict, user_id: int) -> None:
        now = datetime.now(timezone.utc)
        active = self.collection.find_one(
            {"ended_at": {"$exists": False}},
            sort=[("started_at", -1)],
        )
        if active:
            self.collection.update_one(
                {"_id": active["_id"], "ended_at": {"$exists": False}},
                {"$set": {"ended_at": now, "end_reason": "next_track"}},
            )

        genre = song.get("genre") or []
        if isinstance(genre, str):
            genre = [genre]
        elif not isinstance(genre, (list, tuple, set)):
            genre = []
        document = {
            "song_id": str(song["_id"]),
            "title": song.get("title") or "Sin título",
            "artist": song.get("artist") or "Artista desconocido",
            "album": song.get("album"),
            "genre": genre,
            "sd_path": song["sd_path"],
            "duration_sec": song.get("duration_sec"),
            "user_id": user_id,
            "started_at": now,
            "pause_intervals": [],
        }
        self.collection.insert_one(document)

    def pause(self) -> bool:
        now = datetime.now(timezone.utc)
        active = self.collection.find_one(
            {
                "ended_at": {"$exists": False},
                "pause_started_at": {"$exists": False},
            },
            sort=[("started_at", -1)],
        )
        if not active:
            return False
        result = self.collection.update_one(
            {
                "_id": active["_id"],
                "ended_at": {"$exists": False},
                "pause_started_at": {"$exists": False},
            },
            {"$set": {"pause_started_at": now}},
        )
        return result.modified_count == 1

    def resume(self) -> bool:
        now = datetime.now(timezone.utc)
        active = self.collection.find_one(
            {
                "ended_at": {"$exists": False},
                "pause_started_at": {"$exists": True},
            },
            sort=[("started_at", -1)],
        )
        if not active:
            return False
        result = self.collection.update_one(
            {
                "_id": active["_id"],
                "ended_at": {"$exists": False},
                "pause_started_at": active["pause_started_at"],
            },
            {
                "$push": {
                    "pause_intervals": {
                        "started_at": active["pause_started_at"],
                        "ended_at": now,
                    }
                },
                "$unset": {"pause_started_at": ""},
            },
        )
        return result.modified_count == 1

    def report(self, period: str, now: datetime | None = None) -> dict:
        start, end = report_period_bounds(period, now)
        query = {
            "started_at": {"$lt": end},
            "$or": [
                {"ended_at": {"$gt": start}},
                {"ended_at": {"$exists": False}},
            ],
        }
        sessions = list(self.collection.find(query))
        return build_playback_report(sessions, period, now)
