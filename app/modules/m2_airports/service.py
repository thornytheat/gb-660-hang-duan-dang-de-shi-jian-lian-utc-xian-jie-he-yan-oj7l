from .registry import airport_zone, airport_version
from .contracts import normalize_airport

def run(payload: dict | None = None) -> dict:
    payload = payload or {"airport": "jfk", "at": "2026-11-16"}
    code = normalize_airport(payload["airport"])
    at = payload["at"]
    return {"airport": code, "timezone": airport_zone(code, at), "version": airport_version(code, at)}

def demo():
    return run()
