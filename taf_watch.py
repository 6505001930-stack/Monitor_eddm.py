#!/usr/bin/env python3
"""Quick TAF viewer for Tokyo Haneda (RJTT).

TAF is issued four times a day (00/06/12/18Z) and lands in the API a few
minutes past the hour, so the useful question when checking is usually
"is this a new one, and what changed?" rather than "what does it say?".
This prints the raw TAF, decodes the forecast periods into JST, and
remembers the last issue time seen so a repeat run says NEW or UNCHANGED.

Times are shown in JST because that is the clock the airport is on;
the raw TAF stays in UTC as issued.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen

from Monitor_rjtt import (
    ICAO,
    JST,
    JMA_WIND_COMPASS,
    REQUEST_TIMEOUT,
    TAF_URL,
    format_stamp,
    parse_utc,
)

DEFAULT_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".taf_last_seen.json")

# Significant weather worth calling out rather than leaving in the raw string.
WX_NOTES = {
    "TS": "พายุฝนฟ้าคะนอง",
    "TSRA": "พายุฝนฟ้าคะนอง + ฝน",
    "SHRA": "ฝนซู่",
    "-SHRA": "ฝนซู่เบา",
    "RA": "ฝน",
    "-RA": "ฝนเบา",
    "BR": "หมอกน้ำค้าง",
    "FG": "หมอก",
}


def degrees_to_compass(degrees) -> str:
    """16-point compass label, matching how the AMeDAS wind is reported."""
    if degrees is None:
        return "?"
    # JMA_WIND_COMPASS[0] is "Calm"; the 16 points start at index 1 = N.
    index = int((degrees % 360) / 22.5 + 0.5) % 16
    return JMA_WIND_COMPASS[index + 1]


def fetch_taf():
    with urlopen(TAF_URL, timeout=REQUEST_TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data:
        raise ValueError("no TAF returned")
    return data[0]


def to_jst(moment):
    return moment.astimezone(JST) if moment else None


def describe_weather(raw) -> str:
    if not raw:
        return ""
    parts = [WX_NOTES.get(token, token) for token in str(raw).split()]
    return " + ".join(parts)


def format_period(fcst: dict) -> str:
    start = to_jst(parse_utc(fcst.get("timeFrom")))
    end = to_jst(parse_utc(fcst.get("timeTo")))
    span = "{} - {}".format(
        start.strftime("%d %H:%M") if start else "?",
        end.strftime("%d %H:%M") if end else "?",
    )

    change = fcst.get("fcstChange") or "BASE"
    bits = []

    wdir, wspd = fcst.get("wdir"), fcst.get("wspd")
    if wdir is not None and wspd is not None:
        gust = f" gust {fcst['wgst']}kt" if fcst.get("wgst") else ""
        bits.append(f"ลม {degrees_to_compass(wdir)} ({wdir}°) @ {wspd}kt{gust}")

    visib = fcst.get("visib")
    if visib not in (None, "", "6+"):
        bits.append(f"ทัศนวิสัย {visib} SM")
    elif visib == "6+":
        bits.append("ทัศนวิสัย ดี")

    weather = describe_weather(fcst.get("wxString"))
    if weather:
        bits.append(f"** {weather} **")

    clouds = fcst.get("clouds") or []
    if clouds:
        cloud_str = ", ".join(
            f"{c.get('cover', '?')}{'(Cb)' if c.get('type') == 'CB' else ''} {c.get('base', '?')}ft"
            for c in clouds
        )
        bits.append(f"เมฆ {cloud_str}")

    detail = " | ".join(bits) if bits else "(ไม่ระบุ)"
    return f"  [{change:6s}] {span} JST  {detail}"


def read_state(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(path: str, issue_time: str, raw_taf: str):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"issueTime": issue_time, "rawTAF": raw_taf}, f)


def build_report(entry: dict, state: dict) -> tuple[str, bool]:
    issue_time = entry.get("issueTime")
    raw_taf = entry.get("rawTAF", "N/A")
    issued = parse_utc(issue_time)
    is_new = state.get("issueTime") != issue_time

    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    status = "*** ฉบับใหม่ ***" if is_new else "(ฉบับเดิม ยังไม่มีอัปเดต)"

    lines = [
        f"=== TAF {entry.get('icaoId', ICAO)} — เช็คเมื่อ {now_jst} JST ===",
        f"ออกเมื่อ : {format_stamp(issued)}  {status}",
    ]
    if issued:
        lines.append(f"           ({to_jst(issued).strftime('%d %H:%M')} JST)")
    lines.append("")
    lines.append(f"RAW: {raw_taf}")
    lines.append("")
    lines.append("ช่วงพยากรณ์ (เวลา JST):")

    for fcst in entry.get("fcsts", []):
        lines.append(format_period(fcst))

    if is_new and state.get("rawTAF"):
        lines.append("")
        lines.append("ฉบับก่อนหน้า:")
        lines.append(f"  {state['rawTAF']}")

    return "\n".join(lines), is_new


def main():
    parser = argparse.ArgumentParser(
        description="Fetch and decode the current TAF for Tokyo Haneda (RJTT)."
    )
    parser.add_argument(
        "--state",
        default=DEFAULT_STATE_PATH,
        metavar="JSON",
        help="Where to remember the last TAF seen, so repeat runs can flag a new one.",
    )
    parser.add_argument(
        "--quiet-if-same",
        action="store_true",
        help="Print nothing and exit 1 when the TAF has not changed since the last run.",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not update the saved state (useful for a one-off look).",
    )
    args = parser.parse_args()

    try:
        entry = fetch_taf()
    except (URLError, ValueError, TimeoutError, OSError) as exc:
        print(f"TAF: failed to fetch ({exc})", file=sys.stderr)
        return 2

    state = read_state(args.state)
    report, is_new = build_report(entry, state)

    if not is_new and args.quiet_if_same:
        return 1

    print(report)

    if is_new and not args.no_save:
        write_state(args.state, entry.get("issueTime"), entry.get("rawTAF", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
