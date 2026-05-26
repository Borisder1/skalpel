#!/usr/bin/env python3
"""
Fetch logs from Render for a service and save them locally.

Usage examples:
  python fetch_render_logs.py --service-id srv-xxxx --api-key rnd_xxxx --hours 24
  RENDER_API_KEY=... RENDER_SERVICE_ID=... python fetch_render_logs.py --hours 24
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests


def _iso_utc(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _request_json(url: str, headers: dict[str, str], params: dict[str, Any]) -> tuple[int, Any]:
    r = requests.get(url, headers=headers, params=params, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    return r.status_code, body


def fetch_render_logs(api_key: str, service_id: str, start: str, end: str, limit: int = 2000) -> dict[str, Any]:
    """
    Render had multiple logs endpoints over time. We try known variants.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    base = "https://api.render.com/v1/services"
    endpoint_candidates = [
        f"{base}/{service_id}/logs",
        f"{base}/{service_id}/events",
    ]

    params_candidates = [
        {"startTime": start, "endTime": end, "limit": limit},
        {"start": start, "end": end, "limit": limit},
        {"from": start, "to": end, "limit": limit},
    ]

    attempts: list[dict[str, Any]] = []
    for url in endpoint_candidates:
        for params in params_candidates:
            status, body = _request_json(url, headers, params)
            attempts.append({"url": url, "params": params, "status": status, "sample": str(body)[:500]})
            if status == 200:
                return {
                    "ok": True,
                    "url": url,
                    "params": params,
                    "status": status,
                    "data": body,
                    "attempts": attempts,
                }

    return {"ok": False, "attempts": attempts}


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch Render logs for last N hours and save JSON/TXT.")
    ap.add_argument("--api-key", default=os.getenv("RENDER_API_KEY", ""))
    ap.add_argument("--service-id", default=os.getenv("RENDER_SERVICE_ID", ""))
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--out-dir", default="render_logs")
    args = ap.parse_args()

    if not args.api_key or not args.service_id:
        print("❌ Missing API key or service id. Use --api-key/--service-id or env vars.", file=sys.stderr)
        return 2

    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(hours=max(1, args.hours))
    start_iso = _iso_utc(start)
    end_iso = _iso_utc(end)

    result = fetch_render_logs(args.api_key, args.service_id, start_iso, end_iso)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = end.strftime("%Y%m%d_%H%M%S")
    raw_json_path = out_dir / f"render_logs_{args.service_id}_{stamp}.json"
    raw_json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if not result.get("ok"):
        print("❌ Failed to fetch logs from Render API. See attempts in file:")
        print(f"   {raw_json_path}")
        return 1

    data = result.get("data")
    txt_path = out_dir / f"render_logs_{args.service_id}_{stamp}.txt"
    lines: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                line = item.get("message") or item.get("log") or item.get("text") or json.dumps(item, ensure_ascii=False)
            else:
                line = str(item)
            lines.append(line)
    elif isinstance(data, dict):
        # Common shapes: {"logs":[...]}, {"events":[...]}
        arr = data.get("logs") or data.get("events") or data.get("data") or []
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict):
                    line = item.get("message") or item.get("log") or item.get("text") or json.dumps(item, ensure_ascii=False)
                else:
                    line = str(item)
                lines.append(line)
        else:
            lines.append(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        lines.append(str(data))
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    print("✅ Render logs fetched successfully.")
    print(f"JSON: {raw_json_path}")
    print(f"TXT : {txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

