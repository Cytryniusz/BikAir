"""Konfiguracja aplikacji BikAir."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
GRAPH_DIR = DATA_DIR / "graphs"

GRAPH_DIR.mkdir(parents=True, exist_ok=True)

AIRLY_API_KEY = os.getenv("AIRLY_API_KEY", "")
AIRLY_BASE_URL = "https://airapi.airly.eu/v2"

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", 5000))
DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

# Obszary, które obsługujemy w prototypie. Bbox: (north, south, east, west).
SUPPORTED_AREAS = {
    "warsaw": {
        "name": "Warszawa",
        "center": (52.2297, 21.0122),
        "bbox": (52.37, 52.10, 21.27, 20.85),
    },
    "krakow": {
        "name": "Kraków",
        "center": (50.0647, 19.9450),
        "bbox": (50.13, 49.97, 20.10, 19.79),
    },
}

# Wagi dla algorytmu routingu — zmieniają sposób penalizacji złego powietrza.
ROUTING_PROFILES = {
    "clean_air": {"distance_weight": 1.0, "aqi_weight": 3.0},
    "shortest": {"distance_weight": 1.0, "aqi_weight": 0.0},
    "balanced": {"distance_weight": 1.0, "aqi_weight": 1.5},
}

# TTL dla cache pomiarów Airly (sekundy). Airly aktualizuje się co ~10 min.
AIRLY_CACHE_TTL = 600

# Domyślna prędkość rowerzysty (km/h) do szacowania czasu.
DEFAULT_CYCLING_SPEED_KMH = 16.0
