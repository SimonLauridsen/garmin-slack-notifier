#!/usr/bin/env python3
"""
One-time Garmin login using the web SSO flow (same as the MCP).
Avoids the mobile API endpoint that is currently rate-limited.
Saves tokens to ~/.garmin-mcp/ for use by garmin_sleep.py.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
import warnings
from pathlib import Path

import requests
from requests.cookies import RequestsCookieJar

warnings.filterwarnings("ignore")

# ── endpoints ────────────────────────────────────────────────────────────────
GARMIN_API      = "https://connectapi.garmin.com"
SSO_EMBED       = "https://sso.garmin.com/sso/embed"
SSO_SIGNIN      = "https://sso.garmin.com/sso/signin"
SSO_ORIGIN      = "https://sso.garmin.com"
OAUTH_CONSUMER  = "https://thegarth.s3.amazonaws.com/oauth_consumer.json"
OAUTH_PREAUTH   = f"{GARMIN_API}/oauth-service/oauth/preauthorized"
OAUTH_EXCHANGE  = f"{GARMIN_API}/oauth-service/oauth/exchange/user/2.0"
PROFILE_URL     = f"{GARMIN_API}/userprofile-service/socialProfile"

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
UA_MOBILE  = "com.garmin.android.apps.connectmobile"

CSRF_RE   = re.compile(r'name="_csrf"\s+value="(.+?)"')
TICKET_RE = re.compile(r'ticket=([^"&\s]+)')

TOKEN_DIR = Path.home() / ".garmin-mcp"


# ── credentials ──────────────────────────────────────────────────────────────

def get_credentials() -> tuple[str, str]:
    import os
    email    = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")
    if email and password:
        return email, password

    cfg = Path.home() / ".claude.json"
    if cfg.exists():
        data = json.loads(cfg.read_text())
        for project in data.get("projects", {}).values():
            env = project.get("mcpServers", {}).get("garmin", {}).get("env", {})
            e, p = env.get("GARMIN_EMAIL", ""), env.get("GARMIN_PASSWORD", "")
            if e and p and e != "you@email.com":
                return e, p

    raise SystemExit(
        "No credentials found. Set GARMIN_EMAIL and GARMIN_PASSWORD env vars."
    )


# ── OAuth1 signing ────────────────────────────────────────────────────────────

def _oauth1_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token: str = "",
    token_secret: str = "",
    extra_params: dict = None,
) -> str:
    params = {
        "oauth_consumer_key":     consumer_key,
        "oauth_nonce":            secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp":        str(int(time.time())),
        "oauth_version":          "1.0",
    }
    if token:
        params["oauth_token"] = token
    if extra_params:
        params.update(extra_params)

    # Base string (only oauth_ params, no query string)
    sorted_params = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(params.items())
    )
    # Strip existing query string from URL for base string
    base_url = url.split("?")[0]
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
        f'{k}="{urllib.parse.quote(v, safe="")}"'
        for k, v in sorted(params.items())
    )


# ── login flow ────────────────────────────────────────────────────────────────

def fetch_consumer() -> dict:
    r = requests.get(OAUTH_CONSUMER, timeout=10)
    r.raise_for_status()
    return r.json()


def get_login_tickets(email: str, password: str) -> tuple[str, str | None]:
    """Web SSO flow — authenticates once and returns TWO service tickets in one go:
      - embed_ticket  for OAuth1/OAuth2 (connectapi.garmin.com)
      - portal_ticket for DI token      (diauth.garmin.com / social feed)

    Both tickets are requested before either is consumed, so the live TGT
    (Session cookie) is still valid when the second ticket is requested.
    Returns (embed_ticket, portal_ticket_or_None).
    """
    PORTAL_SERVICE = "https://connect.garmin.com/app"
    session = requests.Session()
    session.headers.update({"User-Agent": UA_BROWSER})

    # 1. Prime cookies
    session.get(
        SSO_EMBED,
        params={"clientId": "GarminConnect", "locale": "en", "service": SSO_EMBED},
    )

    # 2. Get CSRF token
    signin_params = {
        "id":          "gauth-widget",
        "embedWidget": "true",
        "locale":      "en",
        "gauthHost":   SSO_EMBED,
    }
    r = session.get(SSO_SIGNIN, params=signin_params)
    csrf_match = CSRF_RE.search(r.text)
    if not csrf_match:
        raise RuntimeError("Could not find CSRF token in SSO page")
    csrf = csrf_match.group(1)

    # 3. POST credentials → embed ticket
    post_params = {
        **signin_params,
        "clientId":                        "GarminConnect",
        "service":                         SSO_EMBED,
        "source":                          SSO_EMBED,
        "redirectAfterAccountLoginUrl":    SSO_EMBED,
        "redirectAfterAccountCreationUrl": SSO_EMBED,
    }
    r = session.post(
        SSO_SIGNIN,
        params=post_params,
        data={"username": email, "password": password, "embed": "true", "_csrf": csrf},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin":       SSO_ORIGIN,
            "Referer":      SSO_SIGNIN,
            "Dnt":          "1",
        },
    )

    ticket_match = TICKET_RE.search(r.text)
    if not ticket_match:
        if "MFA" in r.text or "verif" in r.text.lower() or "factor" in r.text.lower():
            raise RuntimeError("MFA required — not yet supported in this flow")
        raise RuntimeError(
            "Login failed — ticket not found. Check your credentials.\n"
            f"Response snippet: {r.text[:500]}"
        )
    embed_ticket = ticket_match.group(1)

    # 4. While TGT is still live, request a second ticket for the portal service.
    #    CAS issues the ticket as a 302 redirect — do NOT follow it.
    portal_ticket: str | None = None
    try:
        r2 = session.get(
            SSO_SIGNIN,
            params={"service": PORTAL_SERVICE},
            headers={"User-Agent": UA_BROWSER},
            allow_redirects=False,
        )
        location = r2.headers.get("Location", "")
        for text in (location, r2.text):
            m = TICKET_RE.search(text)
            if m:
                portal_ticket = m.group(1)
                break
    except Exception:
        pass

    return embed_ticket, portal_ticket



def get_di_token(portal_ticket: str) -> dict | None:
    """Exchange a portal-service SSO ticket for a DI (Device Integration)
    OAuth2 token via diauth.garmin.com.  The DI token is needed to access
    the social connections activity feed endpoint on connectapi.garmin.com.
    """
    PORTAL_SERVICE = "https://connect.garmin.com/app"
    DI_TOKEN_URL   = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
    DI_GRANT_TYPE  = "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
    DI_CLIENT_IDS  = [
        "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
        "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
        "GARMIN_CONNECT_MOBILE_ANDROID_DI",
    ]

    for client_id in DI_CLIENT_IDS:
        basic = "Basic " + base64.b64encode(f"{client_id}:".encode()).decode()
        try:
            r = requests.post(
                DI_TOKEN_URL,
                headers={
                    "Authorization":  basic,
                    "User-Agent":     UA_MOBILE,
                    "Content-Type":   "application/x-www-form-urlencoded",
                    "Accept":         "application/json",
                },
                data={
                    "client_id":      client_id,
                    "service_ticket": portal_ticket,
                    "grant_type":     DI_GRANT_TYPE,
                    "service_url":    PORTAL_SERVICE,
                },
                timeout=15,
            )
            if r.ok:
                data = r.json()
                data["expires_at"] = int(time.time()) + data.get("expires_in", 3600)
                return data
        except Exception:
            continue
    return None


def exchange_ticket_for_oauth1(ticket: str, consumer: dict) -> dict:
    ck, cs = consumer["consumer_key"], consumer["consumer_secret"]
    query_params = {
        "ticket":              ticket,
        "login-url":          SSO_EMBED,
        "accepts-mfa-tokens": "true",
    }
    url = f"{OAUTH_PREAUTH}?" + urllib.parse.urlencode(query_params)
    auth_header = _oauth1_header("GET", OAUTH_PREAUTH, ck, cs, extra_params=query_params)
    r = requests.get(url, headers={"Authorization": auth_header, "User-Agent": UA_MOBILE}, timeout=15)
    r.raise_for_status()
    params = urllib.parse.parse_qs(r.text)
    token        = params.get("oauth_token", [None])[0]
    token_secret = params.get("oauth_token_secret", [None])[0]
    if not token or not token_secret:
        raise RuntimeError(f"OAuth1 exchange failed: {r.text}")
    return {"oauth_token": token, "oauth_token_secret": token_secret}


def exchange_oauth1_for_oauth2(oauth1: dict, consumer: dict) -> dict:
    ck, cs = consumer["consumer_key"], consumer["consumer_secret"]
    auth_header = _oauth1_header(
        "POST", OAUTH_EXCHANGE, ck, cs,
        token=oauth1["oauth_token"],
        token_secret=oauth1["oauth_token_secret"],
    )
    # Build query string with all oauth params from header
    oauth_params = {}
    for part in auth_header.removeprefix("OAuth ").split(", "):
        k, v = part.split('="', 1)
        oauth_params[k] = urllib.parse.unquote(v.rstrip('"'))

    r = requests.post(
        OAUTH_EXCHANGE,
        params=oauth_params,
        headers={"User-Agent": UA_MOBILE, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    data["expires_at"] = int(time.time()) + data.get("expires_in", 3600)
    return data


def fetch_profile(oauth2: dict) -> dict:
    r = requests.get(
        PROFILE_URL,
        headers={"Authorization": f"Bearer {oauth2['access_token']}", "User-Agent": UA_MOBILE},
        timeout=10,
    )
    r.raise_for_status()
    d = r.json()
    return {
        "displayName": d.get("displayName", ""),
        "profileId":   d.get("profileId") or d.get("userProfileNumber"),
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    email, password = get_credentials()
    print("Fetching OAuth consumer...", flush=True)
    consumer = fetch_consumer()

    print("Logging in via web SSO...", flush=True)
    embed_ticket, portal_ticket = get_login_tickets(email, password)

    print("Exchanging ticket for OAuth1 token...", flush=True)
    oauth1 = exchange_ticket_for_oauth1(embed_ticket, consumer)

    print("Exchanging OAuth1 for OAuth2 bearer...", flush=True)
    oauth2 = exchange_oauth1_for_oauth2(oauth1, consumer)

    print("Fetching profile...", flush=True)
    profile = fetch_profile(oauth2)

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    (TOKEN_DIR / "oauth1_token.json").write_text(json.dumps(oauth1, indent=2))
    (TOKEN_DIR / "oauth2_token.json").write_text(json.dumps(oauth2, indent=2))
    (TOKEN_DIR / "profile.json").write_text(json.dumps(profile, indent=2))

    # Exchange the portal ticket for a DI token (unlocks friends' activity feed).
    if portal_ticket:
        print("Exchanging portal ticket for DI token...", flush=True)
        di_data = get_di_token(portal_ticket)
        if di_data:
            (TOKEN_DIR / "di_token.json").write_text(json.dumps(di_data, indent=2))
            print("DI token obtained — friends' activities will be available.")
        else:
            print("DI token exchange failed (non-critical).")
    else:
        print("Portal ticket not issued — friends' activities unavailable until next login.")

    print(f"\nAuthenticated as {profile['displayName']}")
    print(f"Tokens saved to {TOKEN_DIR}")
    print("Run:  python3 garmin_sleep.py  or  python3 garmin_slack_poster.py")


if __name__ == "__main__":
    main()
