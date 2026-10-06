"""User-generated content (UGC) service.

Handles creation, moderation, voting and reporting of player-authored
questions.  Content is screened by a lightweight profanity/length filter before
it enters the moderation queue.
"""

from __future__ import annotations

import json
import re
import time

from ..db import user_connection
from ..errors import ValidationError
from ..logging_setup import get_logger
from ..validation import MAX_QUESTION_LEN, validate_options

logger = get_logger(__name__)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

MIN_QUESTION_LEN = 10
MAX_TOPIC_LEN = 50
REPORTS_TO_HIDE = 5

_BAD_WORDS = re.compile(
    r"\b(хуй|пизд|ебл|блядь|сука|мудак|чмо|нигг|фашист|убий)\w*",
    re.IGNORECASE | re.UNICODE,
)


def auto_moderate(question: str, options: list[str], correct: int) -> tuple[bool, str]:
    """Fast, dependency-free pre-screening.  Returns ``(ok, reason)``."""
    text = (question or "").strip()
    if len(text) < MIN_QUESTION_LEN:
        return False, f"Вопрос слишком короткий (мин. {MIN_QUESTION_LEN} символов)"
    if len(text) > MAX_QUESTION_LEN:
        return False, f"Вопрос слишком длинный (макс. {MAX_QUESTION_LEN} символов)"
    try:
        clean_options = validate_options(options, min_count=2, max_count=6)
    except ValidationError as exc:
        return False, str(exc)
    if not 0 <= int(correct) < len(clean_options):
        return False, "Неверный индекс правильного ответа"
    if _BAD_WORDS.search(text) or any(_BAD_WORDS.search(o) for o in clean_options):
        return False, "Текст содержит недопустимые слова"
    return True, "OK"


def create_question(author: str, question: str, options: list[str], correct: int,
                    topic: str = "", difficulty: int = 2) -> int:
    """Create a UGC question in the moderation queue and return its id."""
    ok, reason = auto_moderate(question, options, correct)
    if not ok:
        raise ValidationError(reason)

    clean_options = validate_options(options, min_count=2, max_count=6)
    difficulty = max(1, min(5, int(difficulty)))
    topic = (topic or "").strip()[:MAX_TOPIC_LEN]
    text = question.strip()

    with user_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM ugc_questions WHERE LOWER(question)=LOWER(?)", (text,)
        ).fetchone()
        if existing:
            raise ValidationError("Такой вопрос уже существует")
        cursor = conn.execute(
            "INSERT INTO ugc_questions(author, question, options, correct, topic, difficulty, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (author, text, json.dumps(clean_options, ensure_ascii=False), int(correct),
             topic, difficulty, STATUS_PENDING, int(time.time())),
        )
        question_id = int(cursor.lastrowid)
    logger.info("UGC question %d created by %s", question_id, author)
    return question_id


def _rows_to_dicts(rows) -> list[dict]:
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["options"] = json.loads(item.get("options") or "[]")
        except json.JSONDecodeError:
            item["options"] = []
        result.append(item)
    return result


def list_questions(topic: str = "", status: str = STATUS_APPROVED,
                   limit: int = 50, offset: int = 0) -> list[dict]:
    """List UGC questions, optionally filtered by topic."""
    try:
        with user_connection() as conn:
            if topic:
                rows = conn.execute(
                    "SELECT * FROM ugc_questions WHERE status=? AND topic LIKE ? "
                    "ORDER BY rating DESC LIMIT ? OFFSET ?",
                    (status, f"%{topic}%", int(limit), int(offset)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM ugc_questions WHERE status=? ORDER BY rating DESC LIMIT ? OFFSET ?",
                    (status, int(limit), int(offset)),
                ).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:  # noqa: BLE001
        logger.error("list_questions: %s", exc)
        return []


def list_by_author(author: str, limit: int = 100) -> list[dict]:
    """Return questions authored by ``author``."""
    try:
        with user_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM ugc_questions WHERE author=? ORDER BY created_at DESC LIMIT ?",
                (author, int(limit)),
            ).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:  # noqa: BLE001
        logger.error("list_by_author(%s): %s", author, exc)
        return []


def list_pending(limit: int = 50) -> list[dict]:
    """Return questions awaiting moderation, oldest first."""
    try:
        with user_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM ugc_questions WHERE status=? ORDER BY created_at LIMIT ?",
                (STATUS_PENDING, int(limit)),
            ).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:  # noqa: BLE001
        logger.error("list_pending: %s", exc)
        return []


def author_question_count(author: str) -> int:
    """Number of questions authored by ``author``."""
    try:
        with user_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ugc_questions WHERE author=?", (author,)
            ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def vote(question_id: int, voter: str, value: int) -> bool:
    """Record a +1/-1 vote and refresh the question's aggregate rating."""
    if value not in (1, -1):
        raise ValidationError("vote должно быть 1 или -1")
    try:
        with user_connection() as conn:
            conn.execute(
                "INSERT INTO ugc_votes(question_id, voter, vote) VALUES(?,?,?) "
                "ON CONFLICT(question_id, voter) DO UPDATE SET vote=excluded.vote",
                (int(question_id), voter, int(value)),
            )
            row = conn.execute(
                "SELECT COALESCE(SUM(vote),0) AS r FROM ugc_votes WHERE question_id=?",
                (int(question_id),),
            ).fetchone()
            conn.execute(
                "UPDATE ugc_questions SET rating=? WHERE id=?",
                (float(row["r"]) if row else 0.0, int(question_id)),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("vote(%s): %s", question_id, exc)
        return False


def report(question_id: int, reporter: str, reason: str = "") -> bool:
    """Report a question; after enough reports it is pulled for re-review."""
    try:
        with user_connection() as conn:
            conn.execute(
                "INSERT INTO ugc_reports(question_id, reporter, reason, created_at) VALUES(?,?,?,?)",
                (int(question_id), reporter, (reason or "")[:200], int(time.time())),
            )
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ugc_reports WHERE question_id=?", (int(question_id),)
            ).fetchone()
            if row and int(row["n"]) >= REPORTS_TO_HIDE:
                conn.execute(
                    "UPDATE ugc_questions SET status=? WHERE id=?", (STATUS_PENDING, int(question_id))
                )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("report(%s): %s", question_id, exc)
        return False


def moderate(question_id: int, approve: bool, reason: str = "") -> bool:
    """Approve or reject a pending question."""
    status = STATUS_APPROVED if approve else STATUS_REJECTED
    try:
        with user_connection() as conn:
            conn.execute(
                "UPDATE ugc_questions SET status=?, reject_reason=? WHERE id=?",
                (status, (reason or "")[:200], int(question_id)),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("moderate(%s): %s", question_id, exc)
        return False


def record_usage(question_id: int, correct: bool) -> None:
    """Update a question's running accuracy statistic."""
    try:
        with user_connection() as conn:
            conn.execute(
                "UPDATE ugc_questions SET usage_count=usage_count+1 WHERE id=?", (int(question_id),)
            )
            row = conn.execute(
                "SELECT usage_count, correct_pct FROM ugc_questions WHERE id=?", (int(question_id),)
            ).fetchone()
            if row and int(row["usage_count"]) > 0:
                count = int(row["usage_count"])
                old_pct = float(row["correct_pct"] or 0.0)
                new_pct = (old_pct * (count - 1) + (100.0 if correct else 0.0)) / count
                conn.execute(
                    "UPDATE ugc_questions SET correct_pct=? WHERE id=?", (new_pct, int(question_id))
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("record_usage(%s): %s", question_id, exc)


def as_quiz_question(row: dict, num_options: int = 4) -> dict | None:
    """Convert a stored UGC row into the internal question schema."""
    options = list(row.get("options") or [])
    if len(options) < 2:
        return None
    correct = int(row.get("correct", 0))
    if not 0 <= correct < len(options):
        correct = 0
    return {
        "question": row.get("question", ""),
        "options": options[:num_options],
        "correct": min(correct, num_options - 1),
        "explanation": "",
        "hint": "",
        "bonus": False,
        "ugc_id": row.get("id"),
    }
