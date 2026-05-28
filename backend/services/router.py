"""Wyznaczanie tras z wagami zależnymi od jakości powietrza.

Rdzeń BikAir: na grafie dróg (NetworkX/OSMnx) liczymy najkrótszą ścieżkę, ale
koszt krawędzi to nie tylko jej długość — doliczamy karę proporcjonalną do AQI
w danym miejscu. Profile z `config.ROUTING_PROFILES` sterują tym, jak mocno
penalizujemy zanieczyszczone powietrze:

    koszt(krawędź) = distance_weight * length + aqi_weight * length * (aqi / 50)

Dla profilu "shortest" (aqi_weight=0) sprowadza się to do zwykłej najkrótszej
trasy. Dla "clean_air" trasa nadkłada drogi, by omijać strefy złego powietrza.

Używamy A* z heurystyką = odległość w linii prostej * distance_weight. Ponieważ
składnik AQI jest nieujemny, heurystyka jest dopuszczalna (nie przeszacowuje),
więc A* zwraca trasę optymalną względem przyjętego kosztu.
"""
from __future__ import annotations

import logging

import networkx as nx

import config
from services import graph_manager
from services.airly_client import Sensor
from services.sensor_mapper import (
    annotate_graph_with_aqi,
    haversine_distance,
    interpolate_value,
)

logger = logging.getLogger(__name__)

# Wartość AQI traktowana jako "neutralna" przy skalowaniu kary.
AQI_REFERENCE = 50.0


class RoutingError(Exception):
    """Nie udało się wyznaczyć trasy (brak ścieżki, zły obszar itp.)."""


def _iter_edge_attrs(edge_dict: dict):
    """Zwróć iterowalne atrybutów krawędzi, niezależnie od typu grafu.

    MultiDiGraph: {key: {atrybuty}}  → zwracamy wartości (dicty atrybutów).
    Zwykły graf:  {atrybuty}         → zwracamy listę z jednym dictem.
    """
    values = list(edge_dict.values())
    if values and isinstance(values[0], dict):
        return edge_dict.values()
    return [edge_dict]


def _make_weight_fn(distance_weight: float, aqi_weight: float):
    """Zbuduj funkcję kosztu krawędzi dla danego profilu."""

    def weight(u: int, v: int, edge_dict: dict) -> float:
        best = None
        for attrs in _iter_edge_attrs(edge_dict):
            length = attrs.get("length", 1.0)
            cost = distance_weight * length
            aqi = attrs.get("aqi")
            if aqi is not None and aqi_weight:
                cost += aqi_weight * length * (aqi / AQI_REFERENCE)
            if best is None or cost < best:
                best = cost
        return best if best is not None else float("inf")

    return weight


def _make_heuristic(graph: nx.MultiDiGraph, target: int, distance_weight: float):
    """Heurystyka A*: odległość w linii prostej do celu * distance_weight."""
    ty = graph.nodes[target]["y"]
    tx = graph.nodes[target]["x"]

    def heuristic(node: int, _target: int) -> float:
        ny = graph.nodes[node]["y"]
        nx_ = graph.nodes[node]["x"]
        return distance_weight * haversine_distance(ny, nx_, ty, tx)

    return heuristic


def _edge_length(graph: nx.MultiDiGraph, u: int, v: int) -> float:
    """Długość (m) najkrótszej krawędzi między dwoma węzłami ścieżki."""
    edges = graph.get_edge_data(u, v) or graph.get_edge_data(v, u) or {}
    lengths = [attrs.get("length", 0.0) for attrs in _iter_edge_attrs(edges)] if edges else []
    return min(lengths) if lengths else 0.0


def _edge_aqi(graph: nx.MultiDiGraph, u: int, v: int) -> float | None:
    """AQI krawędzi (z najkrótszej z równoległych krawędzi)."""
    edges = graph.get_edge_data(u, v) or graph.get_edge_data(v, u) or {}
    if not edges:
        return None
    best = min(_iter_edge_attrs(edges), key=lambda a: a.get("length", float("inf")))
    return best.get("aqi")


def _path_stats(graph: nx.MultiDiGraph, path: list[int],
                sensors: list[Sensor]) -> dict:
    """Policz dystans, czas, AQI i ekspozycję PM2.5 dla ścieżki węzłów."""
    total_length_m = 0.0
    aqi_weighted_sum = 0.0
    aqi_length = 0.0
    max_aqi: float | None = None

    for u, v in zip(path[:-1], path[1:]):
        length = _edge_length(graph, u, v)
        total_length_m += length
        aqi = _edge_aqi(graph, u, v)
        if aqi is not None:
            aqi_weighted_sum += aqi * length
            aqi_length += length
            max_aqi = aqi if max_aqi is None else max(max_aqi, aqi)

    distance_km = total_length_m / 1000.0
    speed = config.DEFAULT_CYCLING_SPEED_KMH
    duration_min = (distance_km / speed) * 60.0 if speed > 0 else 0.0

    avg_aqi = (aqi_weighted_sum / aqi_length) if aqi_length > 0 else None

    # Spalone kalorie — proste oszacowanie dla umiarkowanej jazdy (~35 kcal/km).
    calories = round(distance_km * 35)

    stats = {
        "distance_km": round(distance_km, 2),
        "duration_min": round(duration_min),
        "avg_speed_kmh": round(speed, 1),
        "avg_aqi": round(avg_aqi) if avg_aqi is not None else None,
        "max_aqi": round(max_aqi) if max_aqi is not None else None,
        "calories_kcal": calories,
    }

    # Ekspozycja PM2.5: interpolujemy stężenie w środku trasy i mnożymy przez czas.
    if path and sensors:
        mid = path[len(path) // 2]
        mlat, mlng = graph_manager.node_coords(graph, mid)
        avg_pm25 = interpolate_value(mlat, mlng, sensors, lambda s: s.pm25)
        if avg_pm25 is not None:
            stats["avg_pm25"] = round(avg_pm25, 1)
            stats["pm25_exposure"] = round(avg_pm25 * (duration_min / 60.0), 1)

    return stats


def _ensure_graph_annotated(graph: nx.MultiDiGraph, sensors: list[Sensor]) -> None:
    """Wpisz AQI na krawędzie, ale tylko gdy zmienił się zestaw czujników.

    Annotacja całego grafu miasta jest kosztowna, więc cache'ujemy ją po
    tożsamości listy czujników (ta sama lista z cache => graf już opisany).
    """
    token = id(sensors)
    if graph.graph.get("_aqi_token") == token:
        return
    if sensors:
        annotate_graph_with_aqi(graph, sensors)
    else:
        # Brak czujników — wyzeruj ewentualne stare wartości.
        for _, _, data in graph.edges(data=True):
            data["aqi"] = None
    graph.graph["_aqi_token"] = token


def compute_routes(area_key: str,
                   start: tuple[float, float],
                   end: tuple[float, float],
                   sensors: list[Sensor],
                   profiles: list[str] | None = None) -> list[dict]:
    """Wyznacz trasy dla podanych profili między punktami start i end.

    Zwraca listę tras (po jednej na profil), każda z geometrią i statystykami.
    Identyczne geometrycznie trasy są deduplikowane (np. gdy brak danych AQI,
    "clean_air" i "shortest" dają to samo).
    """
    profiles = profiles or ["clean_air", "shortest"]

    graph = graph_manager.get_graph(area_key)
    _ensure_graph_annotated(graph, sensors)

    start_node = graph_manager.find_nearest_node(graph, start[0], start[1])
    end_node = graph_manager.find_nearest_node(graph, end[0], end[1])

    if start_node == end_node:
        raise RoutingError("Punkt startu i celu mapują się na ten sam węzeł grafu — wybierz dalsze punkty.")

    routes: list[dict] = []
    seen_signatures: set[tuple] = set()

    for profile_name in profiles:
        if profile_name not in config.ROUTING_PROFILES:
            logger.warning("Pomijam nieznany profil: %s", profile_name)
            continue
        profile = config.ROUTING_PROFILES[profile_name]
        weight_fn = _make_weight_fn(profile["distance_weight"], profile["aqi_weight"])
        heuristic = _make_heuristic(graph, end_node, profile["distance_weight"])

        try:
            path = nx.astar_path(graph, start_node, end_node,
                                 heuristic=heuristic, weight=weight_fn)
        except nx.NetworkXNoPath as exc:
            raise RoutingError(f"Brak trasy między punktami dla profilu {profile_name}.") from exc

        signature = (path[0], path[-1], len(path), tuple(path[:: max(1, len(path) // 10)]))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)

        coords = graph_manager.path_to_coordinates(graph, path)
        stats = _path_stats(graph, path, sensors)

        routes.append({
            "id": profile_name,
            "profile": profile_name,
            "label": _PROFILE_LABELS.get(profile_name, profile_name),
            "recommended": profile_name == profiles[0],
            "geometry": [[lat, lng] for lat, lng in coords],
            "stats": stats,
        })

    if not routes:
        raise RoutingError("Nie udało się wyznaczyć żadnej trasy.")

    return routes


_PROFILE_LABELS = {
    "clean_air": "Najzdrowsza",
    "shortest": "Najkrótsza",
    "balanced": "Zbalansowana",
}