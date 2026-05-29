"""Ładowanie i cache grafów drogowych z OpenStreetMap (OSMnx).

OSMnx pobiera siatkę dróg z Overpass API i zwraca multigraf NetworkX.
Pobranie jest powolne (kilka–kilkadziesiąt sekund dla miasta), więc cache'ujemy
na dysku jako pliki .graphml.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

import networkx as nx
import osmnx as ox

import config

logger = logging.getLogger(__name__)

# OSMnx >= 1.9 ma ustawienia jako moduł `settings`.
ox.settings.use_cache = True
ox.settings.log_console = False


def _graph_path(area_key: str) -> str:
    return str(config.GRAPH_DIR / f"{area_key}_bike.graphml")


def load_graph_for_area(area_key: str, force_refresh: bool = False) -> nx.MultiDiGraph:
    """Załaduj graf rowerowy dla obszaru z cache na dysku albo pobierz z OSM.

    `network_type='bike'` w OSMnx oznacza graf, którym można jechać rowerem
    (wyklucza autostrady, włącza drogi rowerowe i drogi z dopuszczeniem rowerów).
    """
    if area_key not in config.SUPPORTED_AREAS:
        raise ValueError(f"Nieobsługiwany obszar: {area_key}")

    path = _graph_path(area_key)

    if not force_refresh:
        try:
            graph = ox.load_graphml(path)
            logger.info("Graf %s wczytany z dysku: %d węzłów, %d krawędzi",
                       area_key, graph.number_of_nodes(), graph.number_of_edges())
            return graph
        except FileNotFoundError:
            logger.info("Brak cache dla %s — pobieram z OSM.", area_key)

    area = config.SUPPORTED_AREAS[area_key]
    north, south, east, west = area["bbox"]

    # OSMnx 2.x API: bbox=(left, bottom, right, top) = (west, south, east, north)
    graph = ox.graph_from_bbox(bbox=(west, south, east, north),
                                network_type="bike", simplify=True)

    # Dodajemy length (w metrach) — używamy przy liczeniu wag.
    graph = ox.distance.add_edge_lengths(graph)

    ox.save_graphml(graph, path)
    logger.info("Graf %s pobrany i zapisany: %d węzłów, %d krawędzi",
               area_key, graph.number_of_nodes(), graph.number_of_edges())
    return graph


@lru_cache(maxsize=4)
def get_graph(area_key: str) -> nx.MultiDiGraph:
    """Zwróć graf z cache w pamięci procesu (LRU). Buduje raz na proces."""
    return load_graph_for_area(area_key)


def find_nearest_node(graph: nx.MultiDiGraph, lat: float, lng: float) -> int:
    """Znajdź ID węzła grafu najbliższego podanym współrzędnym."""
    # OSMnx 1.9: nearest_nodes(graph, X=longitude, Y=latitude)
    return int(ox.distance.nearest_nodes(graph, X=lng, Y=lat))


def detect_area_for_point(lat: float, lng: float) -> Optional[str]:
    """Spróbuj rozpoznać, do którego ze wspieranych obszarów należy punkt."""
    for key, area in config.SUPPORTED_AREAS.items():
        north, south, east, west = area["bbox"]
        if south <= lat <= north and west <= lng <= east:
            return key
    return None


def node_coords(graph: nx.MultiDiGraph, node_id: int) -> tuple[float, float]:
    """Zwróć (lat, lng) dla węzła grafu."""
    node = graph.nodes[node_id]
    return node["y"], node["x"]


def path_to_coordinates(graph: nx.MultiDiGraph, node_ids: list[int]) -> list[tuple[float, float]]:
    """Zamień listę węzłów na listę współrzędnych (lat, lng).

    Uwzględnia geometrię krawędzi (jeśli istnieje) — wtedy linia trasy
    podąża za rzeczywistym kształtem ulicy, a nie tylko łączy węzły.
    """
    if len(node_ids) < 2:
        return [node_coords(graph, n) for n in node_ids]

    coords: list[tuple[float, float]] = []
    for u, v in zip(node_ids[:-1], node_ids[1:]):
        # MultiDiGraph: może być wiele krawędzi u→v, bierzemy najkrótszą.
        edges = graph.get_edge_data(u, v)
        if not edges:
            # Jeśli graf jest "kierunkowy w drugą stronę", spróbuj v→u.
            edges = graph.get_edge_data(v, u) or {}
        if edges:
            best = min(edges.values(), key=lambda e: e.get("length", float("inf")))
            geometry = best.get("geometry")
            if geometry is not None:
                # Shapely LineString: punkty jako (lng, lat).
                pts = [(y, x) for x, y in geometry.coords]
                # Pierwszy punkt segmentu może się dublować z poprzednim — zostawiamy,
                # potem deduplikujemy.
                coords.extend(pts)
                continue
        coords.append(node_coords(graph, u))
    coords.append(node_coords(graph, node_ids[-1]))

    # Deduplikacja sąsiednich identycznych punktów.
    deduped = [coords[0]]
    for pt in coords[1:]:
        if pt != deduped[-1]:
            deduped.append(pt)
    return deduped
