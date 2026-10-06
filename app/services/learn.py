"""Learn mode: turn arbitrary text or a web page into a quiz.

The service is deliberately small and dependency-light.  It caps the amount of
content sent to the model (token budget) and strips navigation/boilerplate so
the generated questions stay on topic.
"""

from __future__ import annotations

from ..errors import ValidationError
from ..logging_setup import get_logger
from . import ai_client

logger = get_logger(__name__)

MAX_CONTENT_LEN = 4000
MAX_QUESTIONS = 8
MIN_CONTENT_LEN = 50
REQUEST_TIMEOUT = 8
MAX_RESPONSE_BYTES = 2_000_000

_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript", "iframe", "form")
_LEARN_SYSTEM_PROMPT = (
    "Ты — эксперт по созданию учебных тестов. "
    "Отвечай ТОЛЬКО валидным JSON без markdown, без пояснений. "
    "Поле 'correct' — целое число (индекс с 0). "
    "Поле 'explanation' — 1 предложение."
)


def extract_text_from_url(url: str) -> tuple[bool, str]:
    """Fetch ``url`` and return ``(ok, text_or_error)``.

    Only the textual content is returned; scripts, styles and layout chrome are
    removed.  Response size is bounded to avoid memory exhaustion.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover
        return False, f"Модуль недоступен: {exc}"

    try:
        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BrainStorm/2.0)"},
            stream=True,
        )
        response.raise_for_status()
        raw = response.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
        if len(raw) > MAX_RESPONSE_BYTES:
            return False, "Страница слишком большая"
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(_STRIP_TAGS):
            tag.decompose()
        lines = [line.strip() for line in soup.get_text(separator="\n").splitlines() if line.strip()]
        return True, "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 - reported to the user
        logger.warning("extract_text_from_url(%s): %s", url, exc)
        return False, f"Ошибка загрузки: {exc}"


def prepare_content(raw_text: str) -> str:
    """Collapse whitespace and truncate the content to the token budget."""
    import re

    text = re.sub(r"\n{3,}", "\n\n", (raw_text or "").strip())
    text = re.sub(r"[ \t]{2,}", " ", text)
    if len(text) > MAX_CONTENT_LEN:
        text = text[:MAX_CONTENT_LEN] + "..."
    return text


def build_learn_prompt(content: str, num: int) -> str:
    """Build the prompt that asks for questions grounded in ``content``."""
    return (
        f"На основе следующего текста создай РОВНО {num} вопросов для викторины.\n"
        "Каждый вопрос должен иметь РОВНО 4 варианта ответа.\n"
        "Ответ только в формате JSON без markdown:\n"
        '{"questions":[{"question":"...","options":["A","B","C","D"],'
        '"correct":0,"explanation":"..."}]}\n\n'
        f"Текст:\n{content}\n\n"
        f"Создай {num} вопросов. Только JSON:"
    )


def generate_learn_questions(content: str, num: int = 6) -> list[dict]:
    """Generate quiz questions from ``content``.

    Raises
    ------
    ValidationError
        When the content is too short to build meaningful questions.
    """
    content = prepare_content(content)
    if len(content) < MIN_CONTENT_LEN:
        raise ValidationError(f"Текст слишком короткий (мин. {MIN_CONTENT_LEN} символов)")

    num = max(1, min(int(num), MAX_QUESTIONS))
    try:
        raw = ai_client.call_gigachat(build_learn_prompt(content, num), system=_LEARN_SYSTEM_PROMPT)
    except ai_client.AIUnavailableError as exc:
        logger.warning("learn generation failed: %s", exc)
        return []
    return ai_client.parse_questions_json(raw, num_options=4)[:num]
