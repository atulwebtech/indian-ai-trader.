
"""Local and cloud dashboard (Flask).
Read-only: no order controls, no secrets.
Supports Render deployment using 0.0.0.0 and PORT.
"""

import json
import os
import time
from pathlib import Path

from flask import Flask, jsonify, render_template

from config.settings import ROOT
from dashboard.state import DashboardState

TEMPLATES = Path(__file__).parent / "templates"


def create_app(settings, mode, **kw) -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATES))

    option_state = Path(
        kw.pop("option_state_path", None)
        or (ROOT / "data" / "option_state.json")
    )

    state = DashboardState(settings, mode, **kw)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def api_state():
        resp = jsonify(state.build())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/options")
    def options_page():
        return render_template("options.html")

    @app.get("/api/options")
    def api_options():
        """Read-only option monitor status."""

        try:
            st = json.loads(option_state.read_text())
        except (OSError, ValueError):
            resp = jsonify({
                "available": False,
                "message": (
                    "Monitor not running. "
                    "Start it with: ./run.sh option-monitor --live"
                )
            })
            resp.headers["Cache-Control"] = "no-store"
            return resp

        age = time.time() - float(st.get("updated_epoch", 0))

        st.update(
            available=True,
            age_seconds=round(age, 1),
            monitor_stale=age > 15
        )

        resp = jsonify(st)
        resp.headers["Cache-Control"] = "no-store"

        return resp

    @app.get("/healthz")
    def health():
        return {"ok": True}

    return app


def run_dashboard(settings, mode, port=None):
    """Start Flask dashboard locally or on Render."""

    # Render automatically supplies PORT.
    # Local development defaults to port 8050.
    port = int(port or os.environ.get("PORT", 8050))

    app = create_app(settings, mode)

    print(f"Dashboard listening on 0.0.0.0:{port}")

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
