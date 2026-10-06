"""Tests for the HTTP API surface."""

from __future__ import annotations


class TestCore:
    def test_index_renders(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "BrainStorm" in response.get_data(as_text=True) or "Мозговой" in response.get_data(as_text=True)

    def test_health_reports_databases(self, client):
        payload = client.get("/health").get_json()
        assert payload["status"] == "ok"
        assert payload["databases"] == {"leaderboard": "ok", "users": "ok"}

    def test_readyz(self, client):
        assert client.get("/readyz").get_json() == {"status": "ready"}

    def test_unknown_route_returns_json_404(self, client):
        response = client.get("/api/does-not-exist")
        assert response.status_code == 404
        assert response.get_json()["ok"] is False


class TestAuth:
    def test_register_creates_session(self, client):
        response = client.post("/api/auth/register", json={"username": "alice", "password": "supersecret"})
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        assert client.get("/api/auth/me").get_json()["logged_in"] is True

    def test_short_password_is_rejected(self, client):
        response = client.post("/api/auth/register", json={"username": "bob", "password": "123"})
        assert response.status_code == 400

    def test_duplicate_username_conflicts(self, client):
        client.post("/api/auth/register", json={"username": "carol", "password": "supersecret"})
        response = client.post("/api/auth/register", json={"username": "carol", "password": "supersecret"})
        assert response.status_code == 409

    def test_login_with_wrong_password_is_401(self, client):
        client.post("/api/auth/register", json={"username": "dave", "password": "supersecret"})
        client.post("/api/auth/logout")
        response = client.post("/api/auth/login", json={"username": "dave", "password": "wrongpass"})
        assert response.status_code == 401

    def test_login_error_does_not_reveal_existence(self, client):
        client.post("/api/auth/register", json={"username": "erin", "password": "supersecret"})
        client.post("/api/auth/logout")
        missing = client.post("/api/auth/login", json={"username": "ghost", "password": "whatever"})
        wrong = client.post("/api/auth/login", json={"username": "erin", "password": "wrongpass"})
        assert missing.get_json()["error"] == wrong.get_json()["error"]

    def test_logout_clears_session(self, client):
        client.post("/api/auth/register", json={"username": "frank", "password": "supersecret"})
        client.post("/api/auth/logout")
        assert client.get("/api/auth/me").get_json()["logged_in"] is False

    def test_me_returns_xp_progression(self, client):
        client.post("/api/auth/register", json={"username": "grace", "password": "supersecret"})
        user = client.get("/api/auth/me").get_json()["user"]
        assert "xp_next_level" in user and "level" in user


class TestProtectedRoutes:
    def test_profile_requires_login(self, client):
        assert client.get("/api/profile").status_code == 401

    def test_ugc_create_requires_login(self, client):
        assert client.post("/api/ugc/create", json={}).status_code == 401

    def test_admin_requires_activation(self, client):
        assert client.get("/api/admin/users").status_code == 401

    def test_admin_activate_with_wrong_key(self, client):
        assert client.post("/api/admin/activate", json={"key": "nope"}).status_code == 403


class TestUGC:
    def test_create_and_list_question(self, client):
        client.post("/api/auth/register", json={"username": "author", "password": "supersecret"})
        response = client.post("/api/ugc/create", json={
            "question": "Сколько будет два плюс два?",
            "options": ["3", "4", "5", "6"],
            "correct": 1,
            "topic": "математика",
            "difficulty": 1,
        })
        assert response.status_code == 200
        assert response.get_json()["coins_earned"] == 5

    def test_rejects_duplicate_options(self, client):
        client.post("/api/auth/register", json={"username": "author2", "password": "supersecret"})
        response = client.post("/api/ugc/create", json={
            "question": "Вопрос с дубликатами вариантов?",
            "options": ["a", "a"],
            "correct": 0,
        })
        assert response.status_code == 400

    def test_rejects_profanity(self, client):
        client.post("/api/auth/register", json={"username": "author3", "password": "supersecret"})
        response = client.post("/api/ugc/create", json={
            "question": "Текст с матом блядь внутри вопроса",
            "options": ["a", "b"],
            "correct": 0,
        })
        assert response.status_code == 400


class TestShopAndCampaign:
    def test_shop_lists_items(self, client):
        payload = client.get("/api/shop/items").get_json()
        assert "hint_free" in payload["items"]

    def test_buy_without_coins_fails(self, client):
        client.post("/api/auth/register", json={"username": "poor", "password": "supersecret"})
        response = client.post("/api/shop/buy", json={"item_id": "double_xp"})
        assert response.status_code == 400

    def test_campaign_levels_for_guest_are_locked(self, client):
        payload = client.get("/api/campaign/levels").get_json()
        assert all(level["locked"] for level in payload["levels"])

    def test_campaign_result_grants_stars(self, client):
        client.post("/api/auth/register", json={"username": "hero", "password": "supersecret"})
        response = client.post("/api/campaign/result", json={
            "level_id": 1, "score": 900, "correct": 9, "total_questions": 10,
        })
        assert response.get_json()["stars"] == 3


class TestLearnValidation:
    def test_short_text_is_rejected(self, client):
        response = client.post("/api/learn/from_text", json={"content": "too short"})
        assert response.status_code == 400

    def test_invalid_url_is_rejected(self, client):
        response = client.post("/api/learn/from_url", json={"url": "ftp://example.com"})
        assert response.status_code == 400


class TestCheatActivation:
    def test_activate_with_correct_code(self, client):
        from app.config import settings
        response = client.post("/api/cheat/activate",
                               json={"code": settings.cheat_tester_code, "username": "tester"})
        assert response.status_code == 200
        assert client.get("/api/cheat/check").get_json()["is_tester"] is True

    def test_activate_requires_username(self, client):
        response = client.post("/api/cheat/activate", json={"code": "x"})
        assert response.status_code == 400
