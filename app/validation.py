"""Input validation helpers.

Every value that crosses a trust boundary (HTTP JSON body, socket payload,
query string) is normalised here.  The functions raise
:class:`~app.errors.ValidationError` on bad input so callers can respond with a
consistent 400 instead of leaking a ``ValueError`` traceback.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ValidationError

USERNAME_RE = re.compile(r"^[\w\-а-яёА-ЯЁ ]+$", re.UNICODE)

MAX_USERNAME_LEN = 20
MIN_USERNAME_LEN = 2
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128
MAX_QUESTION_LEN = 300
MAX_OPTION_LEN = 120
MAX_CHAT_LEN = 200
MAX_TOPIC_LEN = 60
MAX_URL_LEN = 2048

DIFFICULTIES = ("easy", "medium", "hard")
GAME_MODES = ("classic", "ffa", "team", "lives", "coop", "svoyaigra")


def as_str(value: Any, *, default: str = "", strip: bool = True, max_len: int | None = None) -> str:
    """Coerce ``value`` to a safe string."""
    if value is None:
        text = default
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    if strip:
        text = text.strip()
    if max_len is not None:
        text = text[:max_len]
    return text


def as_int(value: Any, *, default: int | None = None, minimum: int | None = None,
           maximum: int | None = None, field: str = "value") -> int:
    """Coerce ``value`` to an int inside the given bounds."""
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        if default is None:
            raise ValidationError(f"{field} must be an integer") from exc
        result = default
    if minimum is not None and result < minimum:
        result = minimum
    if maximum is not None and result > maximum:
        result = maximum
    return result


def as_bool(value: Any, *, default: bool = False) -> bool:
    """Coerce common truthy representations to a bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def validate_username(value: Any) -> str:
    """Validate a user/pseudo-nick name."""
    name = as_str(value, max_len=MAX_USERNAME_LEN)
    if len(name) < MIN_USERNAME_LEN:
        raise ValidationError(f"Имя слишком короткое (мин. {MIN_USERNAME_LEN} символа)")
    if not USERNAME_RE.match(name):
        raise ValidationError("Недопустимые символы в имени")
    return name


def validate_password(value: Any) -> str:
    """Validate a password (length only; hashing happens elsewhere)."""
    password = value if isinstance(value, str) else as_str(value, strip=False)
    if len(password) < MIN_PASSWORD_LEN:
        raise ValidationError(f"Пароль слишком короткий (мин. {MIN_PASSWORD_LEN} символов)")
    if len(password) > MAX_PASSWORD_LEN:
        raise ValidationError("Пароль слишком длинный")
    return password


def validate_difficulty(value: Any, *, default: str = "medium") -> str:
    """Validate a difficulty key, falling back to ``default``."""
    candidate = as_str(value).lower()
    return candidate if candidate in DIFFICULTIES else default


def validate_mode(value: Any, *, default: str = "classic") -> str:
    """Validate a game mode key, falling back to ``default``."""
    candidate = as_str(value).lower()
    return candidate if candidate in GAME_MODES else default


def validate_options(options: Any, *, min_count: int = 2, max_count: int = 6) -> list[str]:
    """Validate and normalise a list of answer options."""
    if not isinstance(options, (list, tuple)):
        raise ValidationError("options must be a list")
    cleaned = [as_str(o, max_len=MAX_OPTION_LEN) for o in options]
    cleaned = [o for o in cleaned if o]
    if not (min_count <= len(cleaned) <= max_count):
        raise ValidationError(f"Неверное количество вариантов ({min_count}-{max_count})")
    lowered = [o.lower() for o in cleaned]
    if len(set(lowered)) != len(lowered):
        raise ValidationError("Варианты ответов должны быть уникальными")
    return cleaned


def validate_url(value: Any) -> str:
    """Validate an http(s) URL used by the learn mode."""
    url = as_str(value, max_len=MAX_URL_LEN)
    if not url.lower().startswith(("http://", "https://")):
        raise ValidationError("Некорректный URL")
    return url


def clamp(value: int, minimum: int, maximum: int) -> int:
    """Clamp ``value`` into ``[minimum, maximum]``."""
    return max(minimum, min(maximum, value))


def paginate_args(args: dict, *, default_limit: int = 50, max_limit: int = 200) -> tuple[int, int]:
    """Parse ``limit``/``offset`` style query parameters."""
    limit = as_int(args.get("limit", args.get("n", default_limit)), default=default_limit,
                   minimum=1, maximum=max_limit, field="limit")
    offset = as_int(args.get("offset", 0), default=0, minimum=0, field="offset")
    return limit, offset
