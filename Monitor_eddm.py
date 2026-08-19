#!/usr/bin/env python3
"""Weather monitor for Munich Airport (ICAO: EDDM).

Fetches current METAR and TAF reports from the NOAA Aviation Weather
Center API and prints a human-readable summary. Can run once or loop
at a fixed interval to keep monitoring conditions.
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen
import json

ICAO = "EDDM"
METAR_URL = f"https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json"
TAF_URL = f"https://aviationweather.gov/api/data/taf?ids={ICAO}&format=json"
REQUEST_TIMEOUT = 10


def fetch_json(url: str):
    with urlopen(url, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def format_metar(entry: dict) -> str:
    lines = [
        f"METAR {entry.get('icaoId', ICAO)} @ {entry.get('reportTime', 'N/A')} UTC",
        f"  Raw     : {entry.get('rawOb', 'N/A')}",
        f"  Temp    : {entry.get('temp', 'N/A')} C   Dewpoint: {entry.get('dewp', 'N/A')} C",
        f"  Wind    : {entry.get('wdir', 'N/A')} deg @ {entry.get('wspd', 'N/A')} kt"
        + (f" gust {entry.get('wgst')}" if entry.get("wgst") else ""),
        f"  Visib   : {entry.get('visib', 'N/A')} SM",
        f"  Altimeter: {entry.get('altim', 'N/A')} hPa",
    ]
    clouds = entry.get("clouds") or []
    if clouds:
        cloud_str = ", ".join(
            f"{c.get('cover', '?')} {c.get('base', '?')}ft" for c in clouds
        )
        lines.append(f"  Clouds  : {cloud_str}")
    return "\n".join(lines)


def format_taf(entry: dict) -> str:
    lines = [
        f"TAF {entry.get('icaoId', ICAO)} issued {entry.get('issueTime', 'N/A')} UTC",
        f"  Raw     : {entry.get('rawTAF', 'N/A')}",
    ]
    for fcst in entry.get("fcsts", []):
        time_from = fcst.get("timeFrom", "?")
        time_to = fcst.get("timeTo", "?")
        wind = f"{fcst.get('wdir', '?')} deg @ {fcst.get('wspd', '?')} kt"
        visib = fcst.get("visib", "?")
        lines.append(f"  {time_from} -> {time_to} | wind {wind} | visib {visib} SM")
    return "\n".join(lines)


def get_report() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"=== Munich Airport ({ICAO}) Weather Report — {now} UTC ==="]

    try:
        metar_data = fetch_json(METAR_URL)
        if metar_data:
            parts.append(format_metar(metar_data[0]))
        else:
            parts.append("METAR: no data available")
    except (URLError, ValueError, TimeoutError) as exc:
        parts.append(f"METAR: failed to fetch ({exc})")

    parts.append("")

    try:
        taf_data = fetch_json(TAF_URL)
        if taf_data:
            parts.append(format_taf(taf_data[0]))
        else:
            parts.append("TAF: no data available")
    except (URLError, ValueError, TimeoutError) as exc:
        parts.append(f"TAF: failed to fetch ({exc})")

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Monitor Munich Airport (EDDM) weather via METAR/TAF."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Polling interval in seconds. If omitted, runs once and exits.",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        print(get_report())
        return

    try:
        while True:
            print(get_report())
            print()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
