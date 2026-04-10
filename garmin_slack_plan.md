# Garmin → Slack Run Notifier

## Goal
A script that watches specific Garmin Connect users' activities and posts a Slack message whenever they complete a new run. Runs once per invocation — scheduled via macOS LaunchAgent every 6 hours.

---

## Prerequisites
- Python 3.9+
- A Slack app with a Bot Token (`xoxb-...`) and `chat:write` permission
- Garmin Connect credentials (your own account)
- Display names of the users to watch (their own profiles must be public)

---

## Dependencies

```bash
pip install requests slack-sdk python-dotenv garminconnect
```

---

## Configuration: `.env`

```
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=yourpassword
GARMIN_WATCH_USERS=christofferclausen,a6de89f1-b8ce-4901-9825-1d09bdce12e9,your-own-display-name
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_CHANNEL=#running
```

`GARMIN_WATCH_USERS` is a comma-separated list of Garmin display names (or UUIDs). Any public Garmin profile works — no social connection required.

---

## Auth Strategy

Authentication uses a custom web SSO flow (`sso.garmin.com/sso/signin`) — not the garminconnect library's login, which hits a rate-limited mobile endpoint.

Tokens are stored in `~/.garmin-mcp/`:
- `oauth1_token.json` — used to refresh OAuth2
- `oauth2_token.json` — used for all Garmin API calls
- `profile.json` — authenticated user's display name

Run `python3 garmin_login.py` once to authenticate. The script auto-refreshes tokens on 401.

---

## How `garmin_slack_poster.py` Works

1. **Load config** from `.env`
2. **Load OAuth2 token** from `~/.garmin-mcp/`; re-authenticate if missing or expired
3. **For each user in `GARMIN_WATCH_USERS`**:
   - Fetch their recent activities from `connectapi.garmin.com/activitylist-service/activities/{displayName}`
   - Filter for `activityType = running` within the last 3 days
   - Look up their full name via the social profile endpoint
4. **For each activity not in `seen_activities.json`**:
   - Post a formatted Slack message
   - Add the activity ID to `seen_activities.json`
5. **Save `seen_activities.json`** (capped at 500 IDs)
6. **Exit** — scheduling is handled externally

---

## Slack Message Format

```
🏃 *Christoffer Clausen just finished a run!*
📅 April 9, 2026
📏 Distance: 14.77 km
⏱️ Duration: 1:18:30
💨 Avg Pace: 5:19 /km
🔗 Activity: Aarhus Løb
```

All values use the metric system (km, min:ss /km).

---

## Scheduling: macOS LaunchAgent

`~/Library/LaunchAgents/com.simonlauridsen.garmin-slack.plist`

- Runs `garmin_slack_poster.py` every 6 hours (`StartInterval: 21600`)
- `RunAtLoad: true` — also fires on login
- Logs to `/tmp/garmin_slack.log`

To reload after changes:
```bash
launchctl unload ~/Library/LaunchAgents/com.simonlauridsen.garmin-slack.plist
launchctl load   ~/Library/LaunchAgents/com.simonlauridsen.garmin-slack.plist
```

---

## Error Handling
- 401 → auto-refresh OAuth2 via OAuth1; falls back to full re-login
- 429 → logs rate limit warning and exits cleanly
- Per-user fetch errors are logged and skipped (other users still checked)
- Slack API errors are logged without crashing
- `seen_activities.json` capped at 500 IDs
