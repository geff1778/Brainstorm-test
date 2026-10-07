"""Learn-mode routes: generate quizzes from text or a URL."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..errors import ValidationError
from ..services import achievements, learn
from ..validation import as_int, as_str, validate_url
from .helpers import ai_limiter, current_username, rate_limit

bp = Blueprint("learn", __name__, url_prefix="/api/learn")


def _award_learn_achievement() -> None:
    username = current_username()
    if username:
        achievements.unlock(username, "learn_mode")


def _generate(content: str, num: int):
    questions = learn.generate_learn_questions(content, num)
    if not questions:
        raise ValidationError("Не удалось сгенерировать вопросы. Попробуй ещё раз.")
    _award_learn_achievement()
    return jsonify({"questions": questions, "count": len(questions)})


@bp.post("/from_text")
@rate_limit(ai_limiter, key_prefix="learn_text")
def from_text():
    """Generate questions from pasted text."""
    body = request.get_json(silent=True) or {}
    content = as_str(body.get("content"), strip=False)
    num = as_int(body.get("num_questions", 6), default=6, minimum=1, maximum=learn.MAX_QUESTIONS)
    if len(content.strip()) < learn.MIN_CONTENT_LEN:
        raise ValidationError(f"Текст слишком короткий (мин. {learn.MIN_CONTENT_LEN} символов)")
    return _generate(content, num)


@bp.post("/from_url")
@rate_limit(ai_limiter, key_prefix="learn_url")
def from_url():
    """Fetch a page and generate questions from its text."""
    body = request.get_json(silent=True) or {}
    url = validate_url(body.get("url"))
    num = as_int(body.get("num_questions", 6), default=6, minimum=1, maximum=learn.MAX_QUESTIONS)
    ok, text = learn.extract_text_from_url(url)
    if not ok:
        raise ValidationError(text)
    if len(text) < learn.MIN_CONTENT_LEN:
        raise ValidationError("Страница содержит слишком мало текста")
    return _generate(text, num)
