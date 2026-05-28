"""Mapowanie odczytów czujników Airly na krawędzie grafu drogowego.

Strategia: dla każdej krawędzi liczymy jej środek geograficzny, potem
interpolujemy AQI metodą Inverse Distance Weighting (IDW) z N najbliższych
czujników. Wynik zapisujemy jako atrybut `aqi` na krawędzi.

IDW jest prostą metodą — w produkcji można rozważyć kriging, ale do prototypu
i niskiej gęstości czujników IDW jest wystarczająco dokładny.
"""
from __future__ import annotations

import logging
import math
from typing import Iterable

import networkx as nx

from services.airly_client import Sensor

logger = logging.getLogger(__name__)

# Promień Ziemi w metrach.
EARTH_RADIUS_M = 6_371_000


def haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Odległość między dwoma punktami na powierzchni Ziemi (metry)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _edge_midpoint(graph: nx.MultiDiGraph, u: int, v: int) -> tuple[float, float]:
    """Środek krawędzi (średnia współrzędnych końców). Wystarczające do IDW."""
    nu, nv = graph.nodes[u], graph.nodes[v]
    return (nu["y"] + nv["y"]) / 2, (nu["x"] + nv["x"]) / 2


def interpolate_aqi(lat: float, lng: float, sensors: list[Sensor],
                    k: int = 4, power: float = 2.0,
                    max_distance_m: float = 5000.0) -> float | None:
    """Oszacuj AQI w punkcie metodą IDW z k najbliższych czujników.

    - k:               ile czujników brać do interpolacji
    - power:           wykładnik IDW (2 to standard)
    - max_distance_m:  ignoruj czujniki dalsze niż tyle metrów

    Zwraca None, jeśli żaden czujnik nie jest w zasięgu.
    """
    valid = [s for s in sensors if s.aqi is not None]
    if not valid:
        return None

    distances = [(s, haversine_distance(lat, lng, s.lat, s.lng)) for s in valid]
    distances = [(s, d) for s, d in distances if d <= max_distance_m]
    if not distances:
        return None

    # Jeśli punkt pokrywa się z czujnikiem — zwróć wprost.
    for s, d in distances:
        if d < 1.0:
            return float(s.aqi)

    distances.sort(key=lambda pair: pair[1])
    nearest = distances[:k]

    weight_sum = 0.0
    value_sum = 0.0
    for s, d in nearest:
        w = 1.0 / (d ** power)
        weight_sum += w
        value_sum += w * s.aqi
    return value_sum / weight_sum


def annotate_graph_with_aqi(graph: nx.MultiDiGraph, sensors: list[Sensor],
                            k: int = 4, power: float = 2.0,
                            max_distance_m: float = 5000.0) -> int:
    """Wpisz interpolowane AQI na każdą krawędź grafu jako atrybut `aqi`.

    Zwraca liczbę krawędzi, które dostały wartość.
    Krawędzie poza zasięgiem czujników dostają None — router potraktuje je
    jako neutralne (waga = sama długość).
    """
    if not sensors:
        logger.warning("Brak czujników do annotacji grafu.")
        return 0

    count = 0
    for u, v, key, data in graph.edges(keys=True, data=True):
        mid_lat, mid_lng = _edge_midpoint(graph, u, v)
        aqi = interpolate_aqi(mid_lat, mid_lng, sensors, k=k, power=power,
                              max_distance_m=max_distance_m)
        data["aqi"] = aqi
        if aqi is not None:
            count += 1

    logger.info("Annotacja AQI: %d/%d krawędzi w zasięgu czujników",
               count, graph.number_of_edges())
    return count


def sensors_in_bbox(sensors: Iterable[Sensor], bbox: tuple[float, float, float, float]) -> list[Sensor]:
    """Filtruj czujniki do tych mieszczących się w bbox (north, south, east, west)."""
    north, south, east, west = bbox
    return [s for s in sensors if south <= s.lat <= north and west <= s.lng <= east]
