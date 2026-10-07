"""In-memory game domain.

This module contains no Flask or SocketIO imports: it is pure state plus
behaviour, which makes it trivially unit-testable.  The realtime layer is
responsible for transport; the domain only mutates state and returns results.

Models
------
:class:`Team`
    A named group of players with an optional captain.
:class:`Player`
    A single participant (human, spectator, invisible or cheater).
:class:`Room`
    The aggregate root: settings, players, questions, scoring and phase.
"""

from __future__ import annotations

import random
import string
import time
from dataclasses import dataclass, field

from ..logging_setup import get_logger

logger = get_logger(__name__)

# ── Scoring constants ────────────────────────────────────────
SCORE_MULTIPLIERS = {"easy": 1.0, "medium": 1.5, "hard": 2.0}
BASE_SCORE = 100
TIME_BONUS = 50
MAX_TIME = 30.0
STREAK_BONUS_STEP = 10
STREAK_BONUS_CAP = 50
JOKER_COST = 100
HINT_COST = 75
REPHRASE_COST = 50
BONUS_MULTIPLIER = 2.0
MAX_TEAMS = 7
DEFAULT_LIVES = 3
DIFFICULTY_WINDOW = 15
DIFFICULTY_MIN_SAMPLES = 6
DIFFICULTY_EASY_THRESHOLD = 0.45
DIFFICULTY_HARD_THRESHOLD = 0.80
VALID_DIFFICULTIES = ("easy", "medium", "hard")

# ── Room lifecycle constants ─────────────────────────────────
ROOM_IDLE_TIMEOUT = 90
ROOM_EMPTY_TIMEOUT = 30
CODE_LENGTH = 6

_STATE_WAITING = "waiting"
_STATE_PLAYING = "playing"
_STATE_FINISHED = "finished"


def gen_code(existing) -> str:
    """Generate a unique upper-case room code not present in ``existing``."""
    for _ in range(1000):
        code = "".join(random.choices(string.ascii_uppercase, k=CODE_LENGTH))
        if code not in existing:
            return code
    raise RuntimeError("Не удалось сгенерировать уникальный код комнаты")


@dataclass
class Team:
    """A team of players with an optional captain."""

    id: int
    name: str
    leader_sid: str | None = None
    members: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialise the team for transport."""
        return {"id": self.id, "name": self.name, "leader_sid": self.leader_sid,
                "members": list(self.members)}


@dataclass
class Player:
    """A single game participant."""

    sid: str
    name: str
    score: int = 0
    team: int | None = None
    answered: bool = False
    answer_index: int | None = None
    answer_time: float = 0.0
    streak: int = 0
    total_correct: int = 0
    lives: int = DEFAULT_LIVES
    joker_used: bool = False
    is_spectator: bool = False
    is_invisible: bool = False
    infinite_lives: bool = False
    instant_answer: bool = False
    is_cheat: bool = False
    join_time: float = field(default_factory=time.time)

    def reset_answer(self) -> None:
        """Clear the per-question answer state."""
        self.answered = False
        self.answer_index = None
        self.answer_time = 0.0

    def to_dict(self, *, is_host: bool = False, reveal_invisible: bool = False) -> dict | None:
        """Serialise the player; invisible players return ``None`` unless revealed."""
        if self.is_invisible and not reveal_invisible:
            return None
        return {
            "name": self.name,
            "score": self.score,
            "team": self.team,
            "is_host": is_host,
            "total_correct": self.total_correct,
            "lives": self.lives,
            "is_spectator": self.is_spectator,
            "is_invisible": self.is_invisible,
            "sid": self.sid,
        }


@dataclass
class Room:
    """The aggregate root for a single game session."""

    code: str
    host_sid: str
    settings: dict = field(default_factory=dict)
    players: dict[str, Player] = field(default_factory=dict)
    state: str = _STATE_WAITING
    questions: list[dict] = field(default_factory=list)
    current_q: int = 0
    q_start_time: float = 0.0
    ffa_first: str | None = None
    teams: dict[int, Team] = field(default_factory=dict)
    team_draft_active: bool = False
    draft_turn_team: int = 1
    recent_correct: list[bool] = field(default_factory=list)
    current_difficulty: str = "medium"
    keep_scores: bool = False
    is_public: bool = False
    is_sandbox: bool = False
    game_start_time: float = 0.0
    answer_log: list[dict] = field(default_factory=list)
    #: Index of the question currently being resolved; guards against two
    #: concurrent resolvers (e.g. the timer and a fast correct answer) both
    #: advancing the game and skipping a question.
    resolved_q: int = -1
    last_activity: float = field(default_factory=time.time)

    # ── Derived properties ───────────────────────────────────
    def touch(self) -> None:
        """Refresh the last-activity timestamp."""
        self.last_activity = time.time()

    @property
    def mode(self) -> str:
        """Configured game mode."""
        return self.settings.get("game_mode", "classic")

    @property
    def difficulty(self) -> str:
        """Current (possibly adaptive) difficulty."""
        return self.current_difficulty

    @property
    def total_questions(self) -> int:
        """Number of questions in the round."""
        return len(self.questions)

    @property
    def current_question(self) -> dict | None:
        """The question being answered, or ``None``."""
        if 0 <= self.current_q < len(self.questions):
            return self.questions[self.current_q]
        return None

    @property
    def human_players(self) -> list[Player]:
        """All non-spectator players."""
        return [p for p in self.players.values() if not p.is_spectator]

    @property
    def active_players(self) -> list[Player]:
        """Players still able to answer (respects lives mode)."""
        if self.mode == "lives":
            return [p for p in self.players.values() if not p.is_spectator and p.lives > 0]
        return [p for p in self.players.values() if not p.is_spectator]

    # ── Player management ────────────────────────────────────
    def add_player(self, sid: str, name: str, *, spectator: bool = False,
                   invisible: bool = False, is_cheat: bool = False) -> Player:
        """Register a player in the room."""
        player = Player(sid=sid, name=name, is_spectator=spectator,
                        is_invisible=invisible, is_cheat=is_cheat)
        self.players[sid] = player
        return player

    def remove_player(self, sid: str) -> Player | None:
        """Remove a player and detach them from any team."""
        player = self.players.pop(sid, None)
        if player is None:
            return None
        for team in self.teams.values():
            if sid in team.members:
                team.members.remove(sid)
            if team.leader_sid == sid:
                team.leader_sid = None
        return player

    def rename_player(self, sid: str, new_name: str) -> bool:
        """Rename a player unless the new name is already taken."""
        player = self.players.get(sid)
        if player is None or not new_name:
            return False
        if self.name_taken(new_name, exclude_sid=sid):
            return False
        player.name = new_name[:20]
        return True

    def name_taken(self, name: str, exclude_sid: str | None = None) -> bool:
        """Return ``True`` when another player already uses ``name``."""
        lowered = (name or "").lower()
        return any(
            sid != exclude_sid and p.name.lower() == lowered
            for sid, p in self.players.items()
        )

    def players_list(self, viewer_sid: str | None = None) -> list[dict]:
        """Serialise players; only cheaters can see invisible players."""
        reveal = False
        if viewer_sid and viewer_sid in self.players:
            reveal = self.players[viewer_sid].is_cheat
        result = []
        for player in self.players.values():
            data = player.to_dict(is_host=(player.sid == self.host_sid), reveal_invisible=reveal)
            if data is not None:
                result.append(data)
        return result

    def reassign_host(self) -> Player | None:
        """Promote the earliest-joined human player when the host leaves."""
        if self.host_sid in self.players:
            return self.players[self.host_sid]
        humans = self.human_players
        if not humans:
            return None
        new_host = min(humans, key=lambda p: p.join_time)
        self.host_sid = new_host.sid
        return new_host

    # ── Teams ────────────────────────────────────────────────
    def init_teams(self, count: int = 2, names: list[str] | None = None) -> None:
        """(Re)create ``count`` teams with optional custom names."""
        count = max(2, min(MAX_TEAMS, int(count)))
        self.teams = {}
        for index in range(count):
            name = names[index] if names and index < len(names) and str(names[index]).strip() \
                else f"Команда {index + 1}"
            self.teams[index + 1] = Team(id=index + 1, name=str(name)[:30])

    def assign_team_leaders(self) -> None:
        """Assign the earliest joiners as captains for draft mode."""
        humans = sorted(self.human_players, key=lambda p: p.join_time)
        for index, team in enumerate(self.teams.values()):
            if index < len(humans):
                team.leader_sid = humans[index].sid
                team.members = [humans[index].sid]
                humans[index].team = team.id

    def assign_teams_auto(self) -> None:
        """Distribute all players evenly across teams."""
        if not self.teams:
            self.init_teams(2)
        team_ids = sorted(self.teams.keys())
        sids = [p.sid for p in self.human_players]
        random.shuffle(sids)
        for team in self.teams.values():
            team.members = []
        for index, sid in enumerate(sids):
            team_id = team_ids[index % len(team_ids)]
            self.teams[team_id].members.append(sid)
            self.players[sid].team = team_id

    def team_scores(self) -> dict[int, int]:
        """Aggregate scores per team id."""
        scores = dict.fromkeys(self.teams, 0)
        for player in self.players.values():
            if player.team in scores:
                scores[player.team] += player.score
        return scores

    def teams_list(self) -> list[dict]:
        """Serialise all teams."""
        return [team.to_dict() for team in self.teams.values()]

    def draft_pick(self, captain_sid: str, target_sid: str) -> tuple[bool, str]:
        """Let a captain pick an undrafted player during team draft."""
        captain_team = next((t for t in self.teams.values() if t.leader_sid == captain_sid), None)
        if captain_team is None:
            return False, "Вы не капитан"
        if captain_team.id != self.draft_turn_team:
            return False, "Не ваша очередь"
        target = self.players.get(target_sid)
        if target is None or target.team is not None or target.is_spectator:
            return False, "Игрок недоступен"
        captain_team.members.append(target_sid)
        target.team = captain_team.id

        team_ids = sorted(self.teams.keys())
        current = team_ids.index(self.draft_turn_team)
        self.draft_turn_team = team_ids[(current + 1) % len(team_ids)]

        leaders = {t.leader_sid for t in self.teams.values()}
        undrafted = [p for p in self.human_players if p.team is None and p.sid not in leaders]
        if not undrafted:
            self.team_draft_active = False
        return True, "OK"

    # ── Question flow ────────────────────────────────────────
    def reset_answers(self) -> None:
        """Clear answer state for a new question."""
        for player in self.players.values():
            player.reset_answer()
            player.joker_used = False
        self.ffa_first = None

    def answering_players(self) -> list[Player]:
        """Players expected to answer the current question."""
        active = self.active_players
        if self.mode == "team":
            turn = self.settings.get("_turn_team", 1)
            active = [p for p in active if p.team == turn]
        return active

    def all_answered(self) -> bool:
        """Return ``True`` when every expected player has answered."""
        expected = self.answering_players()
        return all(p.answered for p in expected) if expected else True

    def advance_question(self) -> bool:
        """Move to the next question; returns ``False`` when the game ends."""
        if self.mode == "team" and self.teams:
            team_ids = sorted(self.teams.keys())
            current = self.settings.get("_turn_team", team_ids[0])
            index = team_ids.index(current) if current in team_ids else 0
            self.settings["_turn_team"] = team_ids[(index + 1) % len(team_ids)]

        self.current_q += 1
        if self.current_q >= self.total_questions:
            self.state = _STATE_FINISHED
            return False
        if self.mode == "lives" and len(self.active_players) <= 1:
            self.state = _STATE_FINISHED
            return False
        self.reset_answers()
        self.q_start_time = time.time()
        return True

    # ── Scoring ──────────────────────────────────────────────
    def award_point(self, sid: str) -> int:
        """Award points for a correct answer and return the amount."""
        player = self.players.get(sid)
        if player is None:
            return 0
        question = self.current_question
        is_bonus = bool(question.get("bonus")) if question else False
        multiplier = SCORE_MULTIPLIERS.get(self.difficulty, 1.0)
        if is_bonus:
            multiplier *= BONUS_MULTIPLIER
        points = int(BASE_SCORE * multiplier)

        elapsed = max(0.0, player.answer_time - self.q_start_time)
        points += max(0, int(TIME_BONUS * (1 - elapsed / MAX_TIME)))

        player.streak += 1
        player.total_correct += 1
        if player.streak >= 3:
            points += min(STREAK_BONUS_CAP, (player.streak - 2) * STREAK_BONUS_STEP)

        if self.mode == "coop":
            for teammate in self.players.values():
                if not teammate.is_spectator:
                    teammate.score += points
        else:
            player.score += points
        return points

    def reset_streak(self, sid: str) -> None:
        """Reset a player's streak after a wrong answer."""
        player = self.players.get(sid)
        if player is not None:
            player.streak = 0

    def lose_life(self, sid: str) -> int:
        """Decrement a player's lives; returns the remaining count."""
        player = self.players.get(sid)
        if player is None or player.lives <= 0:
            return 0
        if player.infinite_lives:
            return player.lives
        player.lives -= 1
        if player.lives == 0:
            player.is_spectator = True
        return player.lives

    def use_joker(self, sid: str) -> list[int] | None:
        """Spend a joker to remove two wrong answers.

        Returns the indices to keep, or ``None`` when unavailable.
        """
        player = self.players.get(sid)
        question = self.current_question
        if player is None or question is None or player.joker_used or player.answered:
            return None
        if player.score < JOKER_COST:
            return None
        options = question.get("options") or []
        correct = question.get("correct", 0)
        wrong = [i for i in range(len(options)) if i != correct]
        if not wrong:
            return None
        player.joker_used = True
        player.score -= JOKER_COST
        return sorted([correct, random.choice(wrong)])

    def set_lives(self, sid: str, lives: int) -> int | None:
        """Force a player's life count (admin/cheat tooling).

        Returns the new count, or ``None`` when the player is unknown.  A player
        dropped to zero becomes a spectator, matching :meth:`lose_life`.
        """
        player = self.players.get(sid)
        if player is None:
            return None
        player.lives = max(0, int(lives))
        if player.lives == 0:
            player.is_spectator = True
        return player.lives

    # ── Adaptive difficulty ──────────────────────────────────
    def record_answer_stat(self, correct: bool) -> None:
        """Record whether the round's answers included a correct one."""
        self.recent_correct.append(bool(correct))
        if len(self.recent_correct) > DIFFICULTY_WINDOW:
            self.recent_correct.pop(0)

    def answer_distribution(self) -> dict:
        """Summarise the live answer spread for the current question.

        Used by the cheat panel's hidden statistics view.  ``answer_players``
        maps an option index to the names that picked it; unanswered and
        spectator players are excluded from ``total_active``.
        """
        active = self.active_players
        counts: dict[int, int] = {}
        players: dict[int, list[str]] = {}
        for player in active:
            index = player.answer_index
            if index is None or index < 0:
                continue
            counts[index] = counts.get(index, 0) + 1
            players.setdefault(index, []).append(player.name)
        answered = sum(counts.values())
        return {
            "answer_counts": {str(k): v for k, v in sorted(counts.items())},
            "answer_players": {str(k): v for k, v in sorted(players.items())},
            "total_answered": answered,
            "total_active": len(active),
        }

    def recalculate_difficulty(self) -> str:
        """Adjust difficulty based on the recent correct-answer ratio."""
        if len(self.recent_correct) < DIFFICULTY_MIN_SAMPLES:
            return self.current_difficulty
        ratio = sum(self.recent_correct) / len(self.recent_correct)
        previous = self.current_difficulty
        if ratio < DIFFICULTY_EASY_THRESHOLD:
            self.current_difficulty = "easy"
        elif ratio > DIFFICULTY_HARD_THRESHOLD:
            self.current_difficulty = "hard"
        else:
            self.current_difficulty = "medium"
        if previous != self.current_difficulty:
            logger.info("Difficulty %s -> %s (%.0f%% correct)",
                        previous, self.current_difficulty, ratio * 100)
        return self.current_difficulty

    # ── Results & reset ──────────────────────────────────────
    def final_results(self) -> dict:
        """Build the end-of-game payload."""
        ordered = sorted(self.players.values(), key=lambda p: (-p.score, p.join_time))
        result: dict = {
            "mode": self.mode,
            "players": [
                {"rank": index + 1, "name": p.name, "score": p.score, "team": p.team,
                 "total_correct": p.total_correct, "is_spectator": p.is_spectator}
                for index, p in enumerate(ordered)
            ],
        }
        if self.mode == "team":
            scores = self.team_scores()
            result["team_scores"] = scores
            result["winner_team"] = max(scores, key=scores.get) if scores else None
            result["team_names"] = {tid: t.name for tid, t in self.teams.items()}
        if self.mode == "lives":
            result["survivors"] = [p.name for p in self.players.values() if p.lives > 0 and not p.is_spectator]
        return result

    def reset_for_restart(self, keep_scores: bool = False) -> None:
        """Return the room to the lobby, optionally keeping scores."""
        self.state = _STATE_WAITING
        self.questions = []
        self.current_q = 0
        self.resolved_q = -1
        self.ffa_first = None
        self.recent_correct = []
        self.answer_log = []
        self.current_difficulty = self.settings.get("difficulty", "medium")
        self.settings.pop("_turn_team", None)
        self.settings.pop("_si", None)
        for team in self.teams.values():
            team.members = []
            team.leader_sid = None
        self.team_draft_active = False
        for player in self.players.values():
            player.reset_answer()
            player.streak = 0
            player.joker_used = False
            player.is_spectator = False
            player.lives = DEFAULT_LIVES
            player.team = None
            if not keep_scores:
                player.score = 0
                player.total_correct = 0
