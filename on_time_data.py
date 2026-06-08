"""Punctuality reference data and estimators.

Juhe has separate flight-dynamics/flight-status products, but the current
project only configures the fare-search API. Until a dedicated on-time endpoint
and key are configured, this module uses conservative airline/airport estimates.
"""

from __future__ import annotations

import os


AIRLINE_PUNCTUALITY = {
    "MU": "较高",
    "CA": "中等",
    "CZ": "较高",
    "HU": "中等",
    "MF": "较高",
    "SC": "较高",
    "ZH": "中等",
    "3U": "中等",
    "FM": "较高",
    "9C": "中等",
    "HO": "中等",
    "KN": "中等",
    "GJ": "中等",
}

CONGESTED_AIRPORTS = {"PEK", "PVG", "CAN", "SZX", "CTU"}


def estimate_punctuality(airline: str, dep_airport: str, arr_airport: str) -> dict:
    airline_code = str(airline or "").strip().upper()[:2]
    dep = str(dep_airport or "").strip().upper()
    arr = str(arr_airport or "").strip().upper()
    level = AIRLINE_PUNCTUALITY.get(airline_code, "中等")
    risk_factors = []
    if dep in CONGESTED_AIRPORTS:
        risk_factors.append(f"{dep}为繁忙枢纽，高峰易流控")
    if arr in CONGESTED_AIRPORTS:
        risk_factors.append(f"{arr}为繁忙枢纽，到达可能受流控影响")
    return {
        "level": level,
        "risk_factors": risk_factors,
        "source": "标准估算",
        "note": "准点率为估算，非实时航班动态",
    }


def juhe_ontime_configured() -> bool:
    return bool(os.getenv("JUHE_ONTIME_ENDPOINT") and os.getenv("JUHE_FLIGHT_KEY"))
