#!/usr/bin/env python3
"""
Monthly running roundup — posts a stats table to Slack on the last day of each month.
Designed to run daily at 23:59 via LaunchAgent; exits silently on any other day.
"""

from __future__ import annotations

import calendar
import os
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from garmin_slack_poster import (
    GarminSession,
    load_config,
    _fmt_pace_km,
    MCP_TOKEN_DIR,
    UA_MOBILE,
    GARMIN_API,
)

ZONE_LABEL = {1: "Z1 Rest", 2: "Z2 Easy", 3: "Z3 Aerobic", 4: "Z4 Thresh.", 5: "Z5 Max"}
ZONE_EMOJI = {1: "⚪", 2: "🔵", 3: "🟢", 4: "🟠", 5: "🔴"}


# ── helpers ───────────────────────────────────────────────────────────────────

def is_last_day_of_month() -> bool:
    today = date.today()
    return today.day == calendar.monthrange(today.year, today.month)[1]


def initials(full_name: str) -> str:
    return "".join(w[0].upper() for w in full_name.split())


def fmt_hm(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ── data fetching ─────────────────────────────────────────────────────────────

def fetch_month_activities(display_name: str, token: str, year: int, month: int) -> list[dict]:
    """Fetch all running activities for the given month (paginates as needed)."""
    month_start = f"{year}-{month:02d}-01"
    last_day    = calendar.monthrange(year, month)[1]
    month_end   = f"{year}-{month:02d}-{last_day:02d}"
    h = {"Authorization": f"Bearer {token}", "User-Agent": UA_MOBILE, "Accept": "application/json"}
    results, start = [], 0
    while True:
        r = requests.get(
            f"{GARMIN_API}/activitylist-service/activities/{display_name}",
            headers=h, params={"start": start, "limit": 100}, timeout=15,
        )
        r.raise_for_status()
        acts = r.json().get("activityList", [])
        if not acts:
            break
        for a in acts:
            act_date = (a.get("startTimeLocal") or "")[:10]
            if act_date < month_start:
                return results  # gone past the month
            if act_date <= month_end:
                type_key = str(a.get("activityType", {}).get("typeKey", ""))
                if "running" in type_key.lower():
                    results.append(a)
        if len(acts) < 100:
            break
        start += 100
    return results


# ── stats aggregation ─────────────────────────────────────────────────────────

def compute_stats(activities: list[dict]) -> dict | None:
    if not activities:
        return None
    total_dist = sum(a.get("distance") or 0 for a in activities)
    total_dur  = sum(a.get("duration") or 0 for a in activities)
    avg_speed  = total_dist / total_dur if total_dur else 0

    hr_values = [a["averageHR"] for a in activities if a.get("averageHR")]
    avg_hr    = sum(hr_values) / len(hr_values) if hr_values else None

    zone_totals = {i: sum(a.get(f"hrTimeInZone_{i}") or 0 for a in activities) for i in range(1, 6)}
    top_zone    = max(zone_totals, key=lambda z: zone_totals[z])

    vo2max = next((a["vO2MaxValue"] for a in activities if a.get("vO2MaxValue")), None)

    return {
        "count":        len(activities),
        "total_dist":   total_dist,
        "total_dur":    total_dur,
        "avg_speed":    avg_speed,
        "avg_hr":       avg_hr,
        "top_zone":     top_zone,
        "zone_totals":  zone_totals,
        "vo2max":       vo2max,
    }


# ── table builder ─────────────────────────────────────────────────────────────

def build_table(user_stats: list[tuple[str, dict | None]]) -> str:
    cols   = [initials(name) for name, _ in user_stats]
    col_w  = max(10, max(len(c) for c in cols) + 2)
    labels = ["Distance", "Total Time", "Avg Pace", "Top Zone", "Avg HR", "VO2 Max", "# Runs"]
    lbl_w  = max(len(l) for l in labels) + 1  # +1 for a single space of right-padding

    def c(val: str) -> str:
        return val.center(col_w)

    def row(label: str, values: list[str]) -> str:
        return f"{label:<{lbl_w}}│" + "│".join(c(v) for v in values)

    divider = "─" * lbl_w + "┼" + "┼".join("─" * col_w for _ in cols)

    def pace_str(s: dict | None) -> str:
        if not s or not s.get("avg_speed"):
            return "—"
        p, _ = _fmt_pace_km(s["avg_speed"])
        return p

    lines = [
        " " * lbl_w + "│" + "│".join(c(i) for i in cols),
        divider,
        row("Distance",   [f"{s['total_dist']/1000:.1f} km" if s else "—" for _, s in user_stats]),
        row("Total Time", [fmt_hm(s["total_dur"])              if s else "—" for _, s in user_stats]),
        row("Avg Pace",   [pace_str(s)                                        for _, s in user_stats]),
        row("Top Zone",   [ZONE_LABEL.get(s["top_zone"], "—") if s else "—"  for _, s in user_stats]),
        row("Avg HR",     [f"{int(s['avg_hr'])} bpm"  if s and s.get("avg_hr") else "—" for _, s in user_stats]),
        row("VO2 Max",    [str(int(s["vo2max"]))       if s and s.get("vo2max")  else "—" for _, s in user_stats]),
        row("# Runs",     [str(s["count"])             if s else "0"              for _, s in user_stats]),
    ]
    return "\n".join(lines)


# ── awards ────────────────────────────────────────────────────────────────────

def build_awards(user_stats: list[tuple[str, dict | None]]) -> str:
    valid = [(n, s) for n, s in user_stats if s]
    if not valid:
        return ""

    scores: dict[str, int] = {n: 0 for n, _ in valid}
    n = len(valid)
    awards: list[str] = []

    def rank(key_fn, reverse=True) -> list[tuple[str, dict]]:
        return sorted(valid, key=lambda x: key_fn(x[1]), reverse=reverse)

    def award_points(ranked_list: list[tuple[str, dict]]) -> None:
        for i, (name, _) in enumerate(ranked_list):
            scores[name] += n - i

    # Distance King
    by_dist = rank(lambda s: s.get("total_dist", 0))
    w, ws   = by_dist[0]
    awards.append(f"🥇 *Distance King* — {w}  ({ws['total_dist']/1000:.1f} km)")
    award_points(by_dist)

    # Speed Demon (lower secs/km = better)
    def pace_secs(s: dict) -> float:
        spd = s.get("avg_speed", 0)
        return 1000 / spd if spd else float("inf")
    by_pace = rank(pace_secs, reverse=False)
    w, ws   = by_pace[0]
    p, _    = _fmt_pace_km(ws.get("avg_speed"))
    awards.append(f"⚡ *Speed Demon* — {w}  ({p})")
    award_points(by_pace[::-1])  # reverse so fastest gets most points

    # Cardio Warrior (most time in Z4+Z5)
    def z45(s: dict) -> float:
        return s.get("zone_totals", {}).get(4, 0) + s.get("zone_totals", {}).get(5, 0)
    by_z45 = rank(z45)
    w, ws   = by_z45[0]
    awards.append(f"🔥 *Cardio Warrior* — {w}  ({fmt_hm(z45(ws))} in {ZONE_EMOJI[4]}+{ZONE_EMOJI[5]})")
    award_points(by_z45)

    # Iron Heart (lowest avg HR among those with HR data)
    with_hr = [(name, s) for name, s in valid if s.get("avg_hr")]
    if with_hr:
        by_hr = sorted(with_hr, key=lambda x: x[1]["avg_hr"])
        w, ws = by_hr[0]
        awards.append(f"💚 *Iron Heart* — {w}  (avg {int(ws['avg_hr'])} bpm)")
        award_points(by_hr[::-1])

    # Overall champion
    champ = max(scores, key=lambda k: scores[k])
    awards.append(f"\n🏆 *Overall Champion of {date.today().strftime('%B')} — {champ}!*")

    return "\n".join(awards)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not is_last_day_of_month():
        return

    load_dotenv(dotenv_path=BASE_DIR / ".env")
    cfg     = load_config()
    session = GarminSession(cfg["email"], cfg["password"])
    slack   = WebClient(token=cfg["slack_token"])

    session._ensure_oauth2()
    token = session._oauth2["access_token"]

    today      = date.today()
    year, month = today.year, today.month
    month_name  = today.strftime("%B %Y")

    user_stats: list[tuple[str, dict | None]] = []
    for display_name in cfg["watch_users"]:
        prof      = session.lookup_profile(display_name)
        full_name = prof.get("fullName") or prof.get("displayName") or display_name
        acts      = fetch_month_activities(display_name, token, year, month)
        user_stats.append((full_name, compute_stats(acts)))

    table  = build_table(user_stats)
    awards = build_awards(user_stats)

    text = (
        f"📊 *{month_name} — Monthly Running Roundup*\n\n"
        f"```\n{table}\n```\n\n"
        f"{awards}"
    )

    try:
        slack.chat_postMessage(channel=cfg["channel"], text=text, unfurl_links=False)
    except SlackApiError as e:
        print(f"Slack error: {e.response['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
