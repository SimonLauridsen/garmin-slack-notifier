# Garmin → Slack Run Notifier

Automatically posts new runs from Garmin Connect to a Slack channel. Polls a configurable list of users every 30 minutes and notifies your team when someone completes a run.

Any public Garmin profile can be watched — no social connection or Garmin friendship required.

## Example Slack message

```
🏃 *John Doe just finished a run!*
📅 April 9, 2026
📏 Distance: 14.77 km
⏱️ Duration: 1:18:30
👎 Avg Pace: 5:18 /km
❤️ Avg HR: 157 bpm
🫀 HR Zones: Z1 · 0:11  Z2 · 0:23  Z3 █ 4:14  Z4 ███████ 34:24  Z5 ████████ 39:00
⚡ Training Effect: 5.0 — Overreaching
🫁 VO2 Max: 46
🔗 Activity: Morning Run
```

The pace icon is dynamic: 👍 for pace ≤ 5:00/km, 👎 for pace above 5:00/km (uses custom Slack emoji `:thumbsup-kilse:` / `:thumbsdown-kilse:`).

The HR zone bar scales proportionally — the widest bar always represents the dominant zone.

## Requirements

- Python 3.9+
- A Slack app with a Bot Token (`xoxb-...`) and `chat:write` permission
- Garmin Connect account

## Setup

### 1. Install dependencies

```bash
pip install requests slack-sdk python-dotenv garminconnect
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=yourpassword
GARMIN_WATCH_USERS=friend-display-name,another-friend-display-name,your-own-display-name
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_CHANNEL=#running
```

`GARMIN_WATCH_USERS` is a comma-separated list of Garmin display names (found in the profile URL: `connect.garmin.com/profile/<DisplayName>`). UUID-based display names also work.

### 3. Authenticate with Garmin

```bash
python3 garmin_login.py
```

Performs a one-time login using the Garmin web SSO flow and saves OAuth tokens to `~/.garmin-mcp/`. Tokens are refreshed automatically on subsequent runs — you only need to re-run this if authentication breaks.

### 4. Run

```bash
python3 garmin_slack_poster.py
```

The script checks all watched users for new activities, posts any it finds to Slack, and exits. Run it on a schedule (see below).

## Scheduling (macOS)

Create `~/Library/LaunchAgents/com.yourname.garmin-slack.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.yourname.garmin-slack</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>/path/to/garmin_slack_poster.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/garmin-slack-notifier</string>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>StandardOutPath</key>
    <string>/tmp/garmin_slack.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/garmin_slack.log</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.yourname.garmin-slack.plist
```

To reload after changes:

```bash
launchctl unload ~/Library/LaunchAgents/com.yourname.garmin-slack.plist
launchctl load   ~/Library/LaunchAgents/com.yourname.garmin-slack.plist
```

## How it works

1. Loads OAuth2 token from `~/.garmin-mcp/`; re-authenticates automatically on 401
2. For each user in `GARMIN_WATCH_USERS`, fetches their recent activities directly from the Garmin Connect API
3. Filters for running activities within the last 3 days
4. Posts any activity not already in `seen_activities.json` to Slack
5. Saves updated `seen_activities.json` (capped at 500 IDs) and exits

## Files

| File | Description |
|------|-------------|
| `garmin_login.py` | One-time Garmin authentication — run this first |
| `garmin_slack_poster.py` | Main script — fetches activities and posts to Slack |
| `garmin_sleep.py` | Utility script to display your latest sleep data |
| `seen_activities.json` | Tracks already-posted activity IDs (auto-managed) |
| `.env.example` | Environment variable template |
