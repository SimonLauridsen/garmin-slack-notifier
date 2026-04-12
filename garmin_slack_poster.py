#!/usr/bin/env python3
"""
Garmin → Slack Run Notifier
Polls specified Garmin Connect users' activities every 30 minutes and posts
new runs to a Slack channel.

Auth strategy:
  - OAuth2 Bearer token  (~/.garmin-mcp/oauth2_token.json) — own activities
  - DI token             (~/.garmin-mcp/di_token.json)      — social/connections feed
  On 401 both token types are refreshed automatically.
  If the DI token is missing, run `python3 garmin_login.py` once to obtain it.

Run:
    python garmin_slack_poster.py            # continuous daemon (every 30 min)
    python garmin_slack_poster.py --once     # single check and exit (for cron)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── paths ─────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
SEEN_FILE     = BASE_DIR / "seen_activities.json"
MCP_TOKEN_DIR = Path.home() / ".garmin-mcp"
MAX_SEEN_IDS  = 500

# ── Garmin endpoints ──────────────────────────────────────────────────────────
GARMIN_API         = "https://connectapi.garmin.com"
OAUTH_CONSUMER_URL = "https://thegarth.s3.amazonaws.com/oauth_consumer.json"
OAUTH_PREAUTH      = f"{GARMIN_API}/oauth-service/oauth/preauthorized"
OAUTH_EXCHANGE     = f"{GARMIN_API}/oauth-service/oauth/exchange/user/2.0"
PROFILE_URL        = f"{GARMIN_API}/userprofile-service/socialProfile"
ACTIVITIES_SEARCH  = f"{GARMIN_API}/activitylist-service/activities/search/activities"
USER_ACTIVITIES    = f"{GARMIN_API}/activitylist-service/activities"
SSO_EMBED          = "https://sso.garmin.com/sso/embed"
SSO_SIGNIN         = "https://sso.garmin.com/sso/signin"
SSO_ORIGIN         = "https://sso.garmin.com"
PORTAL_SERVICE     = "https://connect.garmin.com/app"
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
UA_MOBILE  = "com.garmin.android.apps.connectmobile"
CSRF_RE    = re.compile(r'name="_csrf"\s+value="(.+?)"')
TICKET_RE  = re.compile(r'ticket=([^"&\s<]+)')

# ── env / config ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    load_dotenv()
    required = ["GARMIN_EMAIL", "GARMIN_PASSWORD", "SLACK_BOT_TOKEN", "SLACK_CHANNEL"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        log.error("Missing required env vars: %s", ", ".join(missing))
        log.error("Copy .env.example to .env and fill in your values.")
        sys.exit(1)

    watch_raw = os.environ.get("GARMIN_WATCH_USERS", "").strip()
    watch_users = [u.strip() for u in watch_raw.split(",") if u.strip()] if watch_raw else []
    if not watch_users:
        log.error("GARMIN_WATCH_USERS is empty. Add at least one Garmin display name.")
        sys.exit(1)

    return {
        "email":       os.environ["GARMIN_EMAIL"],
        "password":    os.environ["GARMIN_PASSWORD"],
        "slack_token": os.environ["SLACK_BOT_TOKEN"],
        "channel":     os.environ["SLACK_CHANNEL"],
        "watch_users": watch_users,
    }


# ── token helpers ─────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _token_valid(token_data: dict) -> bool:
    expires_at = token_data.get("expires_at")
    if not expires_at:
        return bool(token_data.get("access_token"))
    return time.time() < (float(expires_at) - 60)


# ── OAuth1 signing ────────────────────────────────────────────────────────────

def _oauth1_header(
    method: str, base_url: str,
    consumer_key: str, consumer_secret: str,
    token: str = "", token_secret: str = "",
    extra_params: dict | None = None,
) -> str:
    params: dict[str, str] = {
        "oauth_consumer_key":     consumer_key,
        "oauth_nonce":            secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp":        str(int(time.time())),
        "oauth_version":          "1.0",
    }
    if token:
        params["oauth_token"] = token
    if extra_params:
        params.update({k: str(v) for k, v in extra_params.items()})

    sorted_params = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(params.items())
    )
    base_string = "&".join([
        urllib.parse.quote(method.upper(), safe=""),
        urllib.parse.quote(base_url, safe=""),
        urllib.parse.quote(sorted_params, safe=""),
    ])
    signing_key = (
        f"{urllib.parse.quote(consumer_secret, safe='')}"
        f"&{urllib.parse.quote(token_secret, safe='')}"
    )
    sig = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()
    params["oauth_signature"] = sig
    return "OAuth " + ", ".join(
        f'{k}="{urllib.parse.quote(v, safe="")}"' for k, v in sorted(params.items())
    )


# ── web SSO login (avoids rate-limited mobile endpoint) ───────────────────────

def _fetch_consumer() -> dict:
    r = requests.get(OAUTH_CONSUMER_URL, timeout=10)
    r.raise_for_status()
    return r.json()


def _sso_login(email: str, password: str):
    """Returns (embed_ticket, live_sso_session)."""
    s = requests.Session()
    s.headers["User-Agent"] = UA_BROWSER
    s.get(SSO_EMBED, params={"clientId": "GarminConnect", "locale": "en", "service": SSO_EMBED})
    r = s.get(SSO_SIGNIN, params={"id": "gauth-widget", "embedWidget": "true", "locale": "en", "gauthHost": SSO_EMBED})
    csrf = CSRF_RE.search(r.text)
    if not csrf:
        raise RuntimeError("CSRF token not found in SSO page")
    r = s.post(SSO_SIGNIN,
        params={"id": "gauth-widget", "embedWidget": "true", "locale": "en",
                "gauthHost": SSO_EMBED, "clientId": "GarminConnect", "service": SSO_EMBED,
                "source": SSO_EMBED, "redirectAfterAccountLoginUrl": SSO_EMBED,
                "redirectAfterAccountCreationUrl": SSO_EMBED},
        data={"username": email, "password": password, "embed": "true", "_csrf": csrf.group(1)},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Origin": SSO_ORIGIN, "Referer": SSO_SIGNIN, "Dnt": "1"})
    m = TICKET_RE.search(r.text)
    if not m:
        raise GarminConnectAuthenticationError("SSO login failed — check credentials.")
    return m.group(1), s


def _ticket_to_oauth1(ticket: str, consumer: dict) -> dict:
    ck, cs = consumer["consumer_key"], consumer["consumer_secret"]
    qp = {"ticket": ticket, "login-url": SSO_EMBED, "accepts-mfa-tokens": "true"}
    auth = _oauth1_header("GET", OAUTH_PREAUTH, ck, cs, extra_params=qp)
    r = requests.get(f"{OAUTH_PREAUTH}?{urllib.parse.urlencode(qp)}",
                     headers={"Authorization": auth, "User-Agent": UA_MOBILE}, timeout=15)
    r.raise_for_status()
    p = urllib.parse.parse_qs(r.text)
    token, secret = p.get("oauth_token", [None])[0], p.get("oauth_token_secret", [None])[0]
    if not token or not secret:
        raise RuntimeError(f"OAuth1 exchange failed: {r.text}")
    return {"oauth_token": token, "oauth_token_secret": secret}


def _oauth1_to_oauth2(oauth1: dict, consumer: dict) -> dict:
    ck, cs = consumer["consumer_key"], consumer["consumer_secret"]
    auth = _oauth1_header("POST", OAUTH_EXCHANGE, ck, cs,
                          token=oauth1["oauth_token"], token_secret=oauth1["oauth_token_secret"])
    oauth_params = {
        k: urllib.parse.unquote(v.strip('"'))
        for part in auth.removeprefix("OAuth ").split(", ")
        for k, v in [part.split('="', 1)]
    }
    r = requests.post(OAUTH_EXCHANGE, params=oauth_params,
                      headers={"User-Agent": UA_MOBILE, "Content-Type": "application/x-www-form-urlencoded"},
                      timeout=15)
    r.raise_for_status()
    data = r.json()
    data["expires_at"] = int(time.time()) + data.get("expires_in", 3600)
    return data


def _get_portal_ticket(sso_session) -> str | None:
    try:
        r = sso_session.get(SSO_SIGNIN,
            params={"id": "gauth-widget", "embedWidget": "true", "locale": "en",
                    "gauthHost": SSO_EMBED, "clientId": "GarminConnect", "service": PORTAL_SERVICE},
            headers={"User-Agent": UA_BROWSER}, allow_redirects=False)
        location = r.headers.get("Location", "")
        for text in (location, r.text, r.url):
            m = TICKET_RE.search(text)
            if m:
                return m.group(1)
        return None
    except Exception:
        return None


def _portal_ticket_to_di(portal_ticket: str) -> dict | None:
    DI_URL   = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
    DI_GRANT = "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
    for cid in ["GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
                "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
                "GARMIN_CONNECT_MOBILE_ANDROID_DI"]:
        basic = "Basic " + base64.b64encode(f"{cid}:".encode()).decode()
        try:
            r = requests.post(DI_URL,
                headers={"Authorization": basic, "User-Agent": UA_MOBILE,
                         "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={"client_id": cid, "service_ticket": portal_ticket,
                      "grant_type": DI_GRANT, "service_url": PORTAL_SERVICE},
                timeout=15)
            if r.ok:
                d = r.json()
                d["expires_at"] = int(time.time()) + d.get("expires_in", 3600)
                return d
        except Exception:
            continue
    return None


def full_login(email: str, password: str) -> None:
    """Full web SSO login; writes OAuth1/2 and optionally DI tokens to ~/.garmin-mcp/."""
    log.info("Performing full Garmin web SSO login...")
    consumer = _fetch_consumer()
    ticket, sso_session = _sso_login(email, password)
    oauth1  = _ticket_to_oauth1(ticket, consumer)
    oauth2  = _oauth1_to_oauth2(oauth1, consumer)
    r = requests.get(PROFILE_URL,
                     headers={"Authorization": f"Bearer {oauth2['access_token']}", "User-Agent": UA_MOBILE},
                     timeout=10)
    r.raise_for_status()
    d = r.json()
    profile = {"displayName": d.get("displayName", ""), "profileId": d.get("profileId"),
               "fullName": d.get("fullName", "")}

    MCP_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(MCP_TOKEN_DIR / "oauth1_token.json", oauth1)
    _write_json(MCP_TOKEN_DIR / "oauth2_token.json", oauth2)
    _write_json(MCP_TOKEN_DIR / "profile.json", profile)

    # Try to also get a DI token from the same live SSO session
    portal_ticket = _get_portal_ticket(sso_session)
    if portal_ticket:
        di = _portal_ticket_to_di(portal_ticket)
        if di:
            _write_json(MCP_TOKEN_DIR / "di_token.json", di)
            log.info("DI token obtained — connections feed enabled.")
        else:
            log.debug("DI token exchange failed (non-critical).")
    else:
        log.debug("Portal ticket not obtained from SSO session (non-critical).")

    log.info("Logged in as %s", profile.get("fullName") or profile.get("displayName"))


# ── Garmin session ────────────────────────────────────────────────────────────

class GarminSession:
    def __init__(self, email: str, password: str) -> None:
        self.email    = email
        self.password = password
        self._oauth1: dict = {}
        self._oauth2: dict = {}
        self._di:     dict = {}
        self._profile: dict = {}
        self._reload()

    def _reload(self) -> None:
        self._oauth1  = _read_json(MCP_TOKEN_DIR / "oauth1_token.json")
        self._oauth2  = _read_json(MCP_TOKEN_DIR / "oauth2_token.json")
        self._di      = _read_json(MCP_TOKEN_DIR / "di_token.json")
        self._profile = _read_json(MCP_TOKEN_DIR / "profile.json")

    @property
    def has_di_token(self) -> bool:
        return _token_valid(self._di)

    def _refresh_oauth2(self) -> None:
        log.info("Refreshing OAuth2 via OAuth1 exchange...")
        consumer     = _fetch_consumer()
        self._oauth2 = _oauth1_to_oauth2(self._oauth1, consumer)
        _write_json(MCP_TOKEN_DIR / "oauth2_token.json", self._oauth2)

    def _reauthenticate(self) -> None:
        full_login(self.email, self.password)
        self._reload()

    def _ensure_oauth2(self) -> None:
        if _token_valid(self._oauth2):
            return
        if self._oauth1:
            try:
                self._refresh_oauth2()
                return
            except Exception as e:
                log.warning("OAuth1→OAuth2 refresh failed: %s", e)
        self._reauthenticate()

    def _get(self, url: str, bearer: str, params: dict | None = None) -> dict | list:
        for attempt in range(2):
            r = requests.get(url,
                headers={"Authorization": f"Bearer {bearer}", "User-Agent": UA_MOBILE, "Accept": "application/json"},
                params=params, timeout=15)
            if r.status_code == 401 and attempt == 0:
                log.warning("401 — reauthenticating...")
                self._reauthenticate()
                bearer = self._oauth2.get("access_token", "") if "connectapi" in url else self._di.get("access_token", "")
                continue
            if r.status_code == 429:
                raise GarminConnectTooManyRequestsError("Garmin rate limit")
            r.raise_for_status()
            return r.json()
        return {}

    def get_user_activities(self, display_name: str, days_back: int = 3) -> list[dict]:
        """Fetch recent activities for any public Garmin profile by display name."""
        self._ensure_oauth2()
        result = self._get(
            f"{USER_ACTIVITIES}/{display_name}",
            self._oauth2["access_token"],
            {"start": 0, "limit": 20},
        )
        acts = result if isinstance(result, list) else result.get("activityList", [])
        cutoff = (date.today() - timedelta(days=days_back)).isoformat()
        return [a for a in acts if _is_run(a) and _activity_date_iso(a) >= cutoff]

    def lookup_profile(self, display_name: str) -> dict:
        self._ensure_oauth2()
        try:
            return self._get(f"{PROFILE_URL}/{display_name}", self._oauth2["access_token"])
        except Exception:
            return {}

    @property
    def own_display_name(self) -> str:
        return self._profile.get("displayName", "")

    @property
    def own_full_name(self) -> str:
        return self._profile.get("fullName", "") or self._profile.get("displayName", "")


# ── activity helpers ──────────────────────────────────────────────────────────

def _is_run(activity: dict) -> bool:
    type_key = (activity.get("activityType", {}).get("typeKey", "")
                or activity.get("activityType", ""))
    return "running" in str(type_key).lower()


def _activity_date_iso(activity: dict) -> str:
    raw = activity.get("startTimeLocal") or activity.get("startTimeGMT", "")
    return raw[:10] if raw else "1970-01-01"


def _fmt_distance_km(meters: float | None) -> str:
    if not meters:
        return "N/A"
    return f"{meters / 1000:.2f} km"


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "N/A"
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_pace_km(avg_speed_ms: float | None) -> tuple[str, str]:
    """avg_speed in m/s → (formatted pace string, pace emoji)"""
    if not avg_speed_ms or avg_speed_ms <= 0:
        return "N/A", ":thumbsdown-kilse:"
    secs_per_km = 1000 / avg_speed_ms
    m, s = divmod(int(secs_per_km), 60)
    emoji = ":thumbsup-kilse:" if secs_per_km <= 300 else ":thumbsdown-kilse:"
    return f"{m}:{s:02d} /km", emoji


_TRAINING_EFFECT_LABELS = [
    (2.0, "No Benefit"),
    (3.0, "Maintaining"),
    (4.0, "Improving"),
    (4.5, "Highly Improving"),
    (5.1, "Overreaching"),
]

def _fmt_training_effect(value: float | None) -> str:
    if value is None:
        return "N/A"
    label = "Overreaching"
    for threshold, lbl in _TRAINING_EFFECT_LABELS:
        if value < threshold:
            label = lbl
            break
    return f"{value:.1f} — {label}"


_ZONE_COLORS = ["⚪", "🔵", "🟢", "🟠", "🔴"]  # Z1–Z5 matching Garmin's zone colours

def _fmt_hr_zones(activity: dict) -> str | None:
    times = [activity.get(f"hrTimeInZone_{i}") or 0 for i in range(1, 6)]
    if not any(times):
        return None
    parts = []
    for i, (color, t) in enumerate(zip(_ZONE_COLORS, times), 1):
        m, s = divmod(int(t), 60)
        parts.append(f"{color} {m}:{s:02d}")
    return "  ".join(parts)


def _fmt_date(activity: dict) -> str:
    raw = activity.get("startTimeLocal") or activity.get("startTimeGMT", "")
    try:
        return datetime.fromisoformat(raw[:10]).strftime("%B %-d, %Y")
    except Exception:
        return raw[:10] if raw else "Unknown date"


def _display_name_for(activity: dict) -> str:
    return (activity.get("ownerFullName")
            or activity.get("ownerDisplayName")
            or "Unknown")


# ── Slack ─────────────────────────────────────────────────────────────────────

def post_run(slack: WebClient, channel: str, activity: dict) -> None:
    name             = _display_name_for(activity)
    distance         = _fmt_distance_km(activity.get("distance"))
    duration         = _fmt_duration(activity.get("duration"))
    pace, pace_emoji = _fmt_pace_km(activity.get("averageSpeed"))
    run_date         = _fmt_date(activity)
    act_name         = activity.get("activityName") or "Running"
    avg_hr           = activity.get("averageHR")
    hr_str           = f"{int(avg_hr)} bpm" if avg_hr else "N/A"
    hr_zones         = _fmt_hr_zones(activity)
    training_effect  = _fmt_training_effect(activity.get("aerobicTrainingEffect"))
    vo2max           = activity.get("vO2MaxValue")
    vo2max_str       = str(int(vo2max)) if vo2max else "N/A"

    lines = [
        f"🏃 *{name} just finished a run!*",
        f"📅 {run_date}",
        f"📏 Distance: {distance}",
        f"⏱️ Duration: {duration}",
        f"{pace_emoji} Avg Pace: {pace}",
        f"❤️ Avg HR: {hr_str}",
    ]
    if hr_zones:
        lines.append(f"🫀 HR Zones: {hr_zones}")
    lines += [
        f"⚡ Training Effect: {training_effect}",
        f"🫁 VO2 Max: {vo2max_str}",
        f"🔗 Activity: {act_name}",
    ]
    text = "\n".join(lines)
    try:
        resp = slack.chat_postMessage(channel=channel, text=text, unfurl_links=False)
        log.info("Posted: %s — %s %s", name, distance, duration)
        return resp["ts"]
    except SlackApiError as e:
        log.error("Slack error: %s", e.response["error"])
    return None


# ── seen activities ───────────────────────────────────────────────────────────

def load_seen() -> tuple[set[str], dict[str, str]]:
    data = _read_json(SEEN_FILE)
    return set(data.get("seen_ids", [])), data.get("threads", {})


def save_seen(seen: set[str], threads: dict[str, str]) -> None:
    ids = sorted(seen)[-MAX_SEEN_IDS:]
    # Prune threads for IDs that were evicted
    pruned = {aid: ts for aid, ts in threads.items() if aid in set(ids)}
    _write_json(SEEN_FILE, {"seen_ids": ids, "threads": pruned})


# ── main check ────────────────────────────────────────────────────────────────

def check_and_post(session: GarminSession, slack: WebClient,
                   watch_users: list[str], channel: str) -> None:

    seen, threads = load_seen()
    name_cache: dict[str, str] = {}  # display_name → full name for Slack message

    new_count = 0
    for display_name in watch_users:
        log.info("Checking activities for %s...", display_name)
        try:
            activities = session.get_user_activities(display_name)
        except Exception as e:
            log.warning("Could not fetch activities for %s: %s", display_name, e)
            continue

        # Resolve a human-readable name for Slack once per user
        if display_name not in name_cache:
            prof = session.lookup_profile(display_name)
            name_cache[display_name] = (
                prof.get("fullName") or prof.get("displayName") or display_name
            )
        full_name = name_cache[display_name]

        for activity in activities:
            aid = str(activity.get("activityId", ""))
            if not aid or aid in seen:
                continue
            # Inject name so post_run can display it
            activity.setdefault("ownerFullName", full_name)
            log.info("New activity %s from %s", aid, full_name)
            thread_ts = post_run(slack, channel, activity)
            seen.add(aid)
            if thread_ts:
                threads[aid] = thread_ts
            new_count += 1

    save_seen(seen, threads)
    if new_count == 0:
        log.info("No new runs.")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()

    if not (MCP_TOKEN_DIR / "oauth2_token.json").exists():
        log.info("No cached tokens — running initial login...")
        full_login(cfg["email"], cfg["password"])

    session = GarminSession(cfg["email"], cfg["password"])
    slack   = WebClient(token=cfg["slack_token"])

    try:
        check_and_post(session, slack, cfg["watch_users"], cfg["channel"])
    except GarminConnectAuthenticationError as e:
        log.error("Auth error: %s", e)
        sys.exit(1)
    except GarminConnectTooManyRequestsError:
        log.warning("Garmin rate limited — try again later.")
        sys.exit(1)
    except Exception as e:
        log.exception("Unexpected error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
