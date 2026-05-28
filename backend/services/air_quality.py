"""Orkiestracja danych o jakości powietrza dla obsługiwanych obszarów.

Łączy klienta Airly z warstwą cache: pobiera czujniki wraz z pomiarami wokół
centrum obszaru i trzyma je przez `config.AIRLY_CACHE_TTL`. Dzięki temu kolejne
żądania routingu nie odpytują Airly za każdym razem (limity planu darmowego).

Jeśli klucz API nie jest ustawiony albo Airly zwróci błąd, zwracamy pustą listę —
router potraktuje wtedy wszystkie krawędzie jako neutralne i policzy zwykłą
najkrótszą trasę. Dzięki temu backend da się uruchomić i zademonstrować bez klucza.
"""
from __future__ import annotations

import logging

import config
from services.airly_client import AirlyClient, AirlyError, Sensor
from services.cache import TTLCache
from services.sensor_mapper import haversine_distance, sensors_in_bbox

logger = logging.getLogger(__name__)

# Wspólny cache dla wszystkich obszarów. Klucz = area_key.
_sensor_cache = TTLCache(default_ttl=config.AIRLY_CACHE_TTL)

# Jeden klient na proces.
_client = AirlyClient()


def _bbox_radius_km(center: tuple[float, float], bbox: tuple[float, float, float, float]) -> float:
    """Promień (km) od centrum obszaru do najdalszego rogu bbox — z zapasem."""
    lat, lng = center
    north, south, east, west = bbox
    corners = [(north, west), (north, east), (south, west), (south, east)]
    max_m = max(haversine_distance(lat, lng, clat, clng) for clat, clng in corners)
    return max_m / 1000.0 * 1.1  # +10% zapasu


def get_sensors_for_area(area_key: str, force_refresh: bool = False) -> list[Sensor]:
    """Zwróć czujniki z aktualnymi pomiarami dla danego obszaru (z cache).

    Zwraca pustą listę, gdy brak klucza API lub Airly jest niedostępne — to
    świadomy fallback, nie błąd.
    """
    if area_key not in config.SUPPORTED_AREAS:
        raise ValueError(f"Nieobsługiwany obszar: {area_key}")

    if not force_refresh:
        cached = _sensor_cache.get(area_key)
        if cached is not None:
            return cached

    area = config.SUPPORTED_AREAS[area_key]

    if not _client.keys.has_any():
        logger.warning("Brak kluczy Airly — zwracam pustą listę czujników dla %s.", area_key)
        _sensor_cache.set(area_key, [])
        return []

    center = area["center"]
    radius_km = _bbox_radius_km(center, area["bbox"])

    try:
        sensors = _client.fetch_sensors_with_measurements(
            lat=center[0], lng=center[1], max_distance_km=radius_km, max_results=100,
        )
    except AirlyError as exc:
        logger.error("Airly niedostępne dla %s: %s — zwracam pustą listę.", area_key, exc)
        # Cache'ujemy pustkę na krótko, żeby nie zalewać API próbami.
        _sensor_cache.set(area_key, [], ttl=60)
        return []

    # Trzymamy tylko czujniki w granicach obszaru — interpolacja poza bbox nie ma sensu.
    sensors = sensors_in_bbox(sensors, area["bbox"])
    _sensor_cache.set(area_key, sensors)
    logger.info("Airly: %d czujników z pomiarami w obszarze %s", len(sensors), area_key)
    return sensors


def cache_age_seconds(area_key: str) -> float | None:
    """Wiek danych w cache (sekundy) — do pola `refreshed_ago` w API."""
    return _sensor_cache.entry_age(area_key)


def key_pool_stats() -> dict:
    """Stan puli kluczy Airly (do /api/health): ile łącznie, ile dostępnych."""
    return _client.keys.stats()