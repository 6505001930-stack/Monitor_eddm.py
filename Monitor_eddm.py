#!/usr/bin/env python3
"""Weather monitor for Munich Airport (ICAO: EDDM).

The headline temperature comes from METAR, which is the freshest actual
measurement available for the airport: it is issued every 30 minutes and
reaches the API within a few minutes. The DWD open-data feeds are also
real measurements but are archive products published 40-50 minutes
behind wall clock, so they are reported as supporting detail rather than
as the current temperature. Every reading is printed with its age so a
stale figure is never mistaken for a live one.

Also fetches TAF (aerodrome forecast) and compares 2m temperature across
four forecast models via Open-Meteo.
"""

import argparse
import csv
import io
import os
import statistics
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.request import urlopen
import json

ICAO = "EDDM"
METAR_URL = f"https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json"
TAF_URL = f"https://aviationweather.gov/api/data/taf?ids={ICAO}&format=json"
REQUEST_TIMEOUT = 10

DWD_AIR_TEMP_URL = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    "climate/10_minutes/air_temperature"
)
# The station description file lives under recent/, not now/.
DWD_STATION_LIST_URL = f"{DWD_AIR_TEMP_URL}/recent/zehn_min_tu_Beschreibung_Stationen.txt"
DWD_ZIP_URL_TEMPLATE = DWD_AIR_TEMP_URL + "/now/10minutenwerte_TU_{station_id:05d}_now.zip"
# München-Flughafen, used only if the station list lookup below fails.
DWD_FALLBACK_STATION_ID = 1262
DWD_MISSING_VALUE = "-999"

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
MUNICH_LAT = 48.3538  # EDDM
MUNICH_LON = 11.7861
OPEN_METEO_MODELS = ["icon_seamless", "gfs_seamless", "ecmwf_ifs025", "ukmo_seamless"]


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
    if text.isdigit() and len(text) == 12:  # DWD MESS_DATUM, e.g. 202608191150
        return datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)

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


def find_munich_station_id() -> int:
    with urlopen(DWD_STATION_LIST_URL, timeout=REQUEST_TIMEOUT) as response:
        text = response.read().decode("latin-1")

    matches = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        if "münchen" in line.lower() or "muenchen" in line.lower():
            matches.append((int(parts[0]), line))

    for station_id, line in matches:
        if "flughafen" in line.lower():
            return station_id
    if matches:
        return matches[0][0]
    return DWD_FALLBACK_STATION_ID


def fetch_dwd_rows():
    """Return (station_id, every valid 10-minute row in today's file)."""
    station_id = find_munich_station_id()
    url = DWD_ZIP_URL_TEMPLATE.format(station_id=station_id)
    with urlopen(url, timeout=REQUEST_TIMEOUT) as response:
        zip_bytes = response.read()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        product_name = next(
            name for name in zf.namelist() if name.startswith("produkt_zehn_now_tu")
        )
        with zf.open(product_name) as f:
            text = f.read().decode("latin-1")

    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    reader.fieldnames = [name.strip() for name in reader.fieldnames]
    rows = [{k.strip(): v.strip() for k, v in row.items() if k} for row in reader]
    rows = [row for row in rows if row.get("TT_10", DWD_MISSING_VALUE) != DWD_MISSING_VALUE]
    if not rows:
        raise ValueError("no valid TT_10 readings in DWD response")
    return station_id, rows


def format_dwd(station_id: int, row: dict) -> str:
    measured = parse_utc(row.get("MESS_DATUM"))
    lines = [
        f"DWD station observation — station {station_id} (Munich)",
        f"  Measured : {format_stamp(measured)}",
        f"  Air temp : {row.get('TT_10', 'N/A')} C   (TT_10, 2m height, raw)",
        f"  Dewpoint : {row.get('TD_10', 'N/A')} C",
        f"  Humidity : {row.get('RF_10', 'N/A')} %",
        f"  Pressure : {row.get('PP_10', 'N/A')} hPa",
    ]
    return "\n".join(lines)


def fetch_metar_history(hours: int = 6):
    url = f"{METAR_URL}&hours={hours}"
    return fetch_json(url) or []


def build_pairs(metar_entries, dwd_rows):
    """Pair METAR and DWD readings that share the exact same observation time.

    METAR is issued at :20 and :50, and DWD's 10-minute series has rows at
    those same minutes, so an exact timestamp match gives a like-for-like
    comparison. Comparing each source's newest value instead would compare
    readings taken up to half an hour apart.
    """
    dwd_by_time = {}
    for row in dwd_rows:
        moment = parse_utc(row.get("MESS_DATUM"))
        raw = row.get("TT_10")
        if moment is None or raw in (None, "", DWD_MISSING_VALUE):
            continue
        try:
            dwd_by_time[moment] = float(raw)
        except ValueError:
            continue

    pairs = {}
    for entry in metar_entries:
        moment = parse_utc(entry.get("obsTime") or entry.get("reportTime"))
        temp = entry.get("temp")
        if moment is None or temp is None or moment not in dwd_by_time:
            continue
        try:
            metar_temp = float(temp)
        except (TypeError, ValueError):
            continue
        dwd_temp = dwd_by_time[moment]
        # How fast the air was warming or cooling at that moment, from DWD's
        # own series. If TT_10 is a mean over the trailing 10 minutes while
        # METAR is a spot reading, the difference between them will track
        # this trend instead of being a fixed sensor offset.
        previous = dwd_by_time.get(moment - timedelta(minutes=10))
        trend = round(dwd_temp - previous, 2) if previous is not None else None
        pairs[moment] = (metar_temp, dwd_temp, round(metar_temp - dwd_temp, 2), trend)
    return pairs


def update_pair_log(path: str, pairs: dict):
    """Merge new pairs into the CSV, keyed by observation time.

    DWD publishes 40-50 minutes late, so the DWD row matching a given METAR
    often only appears on a later run. Keying by timestamp lets those gaps
    fill themselves in without creating duplicates.
    """
    fields = ["obs_time_utc", "metar_temp_c", "dwd_tt10_c", "diff_c", "dwd_trend_c"]
    existing = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("obs_time_utc"):
                    existing[row["obs_time_utc"]] = row

    added = 0
    for moment, (metar_temp, dwd_temp, diff, trend) in pairs.items():
        key = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        if key not in existing:
            added += 1
        existing[key] = {
            "obs_time_utc": key,
            "metar_temp_c": metar_temp,
            "dwd_tt10_c": dwd_temp,
            "diff_c": diff,
            "dwd_trend_c": "" if trend is None else trend,
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
            trend = float(row.get("dwd_trend_c") or "")
        except ValueError:
            continue
        if trend > 0.1:
            rising.append(diff)
        elif trend < -0.1:
            falling.append(diff)

    lines = [
        "METAR vs DWD same-time comparison",
        f"  Log      : {path} ({len(diffs)} pairs, {added} new this run)",
    ]
    if not diffs:
        lines.append("  No overlapping timestamps yet — DWD lags, so pairs fill in later.")
        return "\n".join(lines)

    mean = statistics.fmean(diffs)
    lines.append(f"  Mean diff: {mean:+.2f} C (METAR minus DWD)")
    if len(diffs) > 1:
        lines.append(f"  Spread   : sd {statistics.stdev(diffs):.2f} C, "
                     f"range {min(diffs):+.1f} to {max(diffs):+.1f} C")
    lines.append(f"  Sign     : {sum(1 for d in diffs if d > 0)} positive, "
                 f"{sum(1 for d in diffs if d == 0)} zero, "
                 f"{sum(1 for d in diffs if d < 0)} negative")

    # A trailing-window mean compared against a spot reading looks warm while
    # the air warms and cool while it cools, so the sign flip separates that
    # artefact from a genuine sensor offset. A fixed offset does not flip.
    if rising:
        lines.append(f"  While warming ({len(rising)} pairs): mean {statistics.fmean(rising):+.2f} C")
    if falling:
        lines.append(f"  While cooling ({len(falling)} pairs): mean {statistics.fmean(falling):+.2f} C")

    if len(diffs) < 20:
        lines.append("  Too few pairs to conclude anything yet.")
    elif not falling:
        lines.append("  No cooling-phase pairs yet — cannot yet separate a sensor offset")
        lines.append("  from an averaging artefact. Wait for the evening temperature fall.")
    elif statistics.fmean(rising or [0]) > 0 > statistics.fmean(falling):
        lines.append("  Bias flips sign with the trend — consistent with DWD averaging over")
        lines.append("  10 minutes while METAR reports a spot value, not a sensor difference.")
    elif abs(mean) < 0.3:
        lines.append("  Mean near zero in both phases — consistent with one sensor.")
    else:
        lines.append("  Offset holds in both warming and cooling — points to separate sensors.")
    return "\n".join(lines)


def fetch_open_meteo_current():
    """Open-Meteo's blended nowcast: refreshes roughly every 15 minutes, but
    it is model output smoothed against observations, not a sensor reading.
    Measured once against METAR: it read 25.8 C while METAR read 28.0 C for
    the same moment, a 2.2 C gap — faster than any measurement source here,
    at the cost of accuracy that a real sensor doesn't have.
    """
    url = (
        f"{OPEN_METEO_URL}?latitude={MUNICH_LAT}&longitude={MUNICH_LON}"
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
        f"  Temp     : {temp} C   (model blend, not a sensor — "
        f"off by 2.2 C vs METAR when last checked)",
    ]
    return "\n".join(lines)


def fetch_model_forecasts():
    models_param = ",".join(OPEN_METEO_MODELS)
    url = (
        f"{OPEN_METEO_URL}?latitude={MUNICH_LAT}&longitude={MUNICH_LON}"
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
        "Open-Meteo model comparison — 2m temperature (Munich)",
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


def headline(metar_entry, dwd) -> str:
    """Lead with the freshest real measurement available."""
    if metar_entry:
        observed = parse_utc(metar_entry.get("obsTime") or metar_entry.get("reportTime"))
        return (
            f"CURRENT TEMPERATURE : {metar_entry.get('temp', 'N/A')} C\n"
            f"  source            : METAR {ICAO} (airport sensor)\n"
            f"  observed          : {format_stamp(observed)}"
        )
    if dwd:
        station_id, row = dwd
        measured = parse_utc(row.get("MESS_DATUM"))
        return (
            f"CURRENT TEMPERATURE : {row.get('TT_10', 'N/A')} C\n"
            f"  source            : DWD station {station_id} (METAR unavailable — fallback)\n"
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

    dwd = None
    dwd_rows = []
    dwd_error = None
    try:
        station_id, dwd_rows = fetch_dwd_rows()
        dwd = (station_id, dwd_rows[-1])
    except (URLError, ValueError, TimeoutError, OSError, zipfile.BadZipFile, StopIteration) as exc:
        dwd_error = f"failed to fetch ({exc})"

    parts = [
        f"=== Munich Airport ({ICAO}) Weather Report — {now} UTC ===",
        "",
        headline(metar_entry, dwd),
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

    if dwd:
        parts.append(format_dwd(*dwd))
    else:
        parts.append(f"DWD: {dwd_error}")

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
            pairs = build_pairs(metar_history, dwd_rows)
            added, existing = update_pair_log(pair_log, pairs)
            parts.append(format_pair_summary(pair_log, added, existing))
        except OSError as exc:
            parts.append(f"METAR vs DWD log: failed ({exc})")

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Monitor Munich Airport (EDDM) weather via METAR/TAF, DWD observations and model forecasts."
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
        help="Append same-time METAR/DWD temperature pairs to this CSV and "
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
