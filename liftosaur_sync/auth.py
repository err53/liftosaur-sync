from __future__ import annotations

import os
import urllib.parse
import webbrowser

from liftosaur_sync.http import http_form_json
from liftosaur_sync.models import StravaTokens


STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_DEFAULT_REDIRECT_URI = "http://localhost/exchange_token"
STRAVA_SCOPES = ("activity:read_all", "activity:write")


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def authorize_strava_interactively(redirect_uri: str | None = None, env_path: str = ".env") -> StravaTokens:
    client_id = require_env("STRAVA_CLIENT_ID")
    client_secret = require_env("STRAVA_CLIENT_SECRET")
    redirect_uri = redirect_uri or os.environ.get("STRAVA_REDIRECT_URI") or STRAVA_DEFAULT_REDIRECT_URI
    url = build_strava_authorize_url(client_id, redirect_uri)
    print("Open this Strava authorization URL:")
    print(url)
    webbrowser.open(url)
    redirected = input("Paste the full redirected URL, or just the code parameter: ").strip()
    code = extract_strava_code(redirected)
    tokens = exchange_strava_code(client_id, client_secret, code)
    persist_env_value(env_path, "STRAVA_REFRESH_TOKEN", tokens.refresh_token)
    if redirect_uri != STRAVA_DEFAULT_REDIRECT_URI:
        persist_env_value(env_path, "STRAVA_REDIRECT_URI", redirect_uri)
    os.environ["STRAVA_REFRESH_TOKEN"] = tokens.refresh_token
    return tokens


def get_strava_access_token(env_path: str = ".env", interactive: bool = True) -> str:
    client_id = require_env("STRAVA_CLIENT_ID")
    client_secret = require_env("STRAVA_CLIENT_SECRET")
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN")
    if not refresh_token:
        if not interactive:
            raise SystemExit("Missing required environment variable: STRAVA_REFRESH_TOKEN")
        return authorize_strava_interactively(env_path=env_path).access_token
    tokens = refresh_strava_access_token(client_id, client_secret, refresh_token)
    if tokens.refresh_token != refresh_token:
        persist_env_value(env_path, "STRAVA_REFRESH_TOKEN", tokens.refresh_token)
        os.environ["STRAVA_REFRESH_TOKEN"] = tokens.refresh_token
    return tokens.access_token


def build_strava_authorize_url(client_id: str, redirect_uri: str, scopes: tuple[str, ...] = STRAVA_SCOPES) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": ",".join(scopes),
    }
    return STRAVA_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def extract_strava_code(value: str) -> str:
    if not value:
        raise ValueError("Strava OAuth response is empty")
    parsed = urllib.parse.urlparse(value)
    if parsed.query:
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("error"):
            raise ValueError(f"Strava OAuth failed: {params['error'][0]}")
        codes = params.get("code")
        if codes and codes[0]:
            return codes[0]
    return value


def exchange_strava_code(client_id: str, client_secret: str, code: str) -> StravaTokens:
    payload = http_form_json(
        "POST",
        STRAVA_TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    return _strava_tokens_from_payload(payload)


def refresh_strava_access_token(client_id: str, client_secret: str, refresh_token: str) -> StravaTokens:
    payload = http_form_json(
        "POST",
        STRAVA_TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    return _strava_tokens_from_payload(payload)


def persist_env_value(path: str, key: str, value: str) -> None:
    line = f"{key}={value}\n"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as file:
            lines = file.readlines()
    else:
        lines = []
    replaced = False
    next_lines: list[str] = []
    for existing in lines:
        stripped = existing.strip()
        if stripped and not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == key:
            next_lines.append(line)
            replaced = True
        else:
            next_lines.append(existing)
    if not replaced:
        if next_lines and not next_lines[-1].endswith("\n"):
            next_lines[-1] += "\n"
        next_lines.append(line)
    with open(path, "w", encoding="utf-8") as file:
        file.writelines(next_lines)


def _strava_tokens_from_payload(payload: object) -> StravaTokens:
    if not isinstance(payload, dict):
        raise RuntimeError("Strava token response was not an object")
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Strava token response did not include access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError("Strava token response did not include refresh_token")
    expires_at = payload.get("expires_at")
    return StravaTokens(access_token, refresh_token, int(expires_at) if expires_at is not None else None)
