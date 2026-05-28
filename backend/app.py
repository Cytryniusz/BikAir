"""Wejście aplikacji Flask dla BikAir."""
import logging

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

import config
from api import api_bp
from services import air_quality

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


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

    @app.errorhandler(404)
    def not_found(_exc):
        return jsonify(error="Nie znaleziono zasobu."), 404

    @app.errorhandler(500)
    def server_error(_exc):
        return jsonify(error="Wewnętrzny błąd serwera."), 500

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)