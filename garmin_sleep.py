#!/usr/bin/env python3
"""
Fetch latest Garmin sleep data.

Auth strategy (in order):
  1. Reuse MCP token cache at ~/.garmin-mcp/ — avoids any re-login
  2. Refresh via OAuth1→OAuth2 exchange when the bearer token expires
  3. Fall back to garminconnect library fresh login (last resort)

On 401, the script reauthenticates automatically and retries once.
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

GARMIN_API = "https://connectapi.garmin.com"
OAUTH_EXCHANGE = f"{GARMIN_API}/oauth-service/oauth/exchange/user/2.0"
PROFILE_URL = f"{GARMIN_API}/userprofile-service/socialProfile"
SLEEP_ENDPOINT = "/wellness-service/wellness/dailySleepData"

MCP_TOKEN_DIR = Path.home() / ".garmin-mcp"
GARTH_TOKEN_DIR = Path.home() / ".garth"
USER_AGENT = "com.garmin.android.apps.connectmobile"
LOOKBACK_DAYS = 7


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_credentials() -> tuple[str, str]:
    email = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")
    if email and password:
        return email, password

    # Read from Claude Code's MCP config (~/.claude.json)
    claude_cfg = Path.home() / ".claude.json"
    if claude_cfg.exists():
        try:
            data = json.loads(claude_cfg.read_text())
            cwd = str(Path.cwd())
            # Prefer project-scoped config, then any project with real creds
            candidates = list(data.get("projects", {}).values())
            for priority_first in [
                [p for p in candidates if any(str(Path.cwd()) in k for k in data.get("projects", {}))],
                candidates,
            ]:
                for project in priority_first:
                    env = project.get("mcpServers", {}).get("garmin", {}).get("env", {})
                    e = env.get("GARMIN_EMAIL", "")
                    p = env.get("GARMIN_PASSWORD", "")
                    if e and p and e != "you@email.com":
                        return e, p
        except Exception:
            pass

    print(
        "Error: Garmin credentials not found.\n"
        "Set GARMIN_EMAIL and GARMIN_PASSWORD environment variables, or configure\n"
        "the Garmin MCP server:  claude mcp add garmin -e GARMIN_EMAIL=you@email.com "
        "-e GARMIN_PASSWORD=yourpass -- npx -y @nicolasvegam/garmin-connect-mcp",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# MCP token cache helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_mcp_tokens() -> tuple[dict, dict, dict]:
    """Return (oauth1, oauth2, profile) dicts from ~/.garmin-mcp/."""
    oauth1 = _read_json(MCP_TOKEN_DIR / "oauth1_token.json")
    oauth2 = _read_json(MCP_TOKEN_DIR / "oauth2_token.json")
    profile = _read_json(MCP_TOKEN_DIR / "profile.json")
    return oauth1, oauth2, profile


def save_mcp_tokens(oauth2: dict, profile: dict) -> None:
    _write_json(MCP_TOKEN_DIR / "oauth2_token.json", oauth2)
    if profile:
        _write_json(MCP_TOKEN_DIR / "profile.json", profile)


def is_oauth2_valid(oauth2: dict) -> bool:
    expires_at = oauth2.get("expires_at") or oauth2.get("expires_in")
    if not expires_at:
        return bool(oauth2.get("access_token"))
    # expires_at is epoch seconds
    return time.time() < (expires_at - 60)


# ---------------------------------------------------------------------------
# OAuth1→OAuth2 exchange (no Cloudflare involved)
# ---------------------------------------------------------------------------

def exchange_oauth1_for_oauth2(oauth1: dict, consumer: dict | None = None) -> dict:
    """Exchange a long-lived OAuth1 token for a fresh OAuth2 bearer."""
    import hmac
    import hashlib
    import base64
    import urllib.parse
    import secrets

    if not consumer:
        # Fetch the consumer key/secret (same as MCP does)
        r = requests.get(
            "https://thegarth.s3.amazonaws.com/oauth_consumer.json",
            timeout=10,
        )
        r.raise_for_status()
        consumer = r.json()

    oauth_token = oauth1.get("oauth_token") or oauth1.get("token")
    oauth_token_secret = oauth1.get("oauth_token_secret") or oauth1.get("token_secret")
    consumer_key = consumer.get("consumer_key") or consumer.get("key")
    consumer_secret = consumer.get("consumer_secret") or consumer.get("secret")

    if not all([oauth_token, oauth_token_secret, consumer_key, consumer_secret]):
        raise ValueError("Incomplete OAuth1 token or consumer data")

    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    method = "GET"
    url = OAUTH_EXCHANGE

    params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp,
        "oauth_token": oauth_token,
        "oauth_version": "1.0",
    }
    sorted_params = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(params.items())
    )
    base_string = "&".join([
        urllib.parse.quote(method, safe=""),
        urllib.parse.quote(url, safe=""),
        urllib.parse.quote(sorted_params, safe=""),
    ])
    signing_key = f"{urllib.parse.quote(consumer_secret, safe='')}&{urllib.parse.quote(oauth_token_secret, safe='')}"
    signature = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()
    params["oauth_signature"] = signature

    auth_header = "OAuth " + ", ".join(
        f'{k}="{urllib.parse.quote(v, safe="")}"' for k, v in sorted(params.items())
    )

    resp = requests.get(
        url,
        headers={"Authorization": auth_header, "User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    token_data = resp.json()
    token_data.setdefault("expires_at", time.time() + token_data.get("expires_in", 3600))
    return token_data


# ---------------------------------------------------------------------------
# Direct Garmin API client
# ---------------------------------------------------------------------------

class GarminSession:
    def __init__(self, oauth2: dict, profile: dict, oauth1: dict = None):
        self.oauth2 = oauth2
        self.oauth1 = oauth1 or {}
        self.profile = profile
        self._consumer: dict | None = None

    @property
    def display_name(self) -> str:
        return self.profile.get("displayName", "")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.oauth2['access_token']}",
            "User-Agent": USER_AGENT,
        }

    def _refresh(self) -> None:
        print("Refreshing OAuth2 token via OAuth1 exchange...", file=sys.stderr)
        if not self._consumer:
            r = requests.get("https://thegarth.s3.amazonaws.com/oauth_consumer.json", timeout=10)
            r.raise_for_status()
            self._consumer = r.json()
        self.oauth2 = exchange_oauth1_for_oauth2(self.oauth1, self._consumer)
        if not self.profile and self.oauth2.get("access_token"):
            self.profile = self._fetch_profile()
        save_mcp_tokens(self.oauth2, self.profile)

    def _fetch_profile(self) -> dict:
        r = requests.get(PROFILE_URL, headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def get(self, path: str, params: dict = None) -> dict:
        """GET with automatic 401 retry."""
        url = path if path.startswith("http") else f"{GARMIN_API}{path}"
        for attempt in range(2):
            r = requests.get(url, headers=self._headers(), params=params, timeout=15)
            if r.status_code == 401 and attempt == 0:
                print("401 received — reauthenticating...", file=sys.stderr)
                if self.oauth1:
                    self._refresh()
                else:
                    raise RuntimeError(
                        "OAuth2 token expired and no OAuth1 token available for refresh.\n"
                        "Use Claude Code to trigger the Garmin MCP (it will re-authenticate),\n"
                        "then run this script again."
                    )
                continue
            r.raise_for_status()
            return r.json()
        return {}


# ---------------------------------------------------------------------------
# Build session
# ---------------------------------------------------------------------------

def build_session_from_mcp() -> GarminSession | None:
    """Try to build a session from MCP token cache."""
    oauth1, oauth2, profile = load_mcp_tokens()
    if not oauth2.get("access_token") and not oauth1:
        return None

    if not is_oauth2_valid(oauth2) and oauth1:
        try:
            oauth2 = exchange_oauth1_for_oauth2(oauth1)
            save_mcp_tokens(oauth2, profile)
        except Exception as e:
            print(f"OAuth1→OAuth2 exchange failed: {e}", file=sys.stderr)
            return None

    if not oauth2.get("access_token"):
        return None

    session = GarminSession(oauth2=oauth2, profile=profile, oauth1=oauth1)
    if not session.display_name:
        try:
            session.profile = session._fetch_profile()
            save_mcp_tokens(oauth2, session.profile)
        except Exception:
            pass

    return session


def build_session_via_garminconnect(email: str, password: str) -> GarminSession:
    """Fall back: use garminconnect library for fresh login, then wrap in GarminSession."""
    try:
        from garminconnect import Garmin, GarminConnectAuthenticationError
    except ImportError:
        print("pip3 install garminconnect  # required for fresh login", file=sys.stderr)
        sys.exit(1)

    api = Garmin(email=email, password=password)
    if GARTH_TOKEN_DIR.exists():
        try:
            api.login(tokenstore=str(GARTH_TOKEN_DIR))
        except Exception:
            api.login()
            api.garth.dump(str(GARTH_TOKEN_DIR))
    else:
        api.login()
        api.garth.dump(str(GARTH_TOKEN_DIR))

    # Extract tokens from garth and write to MCP cache so future runs skip login
    try:
        garth_oauth2 = {
            "access_token": api.garth.oauth2_token.access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "expires_at": time.time() + 3600,
        }
        garth_oauth1 = {
            "oauth_token": api.garth.oauth1_token.oauth_token,
            "oauth_token_secret": api.garth.oauth1_token.oauth_token_secret,
        }
        _write_json(MCP_TOKEN_DIR / "oauth2_token.json", garth_oauth2)
        _write_json(MCP_TOKEN_DIR / "oauth1_token.json", garth_oauth1)
        profile = {"displayName": api.display_name}
        _write_json(MCP_TOKEN_DIR / "profile.json", profile)
        return GarminSession(oauth2=garth_oauth2, profile=profile, oauth1=garth_oauth1)
    except Exception:
        # garth internals may differ — just use garminconnect directly
        class WrappedSession:
            def __init__(self, a):
                self._api = a
                self.display_name = a.display_name

            def get(self, path: str, params: dict = None) -> dict:
                from garminconnect import GarminConnectAuthenticationError
                try:
                    r = requests.get(
                        f"{GARMIN_API}{path}" if not path.startswith("http") else path,
                        headers={
                            "Authorization": f"Bearer {self._api.garth.oauth2_token.access_token}",
                            "User-Agent": USER_AGENT,
                        },
                        params=params,
                        timeout=15,
                    )
                    r.raise_for_status()
                    return r.json()
                except Exception:
                    return self._api.get_sleep_data(params.get("date", date.today().isoformat()))

        return WrappedSession(api)


# ---------------------------------------------------------------------------
# Sleep fetch
# ---------------------------------------------------------------------------

def fetch_sleep(session: GarminSession, cdate: str) -> dict:
    display = session.display_name or "~"
    path = f"{SLEEP_ENDPOINT}/{display}"
    return session.get(path, params={"date": cdate, "nonSleepBufferMinutes": "60"})


def find_latest_sleep(session: GarminSession) -> tuple[str, dict]:
    for days_back in range(LOOKBACK_DAYS + 1):
        target = date.today() - timedelta(days=days_back)
        cdate = target.isoformat()
        try:
            data = fetch_sleep(session, cdate)
            if data and data.get("dailySleepDTO"):
                return cdate, data
        except Exception:
            continue
    return date.today().isoformat(), {}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "N/A"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m"


def fmt_time(ts: int | None) -> str:
    if ts is None:
        return "N/A"
    return datetime.fromtimestamp(ts / 1000).strftime("%H:%M")


def print_sleep(cdate: str, data: dict) -> None:
    dto = data.get("dailySleepDTO", {})
    if not dto:
        print(f"No sleep data found for the past {LOOKBACK_DAYS} days.")
        return

    print(f"\n{'=' * 52}")
    print(f"  Sleep summary — {cdate}")
    print(f"{'=' * 52}")
    print(f"  Bedtime         {fmt_time(dto.get('sleepStartTimestampGMT'))}")
    print(f"  Wake time       {fmt_time(dto.get('sleepEndTimestampGMT'))}")
    print(f"  Total sleep     {fmt_duration(dto.get('sleepTimeSeconds'))}")
    print()
    print(f"  Deep            {fmt_duration(dto.get('deepSleepSeconds'))}")
    print(f"  Light           {fmt_duration(dto.get('lightSleepSeconds'))}")
    print(f"  REM             {fmt_duration(dto.get('remSleepSeconds'))}")
    print(f"  Awake           {fmt_duration(dto.get('awakeSleepSeconds'))}")
    print()

    score = dto.get("sleepScores") or data.get("sleepScores")
    if isinstance(score, dict):
        overall = score.get("overall", {})
        value = overall.get("value") if isinstance(overall, dict) else overall
        print(f"  Sleep score     {value if value is not None else 'N/A'}")
    elif isinstance(score, (int, float)):
        print(f"  Sleep score     {score}")

    if dto.get("restingHeartRate"):
        print(f"  Resting HR      {dto['restingHeartRate']} bpm")
    if dto.get("avgSleepStress") is not None:
        print(f"  Avg stress      {dto['avgSleepStress']:.0f}")

    print(f"{'=' * 52}\n")

    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # 1. Try MCP token cache first (no login required)
    session = build_session_from_mcp()

    if session is None:
        # 2. Fresh login via garminconnect
        print("No cached tokens found — logging in (requires GARMIN_EMAIL / GARMIN_PASSWORD)...", file=sys.stderr)
        email, password = get_credentials()
        session = build_session_via_garminconnect(email, password)

    cdate, data = find_latest_sleep(session)
    print_sleep(cdate, data)


if __name__ == "__main__":
    main()
