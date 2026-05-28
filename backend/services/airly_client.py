"""Klient REST do Airly API (v2).

Airly udostępnia:
  - GET /v2/installations/nearest?lat=&lng=&maxDistanceKM=&maxResults=
  - GET /v2/measurements/installation?installationId=
  - GET /v2/measurements/point?lat=&lng=

Dokumentacja: https://developer.airly.org/docs
"""
from __future__ import annotations

import logging
import re
import threading
import time
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


def _load_keys_from_file(path: str) -> list[str]:
    """Wczytaj klucze Airly z pliku tekstowego (jeden klucz na linię).

    Akceptuje "1. KLUCZ", "1) KLUCZ" lub samo "KLUCZ". Puste linie i komentarze
    (#) pomija. Zachowuje kolejność i usuwa duplikaty.
    """
    keys: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r"^\d+[.)]\s*(.+)$", line)
                token = (match.group(1) if match else line).split()[0].strip()
                if token:
                    keys.append(token)
    except FileNotFoundError:
        return []
    seen: set[str] = set()
    unique: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


class KeyManager:
    """Pula kluczy Airly z rotacją i cooldownem po wyczerpaniu (429).

    Klient prosi o `available_key()`; gdy klucz dostanie 429/401, woła
    `mark_exhausted(key)`, co odkłada go na `cooldown` sekund. Kolejne żądania
    biorą następny dostępny klucz. Gdy wszystkie są na cooldownie — zwraca None.
    """

    def __init__(self, keys: list[str], cooldown: float = 600.0):
        self._keys = keys
        self._cooldown = cooldown
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls) -> "KeyManager":
        keys = _load_keys_from_file(config.AIRLY_KEYS_FILE)
        if not keys and config.AIRLY_API_KEY and config.AIRLY_API_KEY != "/us":
            keys = [config.AIRLY_API_KEY]
        cooldown = getattr(config, "AIRLY_KEY_COOLDOWN", 600)
        logger.info("Airly KeyManager: załadowano %d kluczy z %s.", len(keys), config.AIRLY_KEYS_FILE)
        return cls(keys, cooldown)

    def has_any(self) -> bool:
        return bool(self._keys)

    def available_key(self) -> Optional[str]:
        now = time.monotonic()
        with self._lock:
            for key in self._keys:
                if self._blocked_until.get(key, 0.0) <= now:
                    return key
        return None

    def mark_exhausted(self, key: str) -> None:
        with self._lock:
            self._blocked_until[key] = time.monotonic() + self._cooldown
        logger.warning("Klucz Airly …%s wyczerpany — cooldown %.0fs.", key[-4:], self._cooldown)

    def stats(self) -> dict:
        now = time.monotonic()
        with self._lock:
            available = sum(1 for k in self._keys if self._blocked_until.get(k, 0.0) <= now)
            return {"total": len(self._keys), "available": available}


class AirlyClient:
    """Cienka warstwa nad Airly REST API z rotacją kluczy.

    Klient sam nie cache'uje pomiarów — to robi warstwa wyżej (services/cache.py).
    """

    def __init__(self, key_manager: "KeyManager" = None, timeout: float = 10.0):
        self.keys = key_manager or KeyManager.from_config()
        self.base_url = config.AIRLY_BASE_URL
        self.timeout = timeout
        if not self.keys.has_any():
            logger.warning("Brak kluczy Airly — wywołania Airly nie zadziałają.")

    # ---------- niskopoziomowe helpery ----------

    def _headers(self, key: str) -> dict:
        return {
            "Accept": "application/json",
            "Accept-Language": "pl",
            "apikey": key,
        }

    def _get(self, path: str, params: dict) -> dict:
        """Wykonaj GET, rotując klucze przy 429/401/403 aż któryś zadziała."""
        url = f"{self.base_url}{path}"
        while True:
            key = self.keys.available_key()
            if key is None:
                raise AirlyError("Wszystkie klucze Airly wyczerpane (limit 429).")
            try:
                response = requests.get(url, headers=self._headers(key),
                                        params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                raise AirlyError(f"Sieć: {exc}") from exc

            if response.status_code in (429, 401, 403):
                self.keys.mark_exhausted(key)
                continue  # spróbuj kolejnego klucza
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
