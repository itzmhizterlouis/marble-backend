def test_register_refresh_and_logout(client):
    registered = client.post(
        "/v1/auth/register",
        json={"name": "Ada Creator", "email": "ada@example.com", "password": "creator123"},
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["user"]["email"] == "ada@example.com"
    assert registered.json()["user"]["email_verified"] is False
    assert registered.json()["access_token"]
    assert "marble_refresh" in registered.cookies

    refreshed = client.post("/v1/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"] != registered.json()["access_token"]

    logged_out = client.post("/v1/auth/logout")
    assert logged_out.status_code == 200
    assert client.post("/v1/auth/refresh").status_code == 401


def test_rejects_duplicate_registration(client):
    payload = {"name": "Kofi Creator", "email": "kofi@example.com", "password": "creator123"}
    assert client.post("/v1/auth/register", json=payload).status_code == 201
    duplicate = client.post("/v1/auth/register", json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "email_taken"
