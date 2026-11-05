import re

_SD_PATH_PATTERN = re.compile(r"^/([0-9]{2})/([0-9]{3})\.mp3$", re.IGNORECASE)


def parse_sd_path(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    match = _SD_PATH_PATTERN.fullmatch(value.strip())
    if not match:
        return None
    folder, track = (int(part) for part in match.groups())
    if not 1 <= folder <= 99 or not 1 <= track <= 255:
        return None
    return folder, track
