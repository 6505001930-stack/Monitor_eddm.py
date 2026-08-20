#!/usr/bin/env python3
"""Weather monitor for Tokyo Haneda Airport (ICAO: RJTT).

The headline temperature comes from METAR, which is the freshest actual
measurement available for the airport: it is issued every 30 minutes and
reaches the API within a few minutes. JMA's AMeDAS network publishes a
real sensor reading every 10 minutes from a station co-located with the
airport (No. 44166, Haneda), reaching the public feed within a couple of
minutes of the observation, so it is reported as supporting detail
alongside METAR. Every reading is printed with its age so a stale figure
is never mistaken for a live one.

Also fetches TAF (aerodrome forecast) and compares 2m temperature across
four forecast models via Open-Meteo.
"""

import argparse
import csv
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import json

ICAO = "RJTT"
METAR_URL = f"https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json"
TAF_URL = f"https://aviationweather.gov/api/data/taf?ids={ICAO}&format=json"
REQUEST_TIMEOUT = 10

JST = timezone(timedelta(hours=9))
# AMeDAS station 44166 "Haneda" sits on the airport itself, so no station
# lookup is needed (unlike the Munich monitor, which searches DWD's list).
JMA_STATION_ID = 44166
JMA_POINT_URL_TEMPLATE = (
    "https://www.jma.go.jp/bosai/amedas/data/point/{station_id}/{date}_{chunk:02d}.json"
)
# JMA quality flag: 0 means a normal, confirmed value.
JMA_OK_FLAG = 0

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HANEDA_LAT = 35.553  # RJTT
HANEDA_LON = 139.781
OPEN_METEO_MODELS = ["jma_seamless", "gfs_seamless", "ecmwf_ifs025", "ukmo_seamless"]


def fetch_json(url: str):
    with urlopen(url, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_utc(raw) -> datetime | None:
    """Parse the assorted timestamp shapes these APIs return, as UTC."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc)

    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit() and len(text) == 14:  # JMA AMeDAS key, e.g. 20260821032000 (JST)
        naive = datetime.strptime(text, "%Y%m%d%H%M%S")
        return naive.replace(tzinfo=JST).astimezone(timezone.utc)

    text = text.replace("Z", "+00:00")
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_stamp(moment: datetime | None) -> str:
    """Render a timestamp together with how old it is."""
    if moment is None:
        return "time unknown"
    age = (datetime.now(timezone.utc) - moment).total_seconds() / 60
    when = moment.strftime("%Y-%m-%d %H:%M UTC")
    if age < 0:
        return f"{when} (in the future)"
    return f"{when} ({int(round(age))} min old)"


def jma_chunks_needed(hours: int):
    """3-hour (date, chunk_start_hour) buckets covering the last `hours`, in JST."""
    now_jst = datetime.now(JST)
    start = now_jst - timedelta(hours=hours)
    floor = start.replace(minute=0, second=0, microsecond=0)
    floor -= timedelta(hours=floor.hour % 3)

    chunks = []
    cur = floor
    while cur <= now_jst:
        chunks.append((cur.strftime("%Y%m%d"), cur.hour))
        cur += timedelta(hours=3)
    return chunks


def fetch_jma_rows(hours: int = 6):
    """Return every valid 10-minute AMeDAS reading at Haneda in the last `hours`."""
    rows = []
    for date_str, chunk_hh in jma_chunks_needed(hours):
        url = JMA_POINT_URL_TEMPLATE.format(
            station_id=JMA_STATION_ID, date=date_str, chunk=chunk_hh
        )
        try:
            data = fetch_json(url)
        except HTTPError as exc:
            if exc.code == 404:
                continue  # chunk not published yet (e.g. still in progress)
            raise
        for key, entry in data.items():
            temp = entry.get("temp")
            if not temp or temp[1] != JMA_OK_FLAG:
                continue
            rows.append({"time_jst": key, "temp": temp[0]})

    if not rows:
        raise ValueError("no valid AMeDAS temperature readings for Haneda")
    rows.sort(key=lambda r: r["time_jst"])
    return rows


def format_jma(row: dict) -> str:
    measured = parse_utc(row["time_jst"])
    lines = [
        f"JMA AMeDAS station {JMA_STATION_ID} (Haneda, official 10-min obs)",
        f"  Measured : {format_stamp(measured)}",
        f"  Air temp : {row['temp']} C   (2m height, raw)",
    ]
    return "\n".join(lines)


def fetch_metar_history(hours: int = 6):
    url = f"{METAR_URL}&hours={hours}"
    return fetch_json(url) or []


def build_pairs(metar_entries, jma_rows):
    """Pair METAR and AMeDAS readings that share the exact same observation time.

    RJTT METAR is issued at :00 and :30 UTC, and AMeDAS's 10-minute series
    has rows at :00/:10/.../:50 JST (= :00/:30 UTC lands exactly on those
    ticks once converted), so an exact timestamp match gives a
    like-for-like comparison instead of comparing readings taken minutes
    apart.
    """
    jma_by_time = {}
    for row in jma_rows:
        moment = parse_utc(row["time_jst"])
        if moment is None:
            continue
        jma_by_time[moment] = row["temp"]

    pairs = {}
    for entry in metar_entries:
        moment = parse_utc(entry.get("obsTime") or entry.get("reportTime"))
        temp = entry.get("temp")
        if moment is None or temp is None or moment not in jma_by_time:
            continue
        try:
            metar_temp = float(temp)
        except (TypeError, ValueError):
            continue
        jma_temp = jma_by_time[moment]
        previous = jma_by_time.get(moment - timedelta(minutes=10))
        trend = round(jma_temp - previous, 2) if previous is not None else None
        pairs[moment] = (metar_temp, jma_temp, round(metar_temp - jma_temp, 2), trend)
    return pairs


def update_pair_log(path: str, pairs: dict):
    """Merge new pairs into the CSV, keyed by observation time.

    Each run re-fetches the whole current 3-hour AMeDAS chunk, so pairs
    fill in idempotently across runs without creating duplicates.
    """
    fields = ["obs_time_utc", "metar_temp_c", "jma_temp_c", "diff_c", "jma_trend_c"]
    existing = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("obs_time_utc"):
                    existing[row["obs_time_utc"]] = row

    added = 0
    for moment, (metar_temp, jma_temp, diff, trend) in pairs.items():
        key = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        if key not in existing:
            added += 1
        existing[key] = {
            "obs_time_utc": key,
            "metar_temp_c": metar_temp,
            "jma_temp_c": jma_temp,
            "diff_c": diff,
            "jma_trend_c": "" if trend is None else trend,
        }

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, restval="")
        writer.writeheader()
        for key in sorted(existing):
            writer.writerow(existing[key])

    return added, existing


def format_pair_summary(path: str, added: int, existing: dict) -> str:
    diffs, rising, falling = [], [], []
    for row in existing.values():
        try:
            diff = float(row["diff_c"])
        except (KeyError, TypeError, ValueError):
            continue
        diffs.append(diff)
        try:
            trend = float(row.get("jma_trend_c") or "")
        except ValueError:
            continue
        if trend > 0.1:
            rising.append(diff)
        elif trend < -0.1:
            falling.append(diff)

    lines = [
        "METAR vs JMA AMeDAS same-time comparison",
        f"  Log      : {path} ({len(diffs)} pairs, {added} new this run)",
    ]
    if not diffs:
        lines.append("  No overlapping timestamps yet.")
        return "\n".join(lines)

    mean = statistics.fmean(diffs)
    lines.append(f"  Mean diff: {mean:+.2f} C (METAR minus JMA)")
    if len(diffs) > 1:
        lines.append(f"  Spread   : sd {statistics.stdev(diffs):.2f} C, "
                     f"range {min(diffs):+.1f} to {max(diffs):+.1f} C")
    lines.append(f"  Sign     : {sum(1 for d in diffs if d > 0)} positive, "
                 f"{sum(1 for d in diffs if d == 0)} zero, "
                 f"{sum(1 for d in diffs if d < 0)} negative")

    if rising:
        lines.append(f"  While warming ({len(rising)} pairs): mean {statistics.fmean(rising):+.2f} C")
    if falling:
        lines.append(f"  While cooling ({len(falling)} pairs): mean {statistics.fmean(falling):+.2f} C")

    if len(diffs) < 20:
        lines.append("  Too few pairs to conclude anything yet.")
    elif abs(mean) < 0.3:
        lines.append("  Mean near zero — consistent with one shared sensor location.")
    else:
        lines.append("  Offset holds across samples — points to a real difference "
                     "between the two sensors.")
    return "\n".join(lines)


def fetch_open_meteo_current():
    """Open-Meteo's blended nowcast: refreshes roughly every 15 minutes, but
    it is model output smoothed against observations, not a sensor reading.
    """
    url = (
        f"{OPEN_METEO_URL}?latitude={HANEDA_LAT}&longitude={HANEDA_LON}"
        f"&current=temperature_2m&timezone=UTC"
    )
    data = fetch_json(url)
    current = data.get("current")
    if not current or current.get("temperature_2m") is None:
        raise ValueError("no current temperature in Open-Meteo response")
    return current["time"], current["temperature_2m"]


def format_open_meteo_current(time_label: str, temp) -> str:
    moment = parse_utc(time_label)
    lines = [
        "Open-Meteo nowcast — 2m temperature (fastest update, least accurate)",
        f"  Blended  : {format_stamp(moment)}",
        f"  Temp     : {temp} C   (model blend, not a sensor)",
    ]
    return "\n".join(lines)


def fetch_model_forecasts():
    models_param = ",".join(OPEN_METEO_MODELS)
    url = (
        f"{OPEN_METEO_URL}?latitude={HANEDA_LAT}&longitude={HANEDA_LON}"
        f"&hourly=temperature_2m&models={models_param}"
        f"&timezone=UTC&forecast_days=2"
    )
    data = fetch_json(url)
    hourly = data.get("hourly")
    if not hourly or not hourly.get("time"):
        raise ValueError("no hourly data in Open-Meteo response")

    times = hourly["time"]
    now_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
    idx = times.index(now_hour) if now_hour in times else 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results = []
    for model in OPEN_METEO_MODELS:
        series = hourly.get(f"temperature_2m_{model}")
        if not series:
            continue
        current = series[idx] if idx < len(series) else None
        today_values = [v for t, v in zip(times, series) if t.startswith(today) and v is not None]
        today_max = max(today_values) if today_values else None
        results.append((model, current, today_max))

    if not results:
        raise ValueError("none of the requested models returned data")
    return times[idx], results


def format_model_forecasts(hour_label: str, results) -> str:
    lines = [
        "Open-Meteo model comparison — 2m temperature (Tokyo Haneda)",
        f"  Forecast values for hour {hour_label} UTC (model output, not a measurement)",
    ]
    for model, current, today_max in results:
        cur_str = f"{current} C" if current is not None else "N/A"
        max_str = f"{today_max} C" if today_max is not None else "N/A"
        lines.append(f"  {model:<16}: this hour {cur_str:>7}   today max {max_str:>7}")
    return "\n".join(lines)


def format_metar(entry: dict) -> str:
    observed = parse_utc(entry.get("obsTime") or entry.get("reportTime"))
    lines = [
        f"METAR {entry.get('icaoId', ICAO)} — observed {format_stamp(observed)}",
        f"  Raw      : {entry.get('rawOb', 'N/A')}",
        f"  Temp     : {entry.get('temp', 'N/A')} C   Dewpoint: {entry.get('dewp', 'N/A')} C",
        f"  Wind     : {entry.get('wdir', 'N/A')} deg @ {entry.get('wspd', 'N/A')} kt"
        + (f" gust {entry.get('wgst')}" if entry.get("wgst") else ""),
        f"  Visib    : {entry.get('visib', 'N/A')} SM",
        f"  Altimeter: {entry.get('altim', 'N/A')} hPa",
    ]
    clouds = entry.get("clouds") or []
    if clouds:
        cloud_str = ", ".join(
            f"{c.get('cover', '?')} {c.get('base', '?')}ft" for c in clouds
        )
        lines.append(f"  Clouds   : {cloud_str}")
    return "\n".join(lines)


def format_taf(entry: dict) -> str:
    issued = parse_utc(entry.get("issueTime"))
    lines = [
        f"TAF {entry.get('icaoId', ICAO)} — issued {format_stamp(issued)}",
        f"  Raw      : {entry.get('rawTAF', 'N/A')}",
    ]
    for fcst in entry.get("fcsts", []):
        time_from = parse_utc(fcst.get("timeFrom"))
        time_to = parse_utc(fcst.get("timeTo"))
        span = "{} -> {}".format(
            time_from.strftime("%d %H:%MZ") if time_from else "?",
            time_to.strftime("%d %H:%MZ") if time_to else "?",
        )
        wdir, wspd = fcst.get("wdir"), fcst.get("wspd")
        wind = f"{wdir} deg @ {wspd} kt" if wdir is not None and wspd is not None else "n/a"
        visib = fcst.get("visib") or "n/a"
        lines.append(f"  {span} | wind {wind} | visib {visib} SM")
    return "\n".join(lines)


def headline(metar_entry, jma_row) -> str:
    """Lead with the freshest real measurement available."""
    if metar_entry:
        observed = parse_utc(metar_entry.get("obsTime") or metar_entry.get("reportTime"))
        return (
            f"CURRENT TEMPERATURE : {metar_entry.get('temp', 'N/A')} C\n"
            f"  source            : METAR {ICAO} (airport sensor)\n"
            f"  observed          : {format_stamp(observed)}"
        )
    if jma_row:
        measured = parse_utc(jma_row["time_jst"])
        return (
            f"CURRENT TEMPERATURE : {jma_row['temp']} C\n"
            f"  source            : JMA AMeDAS {JMA_STATION_ID} (METAR unavailable — fallback)\n"
            f"  measured          : {format_stamp(measured)}"
        )
    return "CURRENT TEMPERATURE : unavailable (no measurement source reachable)"


def get_report(pair_log: str | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    metar_entry = None
    metar_history = []
    metar_error = None
    try:
        metar_history = fetch_metar_history()
        if metar_history:
            metar_entry = max(
                metar_history,
                key=lambda e: parse_utc(e.get("obsTime") or e.get("reportTime"))
                or datetime.min.replace(tzinfo=timezone.utc),
            )
        else:
            metar_error = "no data available"
    except (URLError, ValueError, TimeoutError, OSError) as exc:
        metar_error = f"failed to fetch ({exc})"

    jma_row = None
    jma_rows = []
    jma_error = None
    try:
        jma_rows = fetch_jma_rows()
        jma_row = jma_rows[-1]
    except (URLError, ValueError, TimeoutError, OSError) as exc:
        jma_error = f"failed to fetch ({exc})"

    parts = [
        f"=== Tokyo Haneda Airport ({ICAO}) Weather Report — {now} UTC ===",
        "",
        headline(metar_entry, jma_row),
        "",
    ]

    if metar_entry:
        parts.append(format_metar(metar_entry))
    else:
        parts.append(f"METAR: {metar_error}")

    parts.append("")

    try:
        taf_data = fetch_json(TAF_URL)
        if taf_data:
            parts.append(format_taf(taf_data[0]))
        else:
            parts.append("TAF: no data available")
    except (URLError, ValueError, TimeoutError, OSError) as exc:
        parts.append(f"TAF: failed to fetch ({exc})")

    parts.append("")

    if jma_row:
        parts.append(format_jma(jma_row))
    else:
        parts.append(f"JMA AMeDAS: {jma_error}")

    parts.append("")

    try:
        nowcast_time, nowcast_temp = fetch_open_meteo_current()
        parts.append(format_open_meteo_current(nowcast_time, nowcast_temp))
    except (URLError, ValueError, TimeoutError, OSError) as exc:
        parts.append(f"Open-Meteo nowcast: failed to fetch ({exc})")

    parts.append("")

    try:
        hour_label, model_results = fetch_model_forecasts()
        parts.append(format_model_forecasts(hour_label, model_results))
    except (URLError, ValueError, TimeoutError, OSError) as exc:
        parts.append(f"Open-Meteo models: failed to fetch ({exc})")

    if pair_log:
        parts.append("")
        try:
            pairs = build_pairs(metar_history, jma_rows)
            added, existing = update_pair_log(pair_log, pairs)
            parts.append(format_pair_summary(pair_log, added, existing))
        except OSError as exc:
            parts.append(f"METAR vs JMA log: failed ({exc})")

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Monitor Tokyo Haneda (RJTT) weather via METAR/TAF, JMA AMeDAS "
                    "observations and model forecasts."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Polling interval in seconds. If omitted, runs once and exits.",
    )
    parser.add_argument(
        "--pair-log",
        metavar="CSV",
        help="Append same-time METAR/JMA temperature pairs to this CSV and "
             "report the running bias between the two sources.",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        print(get_report(args.pair_log))
        return

    try:
        while True:
            print(get_report(args.pair_log))
            print()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
