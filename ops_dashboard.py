import json
import os
from datetime import datetime, timedelta, timezone

OPS_FILE = "ops_dashboard.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load_ops():
    if os.path.exists(OPS_FILE):
        with open(OPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"cycles": [], "events": []}


def save_ops(data):
    with open(OPS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def record_cycle(cycle_data: dict):
    data = load_ops()
    item = {"ts": _now_iso(), **cycle_data}
    data["cycles"].append(item)
    # keep file bounded
    data["cycles"] = data["cycles"][-5000:]
    save_ops(data)


def record_event(kind: str, payload: dict):
    data = load_ops()
    data["events"].append({"ts": _now_iso(), "kind": kind, **payload})
    data["events"] = data["events"][-10000:]
    save_ops(data)


def build_24h_report() -> str:
    data = load_ops()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cycles = [c for c in data["cycles"] if _parse_iso(c["ts"]) >= cutoff]
    events = [e for e in data["events"] if _parse_iso(e["ts"]) >= cutoff]
    if not cycles:
        return "📋 Операційний звіт 24h: даних поки немає."

    n = len(cycles)
    scanned = sum(int(c.get("scanned", 0)) for c in cycles)
    setups = sum(int(c.get("setups", 0)) for c in cycles)
    adx_fail = sum(int(c.get("adx_fail", 0)) for c in cycles)
    vol_fail = sum(int(c.get("vol_fail", 0)) for c in cycles)
    fvg_fail = sum(int(c.get("fvg_fail", 0)) for c in cycles)
    passed = sum(int(c.get("passed_all", 0)) for c in cycles)
    rate_limit = sum(int(c.get("ratelimit", 0)) for c in cycles)
    invalid = sum(int(c.get("invalid", 0)) for c in cycles)
    dry_max = max(int(c.get("dry_cycles", 0)) for c in cycles)
    confirms = sum(1 for e in events if e.get("kind") == "confirm")
    skips = sum(1 for e in events if e.get("kind") == "skip")
    order_ok = sum(1 for e in events if e.get("kind") == "order_opened")
    order_reject = sum(1 for e in events if e.get("kind") == "order_rejected")
    order_exists = sum(1 for e in events if e.get("kind") == "order_already_exists")

    setups_per_cycle = setups / n if n else 0.0
    pass_ratio = (passed / scanned * 100.0) if scanned else 0.0

    return (
        "📋 <b>Операційний звіт за 24 години</b>\n"
        f"Циклів: <b>{n}</b> | Сканувань: <b>{scanned}</b>\n"
        f"Сетапів: <b>{setups}</b> (середнє {setups_per_cycle:.2f}/цикл)\n"
        f"Пройшли всі фільтри: <b>{passed}</b> ({pass_ratio:.1f}%)\n"
        f"Fail фільтрів → ADX: <b>{adx_fail}</b>, VOL: <b>{vol_fail}</b>, FVG: <b>{fvg_fail}</b>\n"
        f"RateLimit: <b>{rate_limit}</b> | Invalid: <b>{invalid}</b> | Max dry streak: <b>{dry_max}</b>\n"
        f"TG рішення → confirm: <b>{confirms}</b>, skip: <b>{skips}</b>\n"
        f"Ордери → відкрито: <b>{order_ok}</b>, reject: <b>{order_reject}</b>, already-exists: <b>{order_exists}</b>"
    )

