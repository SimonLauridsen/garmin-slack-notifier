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
🫀 HR Zones: ⚪ 0:11  🔵 0:23  🟢 4:14  🟠 34:24  🔴 39:00
⚡ Training Effect: 5.0 — Overreaching
🫁 VO2 Max: 46
🔗 Activity: Morning Run
```

The pace icon is dynamic: 👍 for pace ≤ 5:00/km, 👎 for pace above 5:00/km (uses custom Slack emoji `:thumbsup-kilse:` / `:thumbsdown-kilse:`).

HR zones use Garmin's colour scheme: ⚪ Z1 (rest) → 🔵 Z2 (easy) → 🟢 Z3 (aerobic) → 🟠 Z4 (threshold) → 🔴 Z5 (max).

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

## Monthly Roundup

On the last day of each month at 23:59, a summary is posted to Slack with aggregated stats per user and four category awards.

```
📊 *April 2026 — Monthly Running Roundup*

           │    AJ    │    BS    │    CB    
───────────┼──────────┼──────────┼──────────
Distance   │ 82.3 km  │ 54.1 km  │ 31.7 km  
Total Time │  7h 48m  │  5h 12m  │  3h 05m  
Avg Pace   │ 5:41 /km │ 5:46 /km │ 5:50 /km 
Top Zone   │Z4 Thresh.│Z3 Aerobic│Z4 Thresh.
Avg HR     │ 151 bpm  │ 148 bpm  │ 155 bpm  
VO2 Max    │    54    │    51    │    48    
# Runs     │    6     │    5     │    4     

🥇 *Distance King* — Alice Johnson  (82.3 km)
⚡ *Speed Demon* — Alice Johnson  (5:41 /km)
🔥 *Cardio Warrior* — Bob Smith  (3h 22m in 🟠+🔴)
💚 *Iron Heart* — Bob Smith  (avg 148 bpm)

🏆 *Overall Champion of April — Alice Johnson!*
```

Scheduled via a separate LaunchAgent (`com.yourname.garmin-monthly.plist`) that runs daily at 23:59 — the script exits silently on any day that isn't the last of the month.

## Files

| File | Description |
|------|-------------|
| `garmin_login.py` | One-time Garmin authentication — run this first |
| `garmin_slack_poster.py` | Polls for new runs every 30 min and posts to Slack |
| `garmin_monthly_roundup.py` | Posts end-of-month stats table and awards |
| `garmin_sleep.py` | Utility script to display your latest sleep data |
| `seen_activities.json` | Tracks already-posted activity IDs (auto-managed) |
| `.env.example` | Environment variable template |
