"""Klient REST do Airly API (v2).

Airly udostępnia:
  - GET /v2/installations/nearest?lat=&lng=&maxDistanceKM=&maxResults=
  - GET /v2/measurements/installation?installationId=
  - GET /v2/measurements/point?lat=&lng=

Dokumentacja: https://developer.airly.org/docs
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


@dataclass
class Sensor:
    """Pojedynczy czujnik (instalacja) Airly."""
    id: int
    lat: float
    lng: float
    address: str = ""
    elevation: Optional[float] = None
    # Najświeższe pomiary — wypełniane przez fetch_measurements_for_sensor.
    pm25: Optional[float] = None
    pm10: Optional[float] = None
    no2: Optional[float] = None
    aqi: Optional[float] = None
    measured_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lat": self.lat,
            "lng": self.lng,
            "address": self.address,
            "pm25": self.pm25,
            "pm10": self.pm10,
            "no2": self.no2,
            "aqi": self.aqi,
            "measured_at": self.measured_at,
        }


class AirlyError(Exception):
    """Błąd komunikacji z Airly API."""


class AirlyClient:
    """Cienka warstwa nad Airly REST API.

    Klient sam nie cache'uje — to robi warstwa wyżej (services/cache.py).
    """

    def __init__(self, api_key: str = None, timeout: float = 10.0):
        self.api_key = api_key or config.AIRLY_API_KEY
        self.base_url = config.AIRLY_BASE_URL
        self.timeout = timeout
        if not self.api_key:
            logger.warning("AIRLY_API_KEY nie jest ustawiony — wywołania Airly nie zadziałają.")

    # ---------- niskopoziomowe helpery ----------

    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Accept-Language": "pl",
            "apikey": self.api_key,
        }

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}{path}"
        try:
            response = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AirlyError(f"Sieć: {exc}") from exc

        if response.status_code == 429:
            raise AirlyError("Limit zapytań Airly osiągnięty (429).")
        if not response.ok:
            raise AirlyError(f"Airly {response.status_code}: {response.text[:200]}")
        return response.json()

    # ---------- publiczne metody ----------

    def fetch_sensors_near(self, lat: float, lng: float, max_distance_km: float = 30.0,
                          max_results: int = 100) -> list[Sensor]:
        """Pobierz listę czujników w okolicy punktu (bez pomiarów)."""
        data = self._get(
            "/installations/nearest",
            {"lat": lat, "lng": lng, "maxDistanceKM": max_distance_km, "maxResults": max_results},
        )
        sensors: list[Sensor] = []
        for item in data:
            loc = item.get("location", {})
            address = item.get("address") or {}
            address_str = ", ".join(filter(None, [address.get("street"), address.get("city")]))
            sensors.append(Sensor(
                id=item["id"],
                lat=loc.get("latitude"),
                lng=loc.get("longitude"),
                address=address_str,
                elevation=item.get("elevation"),
            ))
        logger.info("Airly: pobrano %d czujników w pobliżu (%.4f, %.4f)", len(sensors), lat, lng)
        return sensors

    def fetch_measurement_for_sensor(self, sensor_id: int) -> dict:
        """Pobierz najnowsze pomiary i indeks AQI dla jednego czujnika."""
        return self._get("/measurements/installation", {"installationId": sensor_id})

    def fetch_measurement_at_point(self, lat: float, lng: float) -> dict:
        """Pobierz interpolowane pomiary w dowolnym punkcie (Airly interpoluje sam)."""
        return self._get("/measurements/point", {"lat": lat, "lng": lng})

    # ---------- wyższego poziomu ----------

    def fetch_sensors_with_measurements(self, lat: float, lng: float,
                                        max_distance_km: float = 30.0,
                                        max_results: int = 100) -> list[Sensor]:
        """Pobierz czujniki i od razu wypełnij ich aktualnymi pomiarami.

        UWAGA: robi N+1 requestów (po jednym na czujnik). Do prototypu OK,
        ale w produkcji powinniśmy używać /measurements/point albo cache'ować.
        """
        sensors = self.fetch_sensors_near(lat, lng, max_distance_km, max_results)
        for sensor in sensors:
            try:
                data = self.fetch_measurement_for_sensor(sensor.id)
                _populate_sensor_from_measurement(sensor, data)
            except AirlyError as exc:
                logger.warning("Nie udało się pobrać pomiarów dla czujnika %s: %s", sensor.id, exc)
        return [s for s in sensors if s.aqi is not None]


def _populate_sensor_from_measurement(sensor: Sensor, data: dict) -> None:
    """Wyciągnij wartości PM2.5, PM10, NO2 i indeks Airly z odpowiedzi /measurements/installation."""
    current = data.get("current") or {}

    for value in current.get("values", []):
        name = value.get("name")
        v = value.get("value")
        if name == "PM25":
            sensor.pm25 = v
        elif name == "PM10":
            sensor.pm10 = v
        elif name == "NO2":
            sensor.no2 = v

    for index in current.get("indexes", []):
        # Airly zwraca kilka indeksów (CAQI, AIRLY_CAQI). Bierzemy pierwszy z wartością.
        if index.get("value") is not None:
            sensor.aqi = float(index["value"])
            break

    sensor.measured_at = current.get("tillDateTime")
