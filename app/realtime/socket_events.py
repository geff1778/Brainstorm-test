"""SocketIO event handlers.

This module owns the transport layer only: it validates payloads, enforces
authorisation (host / cheater / admin) and delegates state changes to the domain
models.  Every event the browser emits has a matching handler here — previously
22 events (chat, cheat panel, admin panel, restart, presentation mode) were
silently dropped because the handler was missing or named differently.

Event catalogue
---------------
Client → server::

    create_room, join_room, rejoin_room, leave_room
    update_settings, init_teams, draft_pick, start_game, restart_room
    submit_answer, use_joker, get_hint, rephrase_question, reaction
    heartbeat, set_presentation_mode
    chat_message, chat_clear, chat_delete_message
    cheat_* (9 events), admin_* (6 events)
    svoyaigra_select_cell, svoyaigra_buzz, svoyaigra_answer, svoyaigra_host_reveal

Server → client::

    room_created, room_joined, rejoin_failed, players_update, host_changed
    settings_updated, teams_initialized, draft_updated, draft_complete
    game_loading, game_started, new_question, question_result, interim_results
    answer_ack, ffa_correct, player_answered, player_eliminated, life_lost
    joker_result, hint_received, question_rephrased, reaction_received
    game_over, room_restarted, kicked, player_kicked, player_renamed
    chat_history, chat_message, chat_cleared, chat_deleted
    cheat_ack, cheat_score_updated, cheat_player_reset, scores_updated, lives_restored
    admin_action_result, heartbeat_ack, presentation_mode_changed
    svoyaigra_question, svoyaigra_buzzed, svoyaigra_result, svoyaigra_wrong,
    svoyaigra_select_turn
"""

from __future__ import annotations

import time

from flask import request
from flask import session as flask_session
from flask_socketio import emit, join_room, leave_room

from ..domain.models import HINT_COST, JOKER_COST, MAX_TEAMS, REPHRASE_COST, Room
from ..domain.registry import registry
from ..errors import ValidationError
from ..logging_setup import get_logger
from ..services import accounts as accounts_service
from ..services import achievements as achievements_service
from ..services import ai_client, leaderboard
from ..services.leaderboard import reset_player_stats as _reset_lb
from ..validation import as_bool, as_int, as_str, clamp, validate_difficulty, validate_mode, validate_username
from . import scheduler
from .chat import chat_store

logger = get_logger(__name__)

TIME_PER_Q = 30
SI_ROWS = 5
SI_COLS = 3
SI_VALUES = [100, 200, 300, 400, 500]
MAX_CHAT_LEN = 200
BONUS_CHANCE = 0.20
ALLOWED_REACTIONS = {"👍", "😂", "🔥", "🧠", "😮", "❤️", "🎉", "💀"}

#: Settings keys a host may change, with their validation rules.
_SETTING_KEYS = (
    "topic", "question_count", "difficulty", "num_options", "game_mode", "timer",
    "team_count", "team_names", "presentation_mode", "si_categories", "si_rows",
)

#: Dependencies injected by :func:`register_handlers`.
_socketio = None
_gigachat_code = ""


# ─────────────────────────────────────────────────────────────
#  Small helpers
# ─────────────────────────────────────────────────────────────
def _err(message: str, *, event: str = "error") -> None:
    emit(event, {"message": message})


def _room_of(sid: str) -> Room | None:
    return registry.get_by_sid(sid)


def _player(room: Room | None, sid: str):
    return room.players.get(sid) if room else None


def _is_admin() -> bool:
    return bool(flask_session.get("is_admin"))


def _is_tester() -> bool:
    return bool(flask_session.get("is_tester"))


def _is_cheater(sid: str) -> bool:
    player = _player(_room_of(sid), sid)
    return bool(player and player.is_cheat)


def _is_host_or_cheater(sid: str, room: Room | None) -> bool:
    if room is None:
        return False
    return room.host_sid == sid or _is_cheater(sid)


def _admin_action(action: str, ok: bool = True, **extra) -> None:
    emit("admin_action_result", {"action": action, "ok": ok, **extra})


def _broadcast_players(room: Room) -> None:
    """Send each player a personalised roster (cheaters also see invisibles)."""
    public = room.players_list(viewer_sid=None)
    teams = room.teams_list()
    for sid in list(room.players.keys()):
        player = room.players.get(sid)
        payload = room.players_list(viewer_sid=sid) if player and player.is_cheat else public
        _socketio.emit("players_update", {"players": payload, "teams": teams}, room=sid)


def _emit_to_all(room: Room, event: str, payload: dict) -> None:
    _socketio.emit(event, payload, room=room.code)


# ─────────────────────────────────────────────────────────────
#  Question lifecycle
# ─────────────────────────────────────────────────────────────
def _question_time(room: Room) -> int:
    """Per-question time limit, honouring the host's ``timer`` setting."""
    return clamp(as_int(room.settings.get("timer", TIME_PER_Q), default=TIME_PER_Q), 10, 120)


def _emit_question(room: Room) -> None:
    question = room.current_question
    if not question:
        return
    # Mark the freshly shown question as unresolved so its own resolve pass runs.
    room.resolved_q = -1
    time_limit = _question_time(room)
    base = {
        "question": {"question": question["question"], "options": question["options"]},
        "question_number": room.current_q + 1,
        "total_questions": room.total_questions,
        "time_limit": time_limit,
        "mode": room.mode,
        "is_bonus": bool(question.get("bonus")),
        "difficulty": room.difficulty,
        "turn_team": room.settings.get("_turn_team", 1),
        "team_scores": room.team_scores(),
        "team_names": {tid: t.name for tid, t in room.teams.items()},
        "presentation": bool(room.settings.get("presentation_mode")),
    }
    for sid, player in room.players.items():
        payload = dict(base)
        if player.is_cheat:
            payload["cheat_correct"] = question["correct"]
        if room.mode == "lives":
            payload["my_lives"] = player.lives
        if player.instant_answer:
            payload["instant_answer"] = True
        _socketio.emit("new_question", payload, room=sid)
    scheduler.spawn_after(time_limit + 1, _timeout_question, room.code, room.current_q)


def _timeout_question(code: str, question_index: int) -> None:
    room = registry.get(code)
    if not room or room.state != "playing" or room.current_q != question_index:
        return
    for player in room.active_players:
        if not player.answered and not player.instant_answer:
            player.answered = True
            player.answer_index = -1
            player.answer_time = time.time()
            room.reset_streak(player.sid)
            room.record_answer_stat(False)
    _resolve_question(room)


def _resolve_question(room: Room) -> None:
    question = room.current_question
    if not question:
        return
    # Re-entrancy guard: the answer path, the timer and the cheat "skip" can all
    # try to resolve the same question.  Whoever gets here first wins; the rest
    # return immediately so a question is never advanced twice.
    if room.resolved_q == room.current_q:
        return
    room.resolved_q = room.current_q
    correct_index = question["correct"]
    options = question["options"]
    correct_text = options[correct_index] if 0 <= correct_index < len(options) else "?"

    answers = {
        sid: {"answer": p.answer_index, "correct": p.answer_index == correct_index,
              "streak": p.streak, "name": p.name}
        for sid, p in room.players.items()
    }
    room.record_answer_stat(any(v["correct"] for v in answers.values()))
    new_difficulty = room.recalculate_difficulty()
    room.answer_log.append({
        "question": question["question"],
        "correct_index": correct_index,
        "correct_text": correct_text,
        "answers": {p.name: p.answer_index for p in room.players.values() if not p.is_spectator},
    })

    _emit_to_all(room, "question_result", {
        "correct_index": correct_index,
        "correct_answer": correct_text,
        "explanation": question.get("explanation", ""),
        "player_answers": answers,
        "scores": {sid: p.score for sid, p in room.players.items()},
        "team_scores": room.team_scores(),
        "team_names": {tid: t.name for tid, t in room.teams.items()},
        "lives": {sid: p.lives for sid, p in room.players.items()},
        "mode": room.mode,
        "is_bonus": bool(question.get("bonus")),
        "new_difficulty": new_difficulty,
    })

    if question.get("ugc_id"):
        # Record usage statistics for player-authored questions.
        try:
            from ..services import ugc as ugc_service
            ugc_service.record_usage(question["ugc_id"], True)
        except Exception:  # noqa: BLE001
            pass

    has_next = room.advance_question()
    scheduler.sleep(4)
    if has_next:
        if room.current_q % 5 == 0 and room.current_q < room.total_questions:
            _emit_to_all(room, "interim_results", {
                "players": [p.to_dict() for p in room.players.values()],
                "next_question": room.current_q + 1,
                "difficulty": room.difficulty,
            })
            scheduler.sleep(5)
        _emit_question(room)
        return

    _finish_game(room)


def _finish_game(room: Room) -> None:
    duration = int(time.time() - room.game_start_time) if room.game_start_time else 0
    results = room.final_results()
    leaderboard.update_leaderboard(results["players"], duration=duration)
    leaderboard.save_room_history(
        room.code, duration, room.mode, room.settings.get("topic", ""),
        [{"name": p.name, "score": p.score, "total_correct": p.total_correct}
         for p in room.players.values()],
        room.answer_log,
    )
    _award_registered_players(room, results)
    _emit_to_all(room, "game_over", results)


def _award_registered_players(room: Room, results: dict) -> None:
    """Apply XP/coins/achievements for players with an account."""
    best = max((p["score"] for p in results["players"]), default=0)
    for entry in results["players"]:
        username = entry["name"]
        if not accounts_service.user_exists(username):
            continue
        won = entry["score"] == best and entry["score"] > 0
        xp_gain = entry["score"] // 10 + 5
        accounts_service.update_user_stats(username, entry["score"], won, xp_gain)
        accounts_service.add_coins(username, 5 if won else 2)
        achievements_service.evaluate(username)


def _player_left(sid: str) -> None:
    room = _room_of(sid)
    if not room:
        return
    player = room.remove_player(sid)
    registry.unindex(sid)
    leave_room(room.code)
    if not room.players:
        registry.remove(room.code)
        chat_store.drop(room.code)
        return
    if room.host_sid == sid:
        new_host = room.reassign_host()
        if new_host:
            _socketio.emit("host_changed", {"host": new_host.name}, room=room.code)
    _broadcast_players(room)
    if room.state == "playing" and room.all_answered():
        scheduler.spawn(_resolve_question, room)
    if player:
        logger.debug("%s left %s", player.name, room.code)


# ─────────────────────────────────────────────────────────────
#  Handler registration
# ─────────────────────────────────────────────────────────────
def register_handlers(socketio, gigachat_code: str) -> None:
    """Attach every event handler to ``socketio``."""
    global _socketio, _gigachat_code
    _socketio = socketio
    _gigachat_code = gigachat_code
    scheduler.spawn_periodic(30, registry.cleanup_stale)

    # ── Connection ───────────────────────────────────────────
    @socketio.on("connect")
    def on_connect():
        """Register a new socket connection."""
        logger.debug("socket connected: %s", request.sid)

    @socketio.on("disconnect")
    def on_disconnect():
        """Clean up a socket: drop the player from their room and re-index."""
        _player_left(request.sid)

    @socketio.on("heartbeat")
    def on_heartbeat(_data=None):
        """Refresh the player's liveness timestamp and acknowledge."""
        room = _room_of(request.sid)
        if room:
            room.touch()
        emit("heartbeat_ack", {"ts": time.time()})

    # ── Room lifecycle ───────────────────────────────────────
    @socketio.on("create_room")
    def on_create_room(data=None):
        """Create a new room and join it as host."""
        data = data or {}
        try:
            name = validate_username(data.get("player_name") or "Игрок")
        except ValidationError as exc:
            _err(str(exc))
            return
        if leaderboard.is_banned(name):
            _err("Вы заблокированы.")
            return
        if registry.at_capacity():
            _err("Сервер перегружен, попробуйте позже.")
            return
        is_public = as_bool(data.get("is_public"))
        is_sandbox = as_bool(data.get("is_sandbox")) and _is_tester()
        room = registry.create(request.sid, is_public=is_public, is_sandbox=is_sandbox)
        room.add_player(request.sid, name, is_cheat=_is_tester())
        room.touch()
        join_room(room.code)
        emit("room_created", {
            "room_code": room.code, "is_host": True,
            "players": room.players_list(viewer_sid=request.sid),
            "is_sandbox": is_sandbox, "teams": room.teams_list(),
        })
        logger.info("%s created room %s (public=%s sandbox=%s)", name, room.code, is_public, is_sandbox)

    @socketio.on("join_room")
    def on_join_room(data=None):
        """Join an existing room as a player, spectator or invisible admin."""
        data = data or {}
        code = as_str(data.get("room_code")).upper()
        try:
            name = validate_username(data.get("player_name") or "Игрок")
        except ValidationError as exc:
            _err(str(exc))
            return
        if leaderboard.is_banned(name):
            _err("Вы заблокированы.")
            return
        room = registry.get(code)
        if room is None:
            _err("Комната не найдена.")
            return
        if room.name_taken(name):
            _err(f"Ник «{name}» уже занят. Выбери другой.")
            return

        spectator = as_bool(data.get("spectator"))
        is_cheater = _is_tester()
        as_admin = as_bool(data.get("as_admin")) and _is_admin()
        invisible = as_bool(data.get("invisible")) and (is_cheater or as_admin)

        if not is_cheater and not as_admin and room.state == "playing" and not spectator:
            _err("Игра уже началась. Войди как зритель.")
            return

        room.add_player(request.sid, name, spectator=spectator,
                        invisible=invisible, is_cheat=is_cheater)
        registry.index(request.sid, room.code)
        room.touch()
        join_room(room.code)
        emit("room_joined", {
            "room_code": room.code, "is_host": False, "is_spectator": spectator,
            "is_invisible": invisible, "players": room.players_list(viewer_sid=request.sid),
            "settings": room.settings, "is_sandbox": room.is_sandbox,
            "teams": room.teams_list(), "team_draft_active": room.team_draft_active,
        })
        if room.state == "playing" and (is_cheater or as_admin):
            player = room.players.get(request.sid)
            emit("game_started", {
                "your_team": player.team if player else None, "mode": room.mode,
                "is_spectator": spectator, "teams": room.teams_list(),
                "team_names": {tid: t.name for tid, t in room.teams.items()},
                "presentation": bool(room.settings.get("presentation_mode")), "rejoin": True,
            })
            if room.current_question:
                question = room.current_question
                payload = {
                    "question": {"question": question["question"], "options": question["options"]},
                    "question_number": room.current_q + 1,
                    "total_questions": room.total_questions, "time_limit": TIME_PER_Q,
                    "difficulty": room.current_difficulty, "mode": room.mode,
                    "is_bonus": bool(question.get("bonus")), "rejoin": True,
                    "team_scores": room.team_scores(),
                    "team_names": {tid: t.name for tid, t in room.teams.items()},
                    "presentation": bool(room.settings.get("presentation_mode")),
                }
                if is_cheater:
                    payload["cheat_correct"] = question["correct"]
                emit("new_question", payload)
        if not invisible:
            for sid in room.players:
                if sid != request.sid:
                    _socketio.emit("player_joined", {
                        "players": room.players_list(viewer_sid=sid),
                        "teams": room.teams_list(), "name": name, "spectator": spectator,
                    }, room=sid)
        emit("chat_history", {"messages": chat_store.history(room.code)})
        logger.info("%s joined %s (spectator=%s invisible=%s admin=%s)",
                    name, room.code, spectator, invisible, as_admin)

    @socketio.on("rejoin_room")
    def on_rejoin_room(data=None):
        """Re-attach a reconnecting socket to its previous room and seat."""
        data = data or {}
        code = as_str(data.get("room_code")).upper()
        name = as_str(data.get("player_name"))
        if not code or not name:
            emit("rejoin_failed", {"message": "Нет данных для восстановления."})
            return
        room = registry.get(code)
        if room is None:
            emit("rejoin_failed", {"message": "Комната не найдена или уже закрыта."})
            return

        existing_sid = next((s for s, p in room.players.items()
                             if p.name.lower() == name.lower()), None)
        if existing_sid and existing_sid != request.sid:
            player = room.players.pop(existing_sid)
            player.sid = request.sid
            room.players[request.sid] = player
            if room.host_sid == existing_sid:
                room.host_sid = request.sid
            for team in room.teams.values():
                if existing_sid in team.members:
                    team.members.remove(existing_sid)
                    team.members.append(request.sid)
                if team.leader_sid == existing_sid:
                    team.leader_sid = request.sid
            registry.rebind(existing_sid, request.sid, code)
        elif existing_sid == request.sid:
            player = room.players[request.sid]
        else:
            if room.state != "waiting":
                emit("rejoin_failed", {"message": "Игра уже идёт, игрок не найден."})
                return
            if room.name_taken(name):
                emit("rejoin_failed", {"message": f"Ник «{name}» занят."})
                return
            player = room.add_player(request.sid, name, is_cheat=_is_tester())
            registry.index(request.sid, code)

        room.touch()
        join_room(code)
        emit("room_joined", {
            "room_code": code, "is_host": room.host_sid == request.sid,
            "is_spectator": player.is_spectator, "is_invisible": player.is_invisible,
            "players": room.players_list(viewer_sid=request.sid), "settings": room.settings,
            "is_sandbox": room.is_sandbox, "teams": room.teams_list(),
            "team_draft_active": room.team_draft_active, "rejoin": True,
        })
        if room.state == "playing" and room.current_question:
            question = room.current_question
            emit("game_started", {
                "your_team": player.team, "mode": room.mode,
                "is_spectator": player.is_spectator, "teams": room.teams_list(),
                "team_names": {tid: t.name for tid, t in room.teams.items()},
                "presentation": bool(room.settings.get("presentation_mode")), "rejoin": True,
            })
            payload = {
                "question": {"question": question["question"], "options": question["options"]},
                "question_number": room.current_q + 1, "total_questions": room.total_questions,
                "time_limit": TIME_PER_Q, "difficulty": room.current_difficulty,
                "mode": room.mode, "is_bonus": bool(question.get("bonus")), "rejoin": True,
            }
            if player.is_cheat:
                payload["cheat_correct"] = question["correct"]
            emit("new_question", payload)
        emit("chat_history", {"messages": chat_store.history(code)})
        _broadcast_players(room)

    @socketio.on("leave_room")
    def on_leave_room(_data=None):
        """Leave the current room, transferring host if needed."""
        _player_left(request.sid)

    @socketio.on("restart_room")
    def on_restart_room(data=None):
        """Reset a finished room back to the lobby for another round."""
        data = data or {}
        room = _room_of(request.sid)
        if not room or not _is_host_or_cheater(request.sid, room):
            return
        keep_scores = as_bool(data.get("keep_scores"))
        same_squad = as_bool(data.get("same_squad"))
        if not same_squad:
            for sid in [s for s, p in room.players.items() if p.is_spectator]:
                room.players.pop(sid, None)
                registry.unindex(sid)
        room.reset_for_restart(keep_scores=keep_scores)
        chat_store.clear(room.code)
        _broadcast_players(room)
        _emit_to_all(room, "room_restarted", {
            "players": room.players_list(), "keep_scores": keep_scores,
        })

    # ── Settings & teams ─────────────────────────────────────
    @socketio.on("update_settings")
    def on_update_settings(data=None):
        """Apply host-chosen room settings (topic, mode, difficulty, ...)."""
        data = data or {}
        room = _room_of(request.sid)
        if not room or room.host_sid != request.sid:
            return
        sandbox = room.is_sandbox
        for key in _SETTING_KEYS:
            if key not in data:
                continue
            try:
                if key == "question_count" and not sandbox:
                    room.settings[key] = clamp(as_int(data[key], default=10), 1, 50)
                elif key == "num_options" and not sandbox:
                    room.settings[key] = clamp(as_int(data[key], default=4), 2, 6)
                elif key == "team_count" and not sandbox:
                    room.settings[key] = clamp(as_int(data[key], default=2), 2, MAX_TEAMS)
                elif key == "si_rows":
                    room.settings[key] = clamp(as_int(data[key], default=SI_ROWS), 3, 5)
                elif key == "difficulty":
                    room.settings[key] = validate_difficulty(data[key])
                elif key == "game_mode":
                    room.settings[key] = validate_mode(data[key])
                else:
                    room.settings[key] = data[key]
            except (ValidationError, TypeError) as exc:
                logger.debug("Ignoring bad setting %s=%r: %s", key, data[key], exc)
        room.current_difficulty = validate_difficulty(room.settings.get("difficulty"), default="medium")
        room.touch()
        _emit_to_all(room, "settings_updated", {"settings": room.settings})

    @socketio.on("init_teams")
    def on_init_teams(data=None):
        """Create teams and open the player draft."""
        data = data or {}
        room = _room_of(request.sid)
        if not room or not _is_host_or_cheater(request.sid, room):
            return
        count = clamp(as_int(data.get("count", 2), default=2), 2, MAX_TEAMS)
        names = data.get("names") if isinstance(data.get("names"), list) else []
        room.init_teams(count, names)
        if as_bool(data.get("draft_mode")):
            room.assign_team_leaders()
            room.team_draft_active = True
            room.draft_turn_team = sorted(room.teams.keys())[0]
        else:
            room.team_draft_active = False
        _broadcast_players(room)
        _emit_to_all(room, "teams_initialized", {
            "teams": room.teams_list(), "draft_active": room.team_draft_active,
            "draft_turn": room.draft_turn_team,
        })

    @socketio.on("draft_pick")
    def on_draft_pick(data=None):
        """Assign the currently drafting player to a team."""
        data = data or {}
        room = _room_of(request.sid)
        if not room or not room.team_draft_active:
            return
        ok, message = room.draft_pick(request.sid, as_str(data.get("target_sid")))
        if not ok:
            _err(message)
            return
        _broadcast_players(room)
        _emit_to_all(room, "draft_updated", {
            "teams": room.teams_list(), "draft_active": room.team_draft_active,
            "draft_turn": room.draft_turn_team,
        })
        if not room.team_draft_active:
            _emit_to_all(room, "draft_complete", {"teams": room.teams_list()})

    # ── Game start ───────────────────────────────────────────
    @socketio.on("start_game")
    def on_start_game(_data=None):
        """Generate questions and begin a round (host or cheater only)."""
        room = _room_of(request.sid)
        if not room or not _is_host_or_cheater(request.sid, room):
            return
        if room.state != "waiting":
            return
        if len(room.human_players) < 1:
            _err("Нужен хотя бы 1 игрок.")
            return
        if room.team_draft_active:
            _err("Сначала завершите выбор команд.")
            return

        settings = room.settings
        sandbox = room.is_sandbox
        topic = as_str(settings.get("topic") or "Общие знания", max_len=60) or "Общие знания"
        count = clamp(as_int(settings.get("question_count", 10), default=10), 1, 50) if not sandbox \
            else max(1, as_int(settings.get("question_count", 10), default=10))
        difficulty = validate_difficulty(settings.get("difficulty"))
        num_options = clamp(as_int(settings.get("num_options", 4), default=4), 2, 6) if not sandbox \
            else max(2, as_int(settings.get("num_options", 4), default=4))
        room.current_difficulty = difficulty

        if room.mode == "svoyaigra":
            scheduler.spawn(_start_svoyaigra, room, difficulty, num_options)
            return
        if room.mode == "team":
            if not room.teams:
                room.init_teams(clamp(as_int(settings.get("team_count", 2), default=2), 2, MAX_TEAMS),
                                settings.get("team_names", []))
            room.assign_teams_auto()
            room.settings["_turn_team"] = sorted(room.teams.keys())[0]

        _emit_to_all(room, "game_loading", {"message": "🤖 GigaChat генерирует вопросы..."})
        scheduler.spawn(_start_standard, room, topic, count, difficulty, num_options)

    def _start_standard(room: Room, topic: str, count: int, difficulty: str, num_options: int) -> None:
        try:
            questions = ai_client.generate_questions(topic, count, difficulty, num_options)
        except Exception as exc:  # noqa: BLE001
            logger.error("question generation failed: %s", exc)
            _emit_to_all(room, "error", {"message": "Ошибка AI. Попробуй ещё раз."})
            return
        if not questions:
            _emit_to_all(room, "error", {"message": "Не удалось получить вопросы."})
            return
        room.questions = questions
        room.state = "playing"
        room.current_q = 0
        room.q_start_time = time.time()
        room.game_start_time = time.time()
        room.answer_log = []
        room.recent_correct = []
        room.reset_answers()
        for sid, player in room.players.items():
            _socketio.emit("game_started", {
                "your_team": player.team, "mode": room.mode,
                "is_spectator": player.is_spectator, "teams": room.teams_list(),
                "team_names": {tid: t.name for tid, t in room.teams.items()},
                "presentation": bool(room.settings.get("presentation_mode")),
                "total_questions": room.total_questions,
            }, room=sid)
        _emit_question(room)

    def _start_svoyaigra(room: Room, difficulty: str, num_options: int) -> None:
        categories_setting = room.settings.get("si_categories") or []
        rows = clamp(as_int(room.settings.get("si_rows", SI_ROWS), default=SI_ROWS), 3, 5)
        cols = len(categories_setting) if categories_setting else SI_COLS
        cols = clamp(cols, 1, 6)
        categories = [as_str(c, max_len=40) for c in categories_setting][:cols] or \
            [f"Тема {i + 1}" for i in range(cols)]
        values = [100 * (r + 1) for r in range(rows)]
        total = rows * cols
        _emit_to_all(room, "game_loading",
                     {"message": f"🤖 Генерируем {total} вопросов для Своей игры..."})

        questions: dict[str, dict] = {}
        for col, category in enumerate(categories):
            try:
                generated = ai_client.generate_questions(category, rows, difficulty, num_options)
            except Exception as exc:  # noqa: BLE001
                logger.error("SI category '%s' failed: %s", category, exc)
                generated = []
            for row in range(rows):
                question = dict(generated[row]) if row < len(generated) else {
                    "question": f"Вопрос по теме «{category}», уровень {row + 1}",
                    "options": [f"Вариант {chr(65 + i)}" for i in range(num_options)],
                    "correct": 0, "explanation": "", "hint": "",
                }
                question["value"] = values[row]
                question["category"] = category
                questions[f"{row}_{col}"] = question

        room.state = "playing"
        room.game_start_time = time.time()
        room.answer_log = []
        room.settings["_si"] = {
            "categories": categories, "rows": rows, "cols": cols, "values": values,
            "questions": questions, "opened": {}, "current_cell": None,
            "first_buzzer": None, "selector_sid": None,
        }
        for sid, player in room.players.items():
            board = {
                "categories": categories, "rows": rows, "cols": cols, "values": values,
                "opened": [], "special": {},
            }
            if player.is_cheat:
                board["special"] = {k: q.get("special") for k, q in questions.items() if q.get("special")}
            _socketio.emit("game_started", {
                "mode": "svoyaigra", "is_spectator": player.is_spectator, "si_board": board,
                "scores": {s: p.score for s, p in room.players.items()},
                "presentation": bool(room.settings.get("presentation_mode")),
            }, room=sid)
        _si_next_selector(room)

    # ── Svoya Igra ───────────────────────────────────────────
    def _si_state(room: Room) -> dict:
        return room.settings.setdefault("_si", {})

    def _si_next_selector(room: Room) -> None:
        state = _si_state(room)
        humans = [p for p in room.players.values() if not p.is_spectator]
        if not humans:
            return
        sids = [p.sid for p in humans]
        current = state.get("selector_sid")
        next_sid = sids[(sids.index(current) + 1) % len(sids)] if current in sids else sids[0]
        state["selector_sid"] = next_sid
        _emit_to_all(room, "svoyaigra_select_turn", {
            "selector": room.players[next_sid].name, "selector_sid": next_sid,
            "opened": list(state.get("opened", {}).keys()),
            "scores": {sid: p.score for sid, p in room.players.items()},
        })
        total = state.get("rows", SI_ROWS) * state.get("cols", SI_COLS)
        if len(state.get("opened", {})) >= total:
            _si_finish(room)

    def _si_finish(room: Room) -> None:
        duration = int(time.time() - room.game_start_time) if room.game_start_time else 0
        results = room.final_results()
        room.state = "finished"
        leaderboard.update_leaderboard(results["players"], duration=duration)
        _emit_to_all(room, "game_over", results)

    @socketio.on("svoyaigra_select_cell")
    def on_si_select(data=None):
        """Open a board cell and broadcast its question (Svoya Igra)."""
        data = data or {}
        room = _room_of(request.sid)
        if not room or room.mode != "svoyaigra":
            return
        state = _si_state(room)
        if state.get("selector_sid") != request.sid and not _is_host_or_cheater(request.sid, room):
            _err("Сейчас не ваш ход")
            return
        row = as_int(data.get("row", 0), default=0, minimum=0, maximum=10)
        col = as_int(data.get("col", 0), default=0, minimum=0, maximum=10)
        cell = f"{row}_{col}"
        if state.get("opened", {}).get(cell):
            _err("Вопрос уже открыт")
            return
        question = state.get("questions", {}).get(cell)
        if not question:
            _err("Вопрос не найден")
            return
        state["current_cell"] = cell
        state["first_buzzer"] = None
        state["buzz_locked"] = False
        room.reset_answers()
        _emit_to_all(room, "svoyaigra_question", {
            "cell": cell, "row": row, "col": col, "question": question["question"],
            "options": question.get("options", []), "value": question.get("value", 100),
            "category": question.get("category", ""),
        })

    @socketio.on("svoyaigra_buzz")
    def on_si_buzz(_data=None):
        """Register the first player to buzz in on the open cell."""
        room = _room_of(request.sid)
        if not room or room.mode != "svoyaigra":
            return
        state = _si_state(room)
        player = _player(room, request.sid)
        if not player or player.is_spectator:
            return
        if state.get("first_buzzer") or state.get("buzz_locked"):
            return
        state["first_buzzer"] = request.sid
        _emit_to_all(room, "svoyaigra_buzzed", {"player": player.name, "sid": request.sid})

    @socketio.on("svoyaigra_answer")
    def on_si_answer(data=None):
        """Score a buzzer's answer and resolve the cell."""
        data = data or {}
        room = _room_of(request.sid)
        if not room or room.mode != "svoyaigra":
            return
        state = _si_state(room)
        player = _player(room, request.sid)
        if not player:
            return
        cell = state.get("current_cell")
        question = state.get("questions", {}).get(cell) if cell else None
        if not question:
            return
        if state.get("first_buzzer") and state["first_buzzer"] != request.sid:
            return
        correct_index = question.get("correct", 0)
        value = question.get("value", 100)
        if as_int(data.get("answer_index", -1), default=-1) == correct_index:
            player.score += value
            state.setdefault("opened", {})[cell] = True
            _emit_to_all(room, "svoyaigra_result", {
                "correct": True, "player": player.name, "value": value,
                "correct_index": correct_index, "explanation": question.get("explanation", ""),
                "scores": {sid: p.score for sid, p in room.players.items()},
                "opened": list(state.get("opened", {}).keys()),
            })
            _si_next_selector(room)
        else:
            player.score = max(0, player.score - value // 2)
            state["first_buzzer"] = None
            _emit_to_all(room, "svoyaigra_wrong", {
                "player": player.name, "penalty": value // 2,
                "scores": {sid: p.score for sid, p in room.players.items()},
            })

    @socketio.on("svoyaigra_host_reveal")
    def on_si_reveal(_data=None):
        """Reveal the answer and award/deduct points as host."""
        room = _room_of(request.sid)
        if not room or not _is_host_or_cheater(request.sid, room):
            return
        state = _si_state(room)
        cell = state.get("current_cell")
        question = state.get("questions", {}).get(cell) if cell else None
        if not question:
            return
        state.setdefault("opened", {})[cell] = True
        _emit_to_all(room, "svoyaigra_result", {
            "correct": None, "correct_index": question.get("correct", 0),
            "explanation": question.get("explanation", ""),
            "scores": {sid: p.score for sid, p in room.players.items()},
            "opened": list(state.get("opened", {}).keys()),
        })
        _si_next_selector(room)

    # ── Answering ────────────────────────────────────────────
    @socketio.on("submit_answer")
    def on_submit_answer(data=None):
        """Record a player's answer and score it when everyone has replied."""
        data = data or {}
        room = _room_of(request.sid)
        player = _player(room, request.sid)
        if not room or room.state != "playing" or not player or player.answered or player.is_spectator:
            return
        question = room.current_question
        if not question:
            return
        room.touch()
        answer = as_int(data.get("answer_index", -1), default=-1)
        is_correct = answer == question["correct"]
        player.answered = True
        player.answer_index = answer
        player.answer_time = time.time()

        if is_correct:
            points = room.award_point(request.sid)
        else:
            points = 0
            room.reset_streak(request.sid)
            if room.mode == "lives":
                lives = room.lose_life(request.sid)
                emit("life_lost", {"lives": lives, "eliminated": lives == 0})
                if lives == 0:
                    _emit_to_all(room, "player_eliminated", {"name": player.name})

        if room.mode == "ffa":
            if is_correct and room.ffa_first is None:
                room.ffa_first = request.sid
                _emit_to_all(room, "ffa_correct", {"player_name": player.name, "points": points})
            if room.ffa_first is not None or room.all_answered():
                scheduler.spawn(_resolve_question, room)
            return

        emit("answer_ack", {"correct": is_correct, "points": points,
                            "streak": player.streak, "answer_index": answer})
        if not player.is_invisible:
            _socketio.emit("player_answered", {"name": player.name},
                           room=room.code, skip_sid=request.sid)
        if room.all_answered():
            scheduler.spawn(_resolve_question, room)

    @socketio.on("use_joker")
    def on_use_joker(_data=None):
        """Spend points to eliminate two wrong options."""
        room = _room_of(request.sid)
        player = _player(room, request.sid)
        if not room or not player or room.state != "playing":
            return
        if player.score < JOKER_COST:
            _err(f"Нужно {JOKER_COST} очков.")
            return
        remaining = room.use_joker(request.sid)
        if remaining is None:
            _err("Джокер недоступен.")
            return
        emit("joker_result", {"keep_indices": remaining, "cost": JOKER_COST,
                              "new_score": player.score})

    @socketio.on("get_hint")
    def on_get_hint(_data=None):
        """Spend points to request an AI hint for the current question."""
        room = _room_of(request.sid)
        player = _player(room, request.sid)
        if not room or not player or room.state != "playing":
            return
        if player.score < HINT_COST:
            _err(f"Нужно {HINT_COST} очков.")
            return
        if player.answered:
            _err("Вы уже ответили.")
            return
        question = room.current_question
        if not question:
            return
        player.score -= HINT_COST
        sid, score = request.sid, player.score
        cached = as_str(question.get("hint"))
        if cached:
            emit("hint_received", {"hint": cached, "cost": HINT_COST, "new_score": score})
            return
        text = question["question"]

        def _generate():
            hint = ai_client.generate_hint(text)
            _socketio.emit("hint_received", {"hint": hint, "cost": HINT_COST, "new_score": score}, room=sid)

        scheduler.spawn(_generate)

    @socketio.on("rephrase_question")
    def on_rephrase(_data=None):
        """Spend points to have the AI reword the current question."""
        room = _room_of(request.sid)
        player = _player(room, request.sid)
        if not room or not player or room.state != "playing":
            return
        question = room.current_question
        if not question:
            return
        if not player.is_cheat:
            if player.answered:
                _err("Вы уже ответили.")
                return
            if player.score < REPHRASE_COST:
                _err(f"Нужно {REPHRASE_COST} очков для перефразировки.")
                return
            player.score -= REPHRASE_COST
        sid = request.sid
        cached = as_str(question.get("rephrased_question"))
        if cached:
            emit("question_rephrased", {"original": question["question"],
                                        "rephrased": cached,
                                        "new_score": room.players[sid].score if sid in room.players else 0})
            return
        text = question["question"]

        def _do():
            try:
                result = ai_client.call_gigachat(
                    "Переформулируй вопрос викторины более простым языком, сохранив смысл и правильный ответ. "
                    "НЕ изменяй варианты ответов. Ответь ТОЛЬКО новой формулировкой без пояснений.\n"
                    f"Вопрос: {text}",
                    system="Ты помогаешь переформулировать вопросы викторины.",
                    retries=1,
                ).strip()
            except Exception:  # noqa: BLE001
                result = text
            score = room.players[sid].score if sid in room.players else 0
            _socketio.emit("question_rephrased", {"original": text, "rephrased": result,
                                                  "new_score": score}, room=sid)

        scheduler.spawn(_do)

    @socketio.on("reaction")
    def on_reaction(data=None):
        """Broadcast an emoji reaction from a player."""
        data = data or {}
        room = _room_of(request.sid)
        player = _player(room, request.sid)
        if not room or not player:
            return
        emoji = as_str(data.get("emoji"))
        if emoji not in ALLOWED_REACTIONS:
            return
        _emit_to_all(room, "reaction_received", {"emoji": emoji, "player": player.name})

    # ── Chat ─────────────────────────────────────────────────
    @socketio.on("chat_message")
    def on_send_chat(data=None):
        """Append a chat message and fan it out to the room."""
        data = data or {}
        room = _room_of(request.sid)
        player = _player(room, request.sid)
        if not room or not player:
            return
        message = as_str(data.get("message"), max_len=MAX_CHAT_LEN + 1)
        if not message:
            return
        if len(message) > MAX_CHAT_LEN:
            _err(f"Сообщение слишком длинное (макс. {MAX_CHAT_LEN} символов).")
            return
        record = chat_store.append(room.code, name=player.name, text=message,
                                   is_host=(player.sid == room.host_sid))
        _emit_to_all(room, "chat_message", record)

    @socketio.on("chat_clear")
    def on_chat_clear(_data=None):
        """Clear the room chat history (host or cheater only)."""
        room = _room_of(request.sid)
        if not room or not _is_host_or_cheater(request.sid, room):
            return
        chat_store.clear(room.code)
        _emit_to_all(room, "chat_cleared", {})

    @socketio.on("chat_delete_message")
    def on_chat_delete(data=None):
        """Delete a single chat message (host or cheater only)."""
        data = data or {}
        room = _room_of(request.sid)
        if not room or not _is_host_or_cheater(request.sid, room):
            return
        ts = data.get("ts")
        if ts is None:
            return
        if chat_store.delete(room.code, ts):
            _emit_to_all(room, "chat_deleted", {"ts": ts})

    # ── Presentation mode ────────────────────────────────────
    @socketio.on("set_presentation_mode")
    def on_presentation(data=None):
        """Toggle the big-screen presentation layout."""
        data = data or {}
        room = _room_of(request.sid)
        if not room or not _is_host_or_cheater(request.sid, room):
            return
        enabled = as_bool(data.get("enabled"))
        room.settings["presentation_mode"] = enabled
        _emit_to_all(room, "presentation_mode_changed", {"enabled": enabled})
        emit("cheat_ack", {"feature": "presentation_mode", "enabled": enabled})

    # ── Cheat panel ──────────────────────────────────────────
    def _require_cheater():
        room = _room_of(request.sid)
        player = _player(room, request.sid)
        if not room or not player or not player.is_cheat:
            return None, None
        return room, player

    @socketio.on("cheat_update_score")
    def on_cheat_update_score(data=None):
        """Set the caller's own score (cheat mode)."""
        data = data or {}
        room, player = _require_cheater()
        if not room:
            return
        player.score = max(0, as_int(data.get("score", player.score), default=player.score))
        emit("cheat_score_updated", {"sid": request.sid, "score": player.score})
        _broadcast_players(room)

    @socketio.on("cheat_rename_player")
    def on_cheat_rename(data=None):
        """Rename a player in the room (cheat mode)."""
        data = data or {}
        room, _ = _require_cheater()
        if not room:
            return
        target = _player(room, as_str(data.get("target_sid")))
        new_name = as_str(data.get("new_name"), max_len=20)
        if not target or not new_name:
            return
        old_name = target.name
        if not room.rename_player(target.sid, new_name):
            _err("Ник уже занят.")
            return
        _emit_to_all(room, "player_renamed", {"sid": target.sid, "old_name": old_name,
                                              "new_name": target.name})
        _broadcast_players(room)
        emit("cheat_ack", {"feature": "rename", "ok": True})

    @socketio.on("cheat_set_infinite_lives")
    def on_cheat_infinite_lives(data=None):
        """Toggle infinite lives for the caller (cheat mode)."""
        data = data or {}
        room, player = _require_cheater()
        if not room:
            return
        player.infinite_lives = as_bool(data.get("enabled"))
        emit("cheat_ack", {"feature": "infinite_lives", "enabled": player.infinite_lives})

    @socketio.on("cheat_set_lives")
    def on_cheat_set_lives(data=None):
        """Set a named player's lives (cheat mode)."""
        data = data or {}
        room, _ = _require_cheater()
        if not room:
            return
        name = as_str(data.get("name"))
        target = next((p for p in room.players.values() if p.name.lower() == name.lower()), None)
        if not target:
            _err("Игрок не найден")
            return
        lives = room.set_lives(target.sid, clamp(as_int(data.get("lives", 3), default=3), 0, 10))
        _emit_to_all(room, "lives_restored", {"name": target.name, "lives": lives})
        _broadcast_players(room)
        emit("cheat_ack", {"feature": "set_lives", "ok": True})

    @socketio.on("cheat_add_score_all")
    def on_cheat_add_score_all(data=None):
        """Add points to every active player (cheat mode)."""
        data = data or {}
        room, _ = _require_cheater()
        if not room:
            return
        amount = as_int(data.get("amount", 100), default=100)
        for player in room.players.values():
            if not player.is_spectator:
                player.score = max(0, player.score + amount)
        _emit_to_all(room, "scores_updated", {"scores": {s: p.score for s, p in room.players.items()}})
        _broadcast_players(room)
        emit("cheat_ack", {"feature": "add_score_all", "ok": True})

    @socketio.on("cheat_skip_question")
    def on_cheat_skip(_data=None):
        """Skip the current question and advance (cheat mode)."""
        room, _ = _require_cheater()
        if not room or room.state != "playing" or not room.current_question:
            return
        # Claim the current index so a racing timer/answer cannot also resolve it.
        if room.resolved_q == room.current_q:
            return
        room.resolved_q = room.current_q
        emit("cheat_ack", {"feature": "skip_question", "ok": True})
        _emit_to_all(room, "question_result", {
            "correct_index": room.current_question["correct"],
            "correct_answer": "", "explanation": "Вопрос пропущен", "player_answers": {},
            "scores": {sid: p.score for sid, p in room.players.items()},
            "team_scores": room.team_scores(), "team_names": {t: v.name for t, v in room.teams.items()},
            "lives": {sid: p.lives for sid, p in room.players.items()}, "mode": room.mode,
            "is_bonus": False, "new_difficulty": room.difficulty,
        })
        scheduler.spawn(_advance_after_skip, room)

    def _advance_after_skip(room: Room) -> None:
        scheduler.sleep(2)
        if room.state != "playing":
            return
        if room.advance_question():
            _emit_question(room)
        else:
            _finish_game(room)

    @socketio.on("cheat_force_start")
    def on_cheat_force_start(_data=None):
        """Force-start the game regardless of readiness (cheat mode)."""
        room, _ = _require_cheater()
        if not room:
            return
        emit("cheat_ack", {"feature": "force_start", "ok": True})
        on_start_game({})

    @socketio.on("cheat_reset_player")
    def on_cheat_reset_player(data=None):
        """Reset a named player's in-room score and streak (cheat mode)."""
        data = data or {}
        room, _ = _require_cheater()
        if not room:
            return
        name = as_str(data.get("name"))
        target = next((p for p in room.players.values() if p.name.lower() == name.lower()), None)
        if not target:
            _err("Игрок не найден")
            return
        target.score = 0
        target.streak = 0
        target.total_correct = 0
        emit("cheat_player_reset", {"name": target.name})
        _broadcast_players(room)

    @socketio.on("cheat_reset_global_stats")
    def on_cheat_reset_global(data=None):
        """Wipe a username's global leaderboard stats (cheat mode)."""
        data = data or {}
        room, _ = _require_cheater()
        if not room:
            return
        username = as_str(data.get("username"))
        ok = _reset_lb(username)
        emit("cheat_ack", {"feature": "reset_global_stats", "ok": ok})

    @socketio.on("cheat_teleport")
    def on_cheat_teleport(data=None):
        """Move the caller into another room by code (cheat mode)."""
        data = data or {}
        _, player = _require_cheater()
        if not player:
            return
        code = as_str(data.get("room_code")).upper()
        target_room = registry.get(code)
        if target_room is None:
            _err("Комната не найдена")
            return
        _player_left(request.sid)
        join_room(code)
        name = as_str(data.get("name") or player.name, max_len=20) or player.name
        if target_room.name_taken(name):
            name = f"{name}{int(time.time()) % 1000}"
        target_room.add_player(request.sid, name, is_cheat=True)
        registry.index(request.sid, code)
        target_room.touch()
        emit("cheat_ack", {"feature": "teleport", "ok": True})
        _broadcast_players(target_room)
        emit("room_joined", {
            "room_code": code, "is_host": False, "is_spectator": False,
            "players": target_room.players_list(viewer_sid=request.sid),
            "settings": target_room.settings, "is_sandbox": target_room.is_sandbox,
            "teams": target_room.teams_list(), "team_draft_active": target_room.team_draft_active,
        })

    # ── Cheat: additional tester tooling ─────────────────────
    @socketio.on("cheat_toggle_invisibility")
    def on_cheat_invisibility(data=None):
        """Toggle the caller's own invisibility in the room (cheat mode).

        Invisible players are hidden from the roster but still receive every
        question; this is useful for silently spectating a live game.
        """
        data = data or {}
        room, player = _require_cheater()
        if not room:
            return
        player.is_invisible = as_bool(data.get("enabled"))
        emit("cheat_ack", {"feature": "invisibility", "enabled": player.is_invisible})
        _broadcast_players(room)

    @socketio.on("cheat_fill_answer")
    def on_cheat_fill_answer(data=None):
        """Answer every remaining player correctly at once (cheat mode).

        Every active player who has not answered is marked correct and awarded
        points, then the question is resolved.  Handy to skip the wait when
        testing the post-question screens.
        """
        data = data or {}
        room, _ = _require_cheater()
        if not room or room.state != "playing" or not room.current_question:
            return
        if room.resolved_q == room.current_q:
            return
        for player in room.answering_players():
            if player.answered or player.instant_answer:
                continue
            player.answered = True
            player.answer_index = room.current_question["correct"]
            player.answer_time = time.time()
            room.award_point(player.sid)
            if not player.is_invisible:
                _socketio.emit("player_answered", {"name": player.name},
                               room=room.code, skip_sid=player.sid)
        emit("cheat_ack", {"feature": "fill_answer", "ok": True})
        scheduler.spawn(_resolve_question, room)

    @socketio.on("cheat_reveal_answer")
    def on_cheat_reveal_answer(_data=None):
        """Force-resolve the current question immediately (cheat mode)."""
        room, _ = _require_cheater()
        if not room or room.state != "playing" or not room.current_question:
            return
        if room.resolved_q == room.current_q:
            return
        emit("cheat_ack", {"feature": "reveal_answer", "ok": True})
        scheduler.spawn(_resolve_question, room)

    @socketio.on("cheat_clear_chat")
    def on_cheat_clear_chat(_data=None):
        """Wipe the room chat (cheat mode)."""
        room, _ = _require_cheater()
        if not room:
            return
        chat_store.clear(room.code)
        _emit_to_all(room, "chat_cleared", {})
        emit("cheat_ack", {"feature": "clear_chat", "ok": True})

    @socketio.on("cheat_reset_answers")
    def on_cheat_reset_answers(_data=None):
        """Re-open the current question so everyone can answer again (cheat mode)."""
        room, _ = _require_cheater()
        if not room or room.state != "playing" or not room.current_question:
            return
        room.reset_answers()
        room.resolved_q = -1
        emit("cheat_ack", {"feature": "reset_answers", "ok": True})
        _broadcast_players(room)
        _emit_question(room)

    @socketio.on("cheat_set_difficulty")
    def on_cheat_set_difficulty(data=None):
        """Force the adaptive difficulty for the room (cheat mode)."""
        data = data or {}
        room, _ = _require_cheater()
        if not room:
            return
        difficulty = validate_difficulty(data.get("difficulty"))
        room.current_difficulty = difficulty
        room.settings["difficulty"] = difficulty
        emit("cheat_ack", {"feature": "set_difficulty", "ok": True, "difficulty": difficulty})
        _broadcast_players(room)

    @socketio.on("cheat_grant_power")
    def on_cheat_grant_power(data=None):
        """Give the caller unlimited jokers and hints (cheat mode).

        The ``instant_answer`` flag makes the timeout logic skip this player, so
        a tester can take their time without being auto-failed.
        """
        data = data or {}
        room, player = _require_cheater()
        if not room:
            return
        enabled = as_bool(data.get("enabled"))
        player.instant_answer = enabled
        if enabled:
            player.infinite_lives = True
        emit("cheat_ack", {"feature": "godmode", "enabled": enabled})

    # ── Admin panel ──────────────────────────────────────────
    def _require_admin_room(data):
        if not _is_admin():
            return None
        return registry.get(as_str((data or {}).get("room_code")).upper())

    @socketio.on("admin_kick_player")
    def on_admin_kick(data=None):
        """Remove a player from a room (admin only)."""
        data = data or {}
        if not _is_admin():
            _admin_action("kick", ok=False)
            return
        room = _require_admin_room(data)
        if not room:
            _admin_action("kick", ok=False)
            return
        name = as_str(data.get("player_name"))
        target = next((p for p in room.players.values() if p.name.lower() == name.lower()), None)
        if not target:
            _admin_action("kick", ok=False)
            return
        target_sid = target.sid
        room.remove_player(target_sid)
        registry.unindex(target_sid)
        _socketio.emit("kicked", {"reason": "Исключён администратором"}, room=target_sid)
        _socketio.emit("player_kicked", {"name": target.name}, room=room.code)
        _broadcast_players(room)
        _admin_action("kick", ok=True)

    @socketio.on("admin_ban_player")
    def on_admin_ban(data=None):
        """Ban a player by name and eject them (admin only)."""
        data = data or {}
        if not _is_admin():
            _admin_action("ban", ok=False)
            return
        room = _require_admin_room(data)
        name = as_str(data.get("player_name"))
        ok = leaderboard.ban(name, as_str(data.get("reason")), as_int(data.get("duration_minutes", 60), default=60))
        if ok and room:
            target = next((p for p in room.players.values() if p.name.lower() == name.lower()), None)
            if target:
                target_sid = target.sid
                room.remove_player(target_sid)
                registry.unindex(target_sid)
                _socketio.emit("kicked", {"reason": "Заблокирован администратором"}, room=target_sid)
                _broadcast_players(room)
        _admin_action("ban", ok=ok)

    @socketio.on("admin_force_end_game")
    def on_admin_force_end(data=None):
        """Terminate the running game in a room (admin only)."""
        data = data or {}
        if not _is_admin():
            _admin_action("force_end", ok=False)
            return
        room = _require_admin_room(data)
        if not room:
            _admin_action("force_end", ok=False)
            return
        room.state = "finished"
        results = room.final_results()
        results["admin_terminated"] = True
        _emit_to_all(room, "game_over", results)
        _admin_action("force_end", ok=True)

    @socketio.on("admin_take_host")
    def on_admin_take_host(data=None):
        """Become host of a room (admin only)."""
        data = data or {}
        if not _is_admin():
            _admin_action("take_host", ok=False)
            return
        room = _require_admin_room(data)
        if not room:
            _admin_action("take_host", ok=False)
            return
        room.host_sid = request.sid
        if request.sid not in room.players:
            room.add_player(request.sid, "Admin", is_cheat=True)
        room.players[request.sid].is_cheat = True
        registry.index(request.sid, room.code)
        join_room(room.code)
        _broadcast_players(room)
        _admin_action("take_host", ok=True)

    @socketio.on("admin_transfer_host")
    def on_admin_transfer_host(data=None):
        """Hand host to a named player (admin only)."""
        data = data or {}
        if not _is_admin():
            _admin_action("transfer_host", ok=False)
            return
        room = _require_admin_room(data)
        if not room:
            _admin_action("transfer_host", ok=False)
            return
        name = as_str(data.get("player_name"))
        target = next((p for p in room.players.values() if p.name.lower() == name.lower()), None)
        if not target:
            _admin_action("transfer_host", ok=False)
            return
        room.host_sid = target.sid
        _emit_to_all(room, "host_changed", {"host": target.name})
        _broadcast_players(room)
        _admin_action("transfer_host", ok=True)

    @socketio.on("admin_chat_system")
    def on_admin_chat_system(data=None):
        """Post a system message into a room's chat (admin only)."""
        data = data or {}
        if not _is_admin():
            _admin_action("chat_system", ok=False)
            return
        room = _require_admin_room(data)
        if not room:
            _admin_action("chat_system", ok=False)
            return
        text = as_str(data.get("text"), max_len=MAX_CHAT_LEN)
        if not text:
            _admin_action("chat_system", ok=False)
            return
        record = chat_store.append(room.code, name="Система", text=text, is_system=True)
        _emit_to_all(room, "chat_message", record)
        _admin_action("chat_system", ok=True)
