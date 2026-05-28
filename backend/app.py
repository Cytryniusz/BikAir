"""Wejście aplikacji Flask dla BikAir."""
from flask import Flask, jsonify
from flask_cors import CORS

import config


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)

    @app.get("/api/health")
    def health():
        return jsonify(
            status="ok",
            service="bikair-backend",
            version="0.1.0",
            airly_configured=bool(config.AIRLY_API_KEY),
        )

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
