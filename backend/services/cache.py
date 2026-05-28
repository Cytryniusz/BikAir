"""Prosty cache TTL w pamięci procesu.

Używany głównie do pomiarów Airly — plan darmowy ma limity zapytań, więc
trzymamy odpowiedzi przez `config.AIRLY_CACHE_TTL` sekund i nie odpytujemy
API częściej niż to konieczne.

Cache jest świadomie trywialny (dict + timestampy). W produkcji wieloprocesowej
trzeba by użyć Redis/memcached, ale dla prototypu działającego w jednym procesie
Flaska to wystarcza.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional


class TTLCache:
    """Cache klucz→wartość z czasem życia wpisu (sekundy)."""

    def __init__(self, default_ttl: float = 600.0):
        self.default_ttl = default_ttl
        # key -> (expires_at, created_at, value)
        self._store: dict[str, tuple[float, float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """Zwróć wartość, jeśli istnieje i nie wygasła — inaczej None."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, _created_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        ttl = self.default_ttl if ttl is None else ttl
        now = time.monotonic()
        with self._lock:
            self._store[key] = (now + ttl, now, value)

    def get_or_set(self, key: str, producer: Callable[[], Any],
                   ttl: Optional[float] = None) -> Any:
        """Zwróć wartość z cache albo wylicz ją `producer()` i zapamiętaj.

        Producent jest wołany poza blokadą, żeby wolne wywołanie sieciowe
        (np. do Airly) nie blokowało innych odczytów cache.
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        value = producer()
        self.set(key, value, ttl)
        return value

    def entry_age(self, key: str) -> Optional[float]:
        """Wiek wpisu w sekundach (do informacji w API), albo None."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            _expires_at, created_at, _value = entry
            return round(time.monotonic() - created_at, 1)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()