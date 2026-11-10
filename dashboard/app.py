
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


def add_password(app, password: str, user: str = "admin"):
    """HTTP Basic auth on every page except /healthz (used by uptime pingers)."""
    import hmac
    from flask import Response, request

    @app.before_request
    def _check():
        if request.path == "/healthz":
            return None
        a = request.authorization
        if a and hmac.compare_digest(a.username or "", user) and hmac.compare_digest(a.password or "", password):
            return None
        return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="Indian AI Trader"'})
    return app


def run_dashboard(settings, mode, port=None):
    """Start the dashboard locally (127.0.0.1:8050) or on Render (0.0.0.0:$PORT, password protected).
    Render is detected from its RENDER env var; DASHBOARD_HOST overrides."""
    on_render = bool(os.getenv("RENDER"))
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0" if on_render else "127.0.0.1")
    port = int(port or os.getenv("PORT", 8050))
    password = os.getenv("DASHBOARD_PASSWORD", "")
    app = create_app(settings, mode)
    if host != "127.0.0.1":
        if not password:
            raise SystemExit("Refusing to open the dashboard to the internet without DASHBOARD_PASSWORD. Set it, then restart.")
        add_password(app, password, os.getenv("DASHBOARD_USER", "admin"))
        print(f"Dashboard listening on {host}:{port} (password protected)")
    else:
        if password:
            add_password(app, password, os.getenv("DASHBOARD_USER", "admin"))
        print(f"Dashboard: http://127.0.0.1:{port}   (Ctrl+C to stop; only this computer can open it)")
    app.run(host=host, port=port, debug=False, use_reloader=False)
