from datetime import datetime, timezone
from app.modules.m2_airports import registry
from app.modules.m3_localtime.resolve import resolve_local

def _leg_date(local_value: str) -> str:
    return datetime.fromisoformat(local_value).date().isoformat()

def build_leg(row: dict) -> dict:
    # 时区来源按航段自身的本地日期解析，使按机场+日期限定的应急接管能即时作用于查询
    departure = resolve_local(
        row["departure_local"],
        registry.airport_zone(row["origin"], _leg_date(row["departure_local"])),
        row.get("departure_fold"),
    )
    arrival = resolve_local(
        row["arrival_local"],
        registry.airport_zone(row["destination"], _leg_date(row["arrival_local"])),
        row.get("arrival_fold"),
    )
    dep_utc = departure.astimezone(timezone.utc); arr_utc = arrival.astimezone(timezone.utc)
    minutes = int((arr_utc - dep_utc).total_seconds() // 60)
    if minutes <= 0 or minutes > 18 * 60:
        raise ValueError("INVALID_FLIGHT_DURATION")
    return {"flight_no": row["flight_no"], "origin": row["origin"], "destination": row["destination"], "departure_utc": dep_utc.isoformat(), "arrival_utc": arr_utc.isoformat(), "duration_minutes": minutes}

def order_legs(legs: list[dict]) -> list[dict]:
    return sorted(legs, key=lambda row: (row["departure_utc"], row["flight_no"]))

