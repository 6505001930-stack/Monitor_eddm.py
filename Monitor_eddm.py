#!/usr/bin/env python3
"""Weather monitor for Munich Airport (ICAO: EDDM).

Fetches current METAR and TAF reports from the NOAA Aviation Weather
Center API, plus raw 10-minute station observations from the German
weather service (DWD), and prints a human-readable summary. Can run
once or loop at a fixed interval to keep monitoring conditions.
"""

import argparse
import csv
import io
import sys
import time
import zipfile
from datetime import datetime, timezone
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


def fetch_dwd_temperature():
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
    return station_id, rows[-1]


def format_dwd(station_id: int, row: dict) -> str:
    lines = [
        f"DWD raw 10-min observation — station {station_id} (Munich)",
        f"  Timestamp : {row.get('MESS_DATUM', 'N/A')} (DWD MESS_DATUM)",
        f"  Air temp  : {row.get('TT_10', 'N/A')} C   (TT_10, 2m height, raw)",
        f"  Dewpoint  : {row.get('TD_10', 'N/A')} C",
        f"  Humidity  : {row.get('RF_10', 'N/A')} %",
        f"  Pressure  : {row.get('PP_10', 'N/A')} hPa",
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
    return results


def format_model_forecasts(results) -> str:
    lines = ["Open-Meteo model comparison — 2m temperature (Munich)"]
    for model, current, today_max in results:
        cur_str = f"{current} C" if current is not None else "N/A"
        max_str = f"{today_max} C" if today_max is not None else "N/A"
        lines.append(f"  {model:<16}: now {cur_str:>7}   today max {max_str:>7}")
    return "\n".join(lines)


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

    parts.append("")

    try:
        station_id, row = fetch_dwd_temperature()
        parts.append(format_dwd(station_id, row))
    except (URLError, ValueError, TimeoutError, zipfile.BadZipFile, StopIteration) as exc:
        parts.append(f"DWD: failed to fetch ({exc})")

    parts.append("")

    try:
        model_results = fetch_model_forecasts()
        parts.append(format_model_forecasts(model_results))
    except (URLError, ValueError, TimeoutError) as exc:
        parts.append(f"Open-Meteo models: failed to fetch ({exc})")

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Monitor Munich Airport (EDDM) weather via METAR/TAF and DWD raw 10-min data."
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
