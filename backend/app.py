"""Wejście aplikacji Flask dla BikAir."""
import logging
import os
import threading

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

import config
from api import api_bp
from services import air_quality, graph_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_preload = {
    "sensors_done": False,
    "graph_done": False,
}


def _preload_worker():
    log = logging.getLogger(__name__)
    try:
        log.info("Preload: pobieranie czujników dla Warszawy…")
        air_quality.get_sensors_for_area("warsaw")
        log.info("Preload: czujniki gotowe.")
    except Exception as exc:
        log.error("Preload: błąd czujników: %s", exc)
    finally:
        _preload["sensors_done"] = True

    try:
        log.info("Preload: ładowanie grafu OSMnx dla Warszawy…")
        graph_manager.get_graph("warsaw")
        log.info("Preload: graf gotowy.")
    except Exception as exc:
        log.error("Preload: błąd grafu: %s", exc)
    finally:
        _preload["graph_done"] = True


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)

    app.register_blueprint(api_bp)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/health")
    def health():
        return jsonify(
            status="ok",
            service="bikair-backend",
            version="0.1.0",
            airly_keys=air_quality.key_pool_stats(),
            supported_areas=list(config.SUPPORTED_AREAS),
        )

    @app.get("/api/ready")
    def ready():
        """Stan gotowości wstępnego ładowania (dla frontendu)."""
        sensors_done = _preload["sensors_done"]
        graph_done = _preload["graph_done"]
        return jsonify(
            ready=sensors_done and graph_done,
            sensors_done=sensors_done,
            graph_done=graph_done,
        )

    @app.errorhandler(404)
    def not_found(_exc):
        return jsonify(error="Nie znaleziono zasobu."), 404

    @app.errorhandler(500)
    def server_error(_exc):
        return jsonify(error="Wewnętrzny błąd serwera."), 500

    # Uruchom pre-loading w tle — pomijamy watcher werkzeug (WERKZEUG_RUN_MAIN
    # jest ustawione tylko w procesie dziecka, który faktycznie serwuje).
    in_reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if in_reloader_child or not config.DEBUG:
        t = threading.Thread(target=_preload_worker, daemon=True, name="preload")
        t.start()

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
