"""GigaChat question generation service.

Responsibilities
----------------
* Build prompts for question and hint generation.
* Call the GigaChat API (with a small retry policy).
* Parse and *normalise* whatever the model returns into a strict question
  schema: ``{"question", "options", "correct", "explanation", "hint", "bonus"}``.
* Fall back to a curated local question bank when the API is unavailable.

Fixes over the original implementation
--------------------------------------
* Batch index conversion no longer corrupts answers.  The original
  ``_fix_indexing_batch`` unconditionally shifted every ``correct`` down by one
  whenever the minimum was ``>= 1`` and the maximum equalled ``num_options`` —
  turning a correct 0-based ``2`` into ``1``.  The rewrite only converts when
  *every* index lies in ``1..n`` **and** at least one equals ``n``.
* Deduplication no longer uses a process-wide ``_session_hashes`` set that made
  every room after the first see an ever-shrinking question pool.  Deduplication
  is now scoped to the current call.
* ``used_questions`` is honoured by the fallback path too, so repeated games do
  not replay the identical bank.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time

from ..config import settings
from ..errors import AIUnavailableError
from ..logging_setup import get_logger

logger = get_logger(__name__)

DIFFICULTY_LABELS: dict[str, str] = {
    "easy": "ЛЁГКИЙ — простые факты, известные любому школьнику",
    "medium": "СРЕДНИЙ — требует кругозора эрудированного взрослого",
    "hard": "СЛОЖНЫЙ — экспертные знания, узкоспециальные детали",
}

SYSTEM_PROMPT = (
    "Ты — методист-составитель вопросов для викторины «Мозговой Штурм». "
    "Твоя единственная задача — вернуть корректный JSON-объект с вопросами.\n"
    "\n"
    "ЖЁСТКИЕ ПРАВИЛА:\n"
    "1. Отвечай ТОЛЬКО JSON-объектом вида {\"questions\": [...]}. "
    "Никакого текста, пояснений, markdown или ```-блоков до/после JSON.\n"
    "2. В массиве РОВНО столько вопросов, сколько просят в запросе.\n"
    "3. Каждый вопрос — объект ровно с полями: "
    "\"question\" (строка), \"options\" (массив строк), \"correct\" (целое число), "
    "\"explanation\" (строка), \"hint\" (строка).\n"
    "4. \"correct\" — индекс правильного ответа в \"options\" с нуля (0..N-1). "
    "Перед выводом мысленно проверь: options[correct] — действительно правильный.\n"
    "5. В \"options\" ровно N вариантов, все РАЗНЫЕ, правдоподобные, одного типа и языка. "
    "Нельзя вариантов «всё перечисленное», «ничего из перечисленного», «нет ответа».\n"
    "6. Правильный ответ НЕ должен быть очевидно длиннее или подробнее остальных.\n"
    "7. \"explanation\" — один короткий проверяемый факт (≤ 140 символов). "
    "\"hint\" — намёк, который НЕ называет правильный вариант (≤ 120 символов).\n"
    "8. Факты должны быть однозначными и проверяемыми. "
    "Никаких вопросов с двумя верными ответами, устаревших данных и выдуманных фактов.\n"
    "9. Язык вопросов, вариантов и пояснений — русский.\n"
    "10. Если не можешь составить вопрос по теме — верни меньше вопросов, но не выдумывай факты."
)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```")
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_QUESTION_OBJ_RE = re.compile(r"\{[^{}]*?\"question\"[^{}]*?\}", re.DOTALL)
_QUESTION_KEYS = ("questions", "вопросы", "items", "data")
_LETTER_BASE = ord("A")


# ─────────────────────────────────────────────────────────────
#  Hashing & deduplication
# ─────────────────────────────────────────────────────────────
def question_hash(question: dict) -> str:
    """Stable short hash of a question's stem, used for deduplication."""
    key = str(question.get("question") or "").strip().lower()[:80]
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _deduplicate(questions: list[dict], seen: set) -> list[dict]:
    """Drop questions whose hash is already present in ``seen``."""
    unique: list[dict] = []
    for question in questions:
        digest = question_hash(question)
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(question)
    return unique


def _mark_bonus(questions: list[dict], chance: float = 0.15) -> list[dict]:
    """Randomly flag a subset of questions as bonus questions."""
    return [{**q, "bonus": random.random() < chance} for q in questions]


# ─────────────────────────────────────────────────────────────
#  Prompt construction
# ─────────────────────────────────────────────────────────────
def build_prompt(topic: str, count: int, difficulty: str, num_options: int,
                 used_hashes: list[str] | None = None) -> str:
    """Build a precise, self-contained user prompt for question generation.

    The prompt restates the hard constraints (exact count, option count, index
    range, no duplicate options) so the model does not have to infer them, and
    pins ``correct`` to a 0-based index.  A concrete, fully worked example keeps
    the output shape stable.
    """
    diff_label = DIFFICULTY_LABELS.get(difficulty, DIFFICULTY_LABELS["medium"])
    max_idx = num_options - 1
    example_options = ["Ag", "Au", "Fe", "Cu", "Sn", "Pb"][:num_options]
    example = {
        "question": "Какой химический символ у золота?",
        "options": example_options,
        "correct": 1,
        "explanation": "Au — от латинского Aurum.",
        "hint": "Благородный металл жёлтого цвета.",
    }
    example_json = json.dumps({"questions": [example]}, ensure_ascii=False, indent=2)
    used_block = ""
    if used_hashes:
        used_block = (
            "⛔ Не повторяй эти вопросы (идентификаторы уже заданных): "
            f"{', '.join(used_hashes[-10:])}\n"
        )
    return (
        f'Составь РОВНО {count} вопросов викторины по теме: "{topic}".\n'
        f"Уровень сложности: {diff_label}.\n"
        f"{used_block}"
        "\n"
        "ФОРМАТ ОТВЕТА — только JSON-объект {\"questions\": [...]}, без текста вокруг:\n"
        f"{example_json}\n"
        "\n"
        "ТРЕБОВАНИЯ К КАЖДОМУ ВОПРОСУ:\n"
        f"• options: ровно {num_options} разных правдоподобных вариантов (A.."
        f"{chr(_LETTER_BASE + max_idx)}), одного языка и уровня детализации;\n"
        f"• correct: целое число 0..{max_idx} — индекс правильного варианта в options; "
        "ОБЯЗАТЕЛЬНО перепроверь, что options[correct] — верный ответ;\n"
        "• explanation: короткое проверяемое пояснение (≤ 140 символов);\n"
        "• hint: намёк без прямого указания правильного варианта (≤ 120 символов);\n"
        "• факты однозначные и проверяемые; не используй «всё/ничего из перечисленного».\n"
        "\n"
        "Верни только JSON."
    )


def build_hint_prompt(question_text: str) -> str:
    """Build the prompt asking for a hint that does not reveal the answer."""
    return (
        "Дай одну подсказку (1-2 предложения) к вопросу. "
        "НЕ называй ответ — наведи на мысль.\n"
        f"Вопрос: {question_text}\nПодсказка:"
    )


# ─────────────────────────────────────────────────────────────
#  GigaChat transport
# ─────────────────────────────────────────────────────────────
def _sleep(seconds: float) -> None:
    """Sleep without blocking the eventlet hub when it is in use."""
    try:
        import eventlet  # noqa: WPS433 - optional dependency
        eventlet.sleep(seconds)
    except Exception:  # pragma: no cover - eventlet always present in prod
        time.sleep(seconds)


#: JSON schema that pins the model's output shape.  When GigaChat supports
#: structured output this makes replies machine-parseable with far fewer
#: malformed answers than free-form prompting.
_QUESTIONS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "correct": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "hint": {"type": "string"},
                },
                "required": ["question", "options", "correct"],
            },
        }
    },
    "required": ["questions"],
}


def call_gigachat(user_prompt: str, system: str = SYSTEM_PROMPT, *,
                  retries: int = 2, timeout: int | None = None,
                  json_schema: dict | None = None) -> str:
    """Send a single-turn chat request to GigaChat and return the text reply.

    Parameters
    ----------
    json_schema:
        Optional JSON schema enabling structured output.  Callers that expect
        JSON pass :data:`_QUESTIONS_SCHEMA`; if the installed SDK or model does
        not support it the request silently falls back to plain chat.

    Raises
    ------
    AIUnavailableError
        When credentials are missing or every retry fails.
    """
    if not settings.gigachat_credentials:
        raise AIUnavailableError("GIGACHAT_CREDENTIALS не задан")

    try:
        from gigachat import GigaChat
        from gigachat.models import Chat
    except ImportError as exc:  # pragma: no cover - dependency issue
        raise AIUnavailableError(f"Библиотека gigachat недоступна: {exc}") from exc

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with GigaChat(
                credentials=settings.gigachat_credentials,
                scope=settings.gigachat_scope,
                model=settings.gigachat_model,
                verify_ssl_certs=settings.gigachat_verify_ssl,
                timeout=timeout or settings.gigachat_timeout,
            ) as client:
                chat = Chat(messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ])
                if json_schema is not None:
                    chat = _with_json_schema(chat, json_schema)
                response = client.chat(chat)
                content = response.choices[0].message.content
            if content and content.strip():
                return content.strip()
            last_error = AIUnavailableError("Пустой ответ GigaChat")
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
            last_error = exc
            logger.warning("GigaChat attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                _sleep(0.5 * attempt)

    raise AIUnavailableError(f"GigaChat недоступен: {last_error}")


def _with_json_schema(chat, schema: dict):
    """Attach a structured-output schema, tolerating SDKs without support."""
    try:
        from gigachat.models.response_format import JsonSchemaResponseFormat
        chat.response_format = JsonSchemaResponseFormat(schema=schema)
    except Exception:  # noqa: BLE001 - older SDK, plain JSON prompting still works
        pass
    return chat


# ─────────────────────────────────────────────────────────────
#  Response parsing
# ─────────────────────────────────────────────────────────────
def _extract_json(raw: str) -> str:
    """Best-effort extraction of a JSON object from a model reply."""
    text = (raw or "").strip().lstrip("\ufeff")
    block = _JSON_BLOCK_RE.search(text)
    if block:
        text = block.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def _parse_response(raw: str) -> list[dict]:
    """Parse the model reply into a list of raw question dicts."""
    text = _extract_json(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("JSON parse error: %s | raw[:200]=%s", exc, text[:200])
        return _regex_fallback(raw)

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return _regex_fallback(raw)

    for key in _QUESTION_KEYS:
        value = data.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    if "question" in data and "options" in data:
        return [data]
    for value in data.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "question" in value[0]:
            return value
    return _regex_fallback(raw)


def _regex_fallback(raw: str) -> list[dict]:
    """Salvage individual question objects when the JSON is malformed."""
    questions: list[dict] = []
    for match in _QUESTION_OBJ_RE.finditer(raw or ""):
        chunk = _TRAILING_COMMA_RE.sub(r"\1", match.group(0))
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if "question" in obj and "options" in obj:
            questions.append(obj)
    if questions:
        logger.info("Regex fallback recovered %d questions", len(questions))
    return questions


# ─────────────────────────────────────────────────────────────
#  Normalisation
# ─────────────────────────────────────────────────────────────
def normalise_correct_index(question: dict, num_options: int) -> int:
    """Resolve ``correct`` to a valid 0-based index.

    Handles integers, numeric strings, option text and ``A``-style letters.
    """
    raw = question.get("correct", 0)
    options = question.get("options") or []

    if isinstance(raw, bool):  # bool is an int subclass - normalise explicitly
        raw = int(raw)
    if isinstance(raw, int):
        if 0 <= raw < num_options:
            return raw
        if raw == num_options:          # unambiguously 1-based
            return num_options - 1
        return 0

    if isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            index = int(text)
            if 0 <= index < num_options:
                return index
            if index == num_options:
                return num_options - 1
            return 0
        lowered = text.lower()
        for index, option in enumerate(options):
            if str(option).strip().lower() == lowered:
                return index
        if len(text) == 1 and text.upper().isalpha():
            index = ord(text.upper()) - _LETTER_BASE
            if 0 <= index < num_options:
                return index

    logger.warning("Не удалось определить correct=%r, ставим 0", raw)
    return 0


def _convert_batch_indices(questions: list[dict], num_options: int) -> list[dict]:
    """Convert a whole batch from 1-based to 0-based indices when unambiguous.

    A batch is treated as 1-based only when every numeric ``correct`` lies in
    ``1..num_options`` and at least one equals ``num_options``.  This avoids the
    original bug where a legitimate 0-based ``2`` was silently decremented.
    """
    numeric = [q.get("correct") for q in questions if isinstance(q.get("correct"), int)]
    if not numeric:
        return questions
    if min(numeric) >= 1 and max(numeric) == num_options:
        logger.info("Batch 1→0 index conversion (%d questions)", len(questions))
        for question in questions:
            if isinstance(question.get("correct"), int) and question["correct"] > 0:
                question["correct"] -= 1
    return questions


_META_OPTION_RE = re.compile(
    r"^\s*(все\s+(?:выше)?перечисленн\w*|ничего\s+из\s+перечисленн\w*|"
    r"все\s+ответы|нет\s+(?:правильного\s+)?ответа|всё\s+выше|оба\s+варианта)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def normalise_question(question: dict, num_options: int) -> dict | None:
    """Validate and normalise a single question, or return ``None``.

    Beyond shape normalisation this rejects low-quality output: questions whose
    stem is too short, options that are duplicates, and "all/none of the above"
    meta-options that make a question untestable.
    """
    if not isinstance(question, dict):
        return None

    text = str(question.get("question") or "").strip()
    if len(text) < 10:
        return None

    options = question.get("options") or []
    if not isinstance(options, list):
        return None

    options = [str(o).strip() for o in options if str(o).strip()]
    if len(options) < 2:
        return None
    if any(_META_OPTION_RE.match(o) for o in options):
        return None

    while len(options) < num_options:
        options.append(f"Вариант {chr(_LETTER_BASE + len(options))}")
    options = options[:num_options]

    seen: set = set()
    unique_options: list[str] = []
    for option in options:
        if option.lower() in seen:
            unique_options.append(f"Другой вариант {chr(_LETTER_BASE + len(unique_options))}")
        else:
            seen.add(option.lower())
            unique_options.append(option)

    normalised = dict(question)
    normalised["question"] = text
    normalised["options"] = unique_options
    normalised["correct"] = normalise_correct_index(question, num_options)
    if not 0 <= normalised["correct"] < len(unique_options):
        normalised["correct"] = 0
    normalised["explanation"] = str(question.get("explanation") or "").strip()[:300]
    normalised["hint"] = str(question.get("hint") or "").strip()[:300]
    return normalised


def parse_questions_json(raw: str, num_options: int = 4) -> list[dict]:
    """Public helper: parse and normalise a raw model reply."""
    questions = _convert_batch_indices(_parse_response(raw), num_options)
    result: list[dict] = []
    for question in questions:
        normalised = normalise_question(question, num_options)
        if normalised:
            result.append(normalised)
    return result


# ─────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────
def generate_questions(topic: str, count: int, difficulty: str, num_options: int,
                       used_questions: list[dict] | None = None) -> list[dict]:
    """Generate ``count`` questions, falling back to the local bank on failure.

    ``used_questions`` are avoided: their hashes are sent to the model and used
    to filter the fallback bank.
    """
    count = max(1, min(int(count), 50))
    num_options = max(2, min(int(num_options), 6))
    used_hashes = [question_hash(q) for q in (used_questions or [])]
    seen = set(used_hashes)

    if settings.ai_enabled:
        try:
            prompt = build_prompt(topic, count, difficulty, num_options, used_hashes)
            logger.info("GigaChat request: topic=%s count=%d diff=%s opts=%d",
                        topic, count, difficulty, num_options)
            raw = call_gigachat(prompt, json_schema=_QUESTIONS_SCHEMA)
            questions = _convert_batch_indices(_parse_response(raw), num_options)
            validated = [q for q in (normalise_question(item, num_options) for item in questions) if q]
            unique = _deduplicate(validated, seen)
            if len(unique) < count:
                logger.info("GigaChat returned %d/%d usable questions; topping up from bank",
                            len(unique), count)
                unique += _get_fallback_questions(count - len(unique), num_options, seen)
            if unique:
                logger.info("GigaChat produced %d questions", len(unique))
                return _mark_bonus(unique[:count])
            logger.warning("All GigaChat questions were filtered out; using fallback")
        except AIUnavailableError as exc:
            logger.warning("AI unavailable (%s); using fallback bank", exc)

    return _get_fallback_questions(count, num_options, seen)


def _get_fallback_questions(count: int, num_options: int, seen: set | None = None) -> list[dict]:
    """Return up to ``count`` questions from the curated local bank."""
    seen = seen if seen is not None else set()
    pool = [dict(q) for q in FALLBACK_QUESTIONS]
    random.shuffle(pool)
    result: list[dict] = []
    for candidate in pool:
        if len(result) >= count:
            break
        options = list(candidate["options"])
        while len(options) < num_options:
            options.append(f"Вариант {len(options) + 1}")
        candidate = dict(candidate)
        candidate["options"] = options[:num_options]
        candidate["correct"] = min(int(candidate.get("correct", 0)), num_options - 1)
        normalised = normalise_question(candidate, num_options)
        if not normalised:
            continue
        digest = question_hash(normalised)
        if digest in seen:
            continue
        seen.add(digest)
        result.append(normalised)
    if len(result) < count:
        logger.warning("Fallback bank exhausted: %d/%d questions", len(result), count)
    return _mark_bonus(result)


def generate_hint(question_text: str) -> str:
    """Return a hint for ``question_text``, with a static fallback."""
    if settings.ai_enabled:
        try:
            hint = call_gigachat(build_hint_prompt(question_text), retries=1)
            hint = re.sub(r"^(Подсказка\s*:?\s*)", "", hint, flags=re.IGNORECASE).strip()
            if hint:
                return hint
        except AIUnavailableError as exc:
            logger.warning("Hint generation failed: %s", exc)
    return "Подумайте внимательно — ответ связан с контекстом вопроса."


def active_backend() -> str:
    """Human-readable description of the active question source."""
    if settings.ai_enabled:
        return f"GigaChat ({settings.gigachat_scope})"
    return "Fallback (встроенный банк вопросов)"


#: Curated fallback bank used when the AI is unavailable.
FALLBACK_QUESTIONS: list[dict] = [
    {"question": "Сколько планет в Солнечной системе?", "options": ["6", "7", "8", "9"], "correct": 2, "explanation": "Плутон исключён МАС в 2006 году."},
    {"question": "Химический символ золота?", "options": ["Ag", "Fe", "Au", "Cu"], "correct": 2, "explanation": "Au — от латинского Aurum."},
    {"question": "В каком году произошла Октябрьская революция?", "options": ["1905", "1914", "1917", "1922"], "correct": 2, "explanation": "7 ноября 1917 г."},
    {"question": "Столица Австралии?", "options": ["Сидней", "Мельбурн", "Канберра", "Брисбен"], "correct": 2, "explanation": "Канберра — компромисс между Сиднеем и Мельбурном."},
    {"question": "Кто написал «Войну и мир»?", "options": ["Достоевский", "Чехов", "Толстой", "Тургенев"], "correct": 2, "explanation": "Лев Толстой (1863-1869)."},
    {"question": "Основной газ атмосферы Земли?", "options": ["Кислород", "Углекислый газ", "Аргон", "Азот"], "correct": 3, "explanation": "Азот ~78%."},
    {"question": "Самый лёгкий металл?", "options": ["Алюминий", "Магний", "Литий", "Натрий"], "correct": 2, "explanation": "Литий 0.53 г/см³."},
    {"question": "В каком году человек впервые полетел в космос?", "options": ["1957", "1959", "1961", "1965"], "correct": 2, "explanation": "Гагарин 12 апреля 1961."},
    {"question": "Самая длинная река в мире?", "options": ["Амазонка", "Нил", "Янцзы", "Миссисипи"], "correct": 1, "explanation": "Нил ~6670 км."},
    {"question": "Сколько костей у взрослого человека?", "options": ["186", "206", "226", "246"], "correct": 1, "explanation": "206 костей."},
    {"question": "Столица Японии?", "options": ["Осака", "Киото", "Токио", "Иокогама"], "correct": 2, "explanation": "Токио с 1869."},
    {"question": "Кто написал «Мастер и Маргарита»?", "options": ["Пастернак", "Булгаков", "Есенин", "Ахматова"], "correct": 1, "explanation": "Булгаков (1928-1940)."},
    {"question": "Скорость света в вакууме (тыс. км/с)?", "options": ["100", "200", "300", "400"], "correct": 2, "explanation": "~300 тыс. км/с."},
    {"question": "Какой орган вырабатывает инсулин?", "options": ["Печень", "Почки", "Поджелудочная железа", "Надпочечники"], "correct": 2, "explanation": "β-клетки поджелудочной."},
    {"question": "Ближайшая к Солнцу планета?", "options": ["Венера", "Земля", "Меркурий", "Марс"], "correct": 2, "explanation": "Меркурий."},
    {"question": "Кто написал 9-ю симфонию?", "options": ["Моцарт", "Шуберт", "Бах", "Бетховен"], "correct": 3, "explanation": "Бетховен 1824."},
    {"question": "Чему равна сумма углов треугольника?", "options": ["90°", "180°", "270°", "360°"], "correct": 1, "explanation": "180°."},
    {"question": "Сколько хромосом у человека?", "options": ["23", "44", "46", "48"], "correct": 2, "explanation": "46 хромосом."},
    {"question": "Животное-символ WWF?", "options": ["Белый медведь", "Большая панда", "Тигр", "Снежный барс"], "correct": 1, "explanation": "Панда с 1961."},
    {"question": "Самая большая страна?", "options": ["Канада", "Китай", "США", "Россия"], "correct": 3, "explanation": "Россия 17.1 млн км²."},
    {"question": "Сколько сторон у шестиугольника?", "options": ["4", "5", "6", "7"], "correct": 2, "explanation": "6 сторон."},
    {"question": "Какой элемент O?", "options": ["Золото", "Азот", "Кислород", "Водород"], "correct": 2, "explanation": "Кислород."},
    {"question": "Сколько нот в гамме?", "options": ["5", "6", "7", "8"], "correct": 2, "explanation": "7 нот."},
    {"question": "Кто изобрёл телефон?", "options": ["Эдисон", "Белл", "Маркони", "Тесла"], "correct": 1, "explanation": "А. Белл 1876."},
    {"question": "Самая высокая гора?", "options": ["К2", "Лхоцзе", "Канченджанга", "Эверест"], "correct": 3, "explanation": "Эверест 8848 м."},
    {"question": "Сколько цветов у радуги?", "options": ["5", "6", "7", "8"], "correct": 2, "explanation": "7 цветов."},
    {"question": "Год основания Apple?", "options": ["1972", "1974", "1976", "1980"], "correct": 2, "explanation": "1976."},
    {"question": "Автор теории относительности?", "options": ["Ньютон", "Бор", "Эйнштейн", "Фейнман"], "correct": 2, "explanation": "Эйнштейн 1905."},
    {"question": "Сколько граней у куба?", "options": ["4", "5", "6", "8"], "correct": 2, "explanation": "6 граней."},
    {"question": "Самое глубокое озеро?", "options": ["Каспийское", "Байкал", "Танганьика", "Гурон"], "correct": 1, "explanation": "Байкал 1642 м."},
    {"question": "Как называется наша галактика?", "options": ["Андромеда", "Треугольник", "Млечный Путь", "Магеллановы"], "correct": 2, "explanation": "Млечный Путь."},
    {"question": "Столица Франции?", "options": ["Лион", "Марсель", "Ницца", "Париж"], "correct": 3, "explanation": "Париж."},
    {"question": "Сколько букв в русском алфавите?", "options": ["30", "32", "33", "35"], "correct": 2, "explanation": "33 буквы."},
    {"question": "Кто написал «Преступление и наказание»?", "options": ["Толстой", "Тургенев", "Горький", "Достоевский"], "correct": 3, "explanation": "Достоевский 1866."},
    {"question": "Сколько океанов?", "options": ["3", "4", "5", "6"], "correct": 2, "explanation": "5 океанов."},
    {"question": "Число π (приближённо)?", "options": ["2.71", "3.14", "3.17", "3.41"], "correct": 1, "explanation": "3.14."},
    {"question": "Самый твёрдый минерал?", "options": ["Рубин", "Кварц", "Корунд", "Алмаз"], "correct": 3, "explanation": "Алмаз."},
    {"question": "Кто первым совершил кругосветное плавание?", "options": ["Колумб", "Васко да Гама", "Магеллан-Элькано", "Дрейк"], "correct": 2, "explanation": "Магеллан-Элькано 1522."},
    {"question": "Что означает www?", "options": ["Wide Web World", "World Wide Web", "Web World Wide", "Wire Web World"], "correct": 1, "explanation": "World Wide Web 1989."},
    {"question": "Основной орган кровообращения?", "options": ["Печень", "Лёгкие", "Сердце", "Почки"], "correct": 2, "explanation": "Сердце."},
]
