import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from endstone_landclaim.web_dashboard import app as dashboard  # noqa: E402


PUBLIC_URL = "https://landclaim.oninetwork.com"
CALLBACK_URL = f"{PUBLIC_URL}/auth/callback"


class StubResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def client(tmp_path):
    dashboard.configure(
        data_folder=str(tmp_path),
        public_url=PUBLIC_URL,
        discord_client_id="client id with spaces",
        discord_client_secret="client-secret",
        allowed_discord_ids=["42"],
        master_password="test-master-password",
    )
    dashboard.app.config.update(TESTING=True)
    with dashboard.app.test_client() as test_client:
        yield test_client


def begin_discord_login(client):
    response = client.get("/auth/login", base_url=PUBLIC_URL)
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["Location"]).query)
    return query


def test_discord_login_uses_canonical_encoded_callback(client):
    query = begin_discord_login(client)

    assert query["client_id"] == ["client id with spaces"]
    assert query["redirect_uri"] == [CALLBACK_URL]
    assert query["scope"] == ["identify"]
    assert query["state"][0]


def test_allowed_discord_user_can_authenticate(client, monkeypatch):
    query = begin_discord_login(client)
    token_request = {}

    def post(_url, data, timeout):
        token_request.update(data)
        assert timeout == 10
        return StubResponse(200, {"access_token": "discord-token"})

    def get(_url, headers, timeout):
        assert headers == {"Authorization": "Bearer discord-token"}
        assert timeout == 10
        return StubResponse(200, {"id": "42", "username": "Approved User", "avatar": None})

    monkeypatch.setattr(dashboard.requests, "post", post)
    monkeypatch.setattr(dashboard.requests, "get", get)

    response = client.get(
        "/auth/callback",
        query_string={"state": query["state"][0], "code": "oauth-code"},
        base_url=PUBLIC_URL,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert token_request["redirect_uri"] == CALLBACK_URL
    assert client.get("/auth/me", base_url=PUBLIC_URL).get_json()["id"] == "42"


def test_unlisted_discord_user_is_rejected(client, monkeypatch):
    query = begin_discord_login(client)
    monkeypatch.setattr(
        dashboard.requests,
        "post",
        lambda *_args, **_kwargs: StubResponse(200, {"access_token": "discord-token"}),
    )
    monkeypatch.setattr(
        dashboard.requests,
        "get",
        lambda *_args, **_kwargs: StubResponse(200, {"id": "99", "username": "Unknown User"}),
    )

    response = client.get(
        "/auth/callback",
        query_string={"state": query["state"][0], "code": "oauth-code"},
        base_url=PUBLIC_URL,
    )

    assert response.status_code == 403
    assert client.get("/auth/me", base_url=PUBLIC_URL).get_json() is None


def test_empty_allow_list_disables_discord_login(tmp_path):
    dashboard.configure(
        data_folder=str(tmp_path),
        public_url=PUBLIC_URL,
        discord_client_id="client-id",
        discord_client_secret="client-secret",
        allowed_discord_ids=[],
    )

    with dashboard.app.test_client() as test_client:
        info = test_client.get("/auth/info", base_url=PUBLIC_URL).get_json()
        response = test_client.get("/auth/login", base_url=PUBLIC_URL)

    assert info["discord"] is False
    assert response.status_code == 503


def test_master_password_remains_available(client):
    rejected = client.post(
        "/auth/password",
        json={"password": "wrong"},
        base_url=PUBLIC_URL,
    )
    accepted = client.post(
        "/auth/password",
        json={"password": "test-master-password"},
        base_url=PUBLIC_URL,
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.get_json()["user"]["auth_method"] == "password"
