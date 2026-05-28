"""Endpointy REST API BikAir.

Wszystkie ścieżki pod prefiksem /api. Frontend (Leaflet + fetch) konsumuje:

  GET  /api/areas              — obsługiwane obszary (Warszawa, Kraków)
  GET  /api/sensors?area=...   — czujniki Airly z pomiarami (warstwa AQI na mapie)
  GET  /api/geocode?q=...      — zamiana adresu na współrzędne (Nominatim/OSM)
  POST /api/route              — wyznaczenie trasy A→B (główny endpoint)

/api/route przyjmuje też parametry w query stringu (GET), żeby dało się go
łatwo przetestować w przeglądarce.
"""
from __future__ import annotations

import logging

import requests
from flask import Blueprint, jsonify, request

import config
from services import air_quality, graph_manager
from services.cache import TTLCache
from services.router import RoutingError, compute_routes

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__, url_prefix="/api")

# Cache geokodowania — Nominatim prosi o nienadużywanie API.
_geocode_cache = TTLCache(default_ttl=86_400)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


class ApiError(Exception):
    """Błąd zwracany klientowi jako JSON z konkretnym kodem HTTP."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@api_bp.errorhandler(ApiError)
def _handle_api_error(exc: ApiError):
    return jsonify(error=exc.message), exc.status


# ---------- parsowanie wejścia ----------

def _parse_point(value) -> tuple[float, float]:
    """Zamień różne formaty punktu na (lat, lng).

    Akceptuje: [lat, lng], {"lat":.., "lng":..} oraz string "lat,lng".
    """
    try:
        if isinstance(value, str):
            lat_str, lng_str = value.split(",")
            return float(lat_str), float(lng_str)
        if isinstance(value, dict):
            return float(value["lat"]), float(value["lng"])
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return float(value[0]), float(value[1])
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(f"Nieprawidłowy format punktu: {value!r}") from exc
    raise ApiError(f"Nieprawidłowy format punktu: {value!r}")


def _resolve_area(start: tuple[float, float], explicit: str | None) -> str:
    """Ustal obszar: jawnie podany albo wykryty z punktu startowego."""
    if explicit:
        if explicit not in config.SUPPORTED_AREAS:
            raise ApiError(
                f"Nieobsługiwany obszar '{explicit}'. Dostępne: {', '.join(config.SUPPORTED_AREAS)}",
                status=400,
            )
        return explicit

    detected = graph_manager.detect_area_for_point(*start)
    if detected is None:
        raise ApiError(
            "Punkt startowy poza obsługiwanym obszarem. "
            f"Wspieramy: {', '.join(a['name'] for a in config.SUPPORTED_AREAS.values())}.",
            status=400,
        )
    return detected


# ---------- endpointy ----------

@api_bp.get("/areas")
def list_areas():
    """Lista obsługiwanych obszarów z ich centrum i prostokątem granicznym."""
    areas = []
    for key, area in config.SUPPORTED_AREAS.items():
        north, south, east, west = area["bbox"]
        areas.append({
            "key": key,
            "name": area["name"],
            "center": {"lat": area["center"][0], "lng": area["center"][1]},
            "bbox": {"north": north, "south": south, "east": east, "west": west},
        })
    return jsonify(areas=areas, profiles=list(config.ROUTING_PROFILES))


@api_bp.get("/sensors")
def list_sensors():
    """Czujniki Airly z aktualnymi pomiarami dla obszaru (warstwa AQI na mapie)."""
    area_key = request.args.get("area", "warsaw")
    if area_key not in config.SUPPORTED_AREAS:
        raise ApiError(
            f"Nieobsługiwany obszar '{area_key}'. Dostępne: {', '.join(config.SUPPORTED_AREAS)}",
            status=400,
        )
    sensors = air_quality.get_sensors_for_area(area_key)
    return jsonify(
        area=area_key,
        count=len(sensors),
        refreshed_ago=air_quality.cache_age_seconds(area_key),
        sensors=[s.to_dict() for s in sensors],
    )


@api_bp.get("/geocode")
def geocode():
    """Zamień adres/nazwę miejsca na współrzędne (przez Nominatim/OSM)."""
    query = (request.args.get("q") or "").strip()
    if not query:
        raise ApiError("Brak parametru 'q' (adres do wyszukania).")

    cached = _geocode_cache.get(query.lower())
    if cached is not None:
        return jsonify(results=cached, cached=True)

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 5, "countrycodes": "pl"},
            headers={"User-Agent": "BikAir/0.1 (projekt edukacyjny PW)"},
            timeout=10.0,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as exc:
        raise ApiError(f"Geokodowanie niedostępne: {exc}", status=502) from exc

    results = [
        {
            "label": item.get("display_name"),
            "lat": float(item["lat"]),
            "lng": float(item["lon"]),
        }
        for item in raw
    ]
    _geocode_cache.set(query.lower(), results)
    return jsonify(results=results, cached=False)


@api_bp.route("/route", methods=["GET", "POST"])
def route():
    """Wyznacz trasę (lub trasy alternatywne) między punktami A i B.

    Body (POST JSON) albo query (GET):
      start    — [lat,lng] / {"lat","lng"} / "lat,lng"   (wymagane)
      end      — jw.                                       (wymagane)
      area     — klucz obszaru (opcjonalny, inaczej wykrywany ze startu)
      profiles — lista profili, np. ["clean_air","shortest"] (opcjonalna)
    """
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        start_raw = payload.get("start")
        end_raw = payload.get("end")
        area_arg = payload.get("area")
        profiles = payload.get("profiles")
    else:
        start_raw = request.args.get("start")
        end_raw = request.args.get("end")
        area_arg = request.args.get("area")
        profiles_arg = request.args.get("profiles")
        profiles = profiles_arg.split(",") if profiles_arg else None

    if not start_raw or not end_raw:
        raise ApiError("Wymagane są parametry 'start' i 'end'.")

    start = _parse_point(start_raw)
    end = _parse_point(end_raw)
    area_key = _resolve_area(start, area_arg)

    sensors = air_quality.get_sensors_for_area(area_key)

    try:
        routes = compute_routes(area_key, start, end, sensors, profiles=profiles)
    except RoutingError as exc:
        raise ApiError(str(exc), status=422) from exc
    except ValueError as exc:
        raise ApiError(str(exc), status=400) from exc

    return jsonify(
        area=area_key,
        start={"lat": start[0], "lng": start[1]},
        end={"lat": end[0], "lng": end[1]},
        air_quality_available=bool(sensors),
        sensors_used=len(sensors),
        routes=routes,
    )