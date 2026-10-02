from flask import Flask
import threading


def _warmup_caches():
    """Pré-aquece caches em background para o 1º clique não esperar as fontes."""
    try:
        from .services.satellite_service import get_satellites_tle
        get_satellites_tle()
    except Exception:
        pass
    try:
        from .services.aircraft_service import get_aircraft
        get_aircraft()
    except Exception:
        pass
    try:
        from .services.camera_service import get_cameras
        get_cameras()
    except Exception:
        pass
    try:
        from .services.ship_service import get_ships
        get_ships()
    except Exception:
        pass
    try:
        from .services.infra_service import get_power_plants
        get_power_plants()
    except Exception:
        pass
    try:
        # Varredura de rede é a mais lenta de todas (scan de portas +
        # geoIP) — pré-aquecer em background evita que o 1º clique na
        # camada trave a UI esperando o scan completo terminar.
        from .services.computer_service import get_computers
        get_computers()
    except Exception:
        pass


def create_app():
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["JSON_SORT_KEYS"] = False

    from .routes.pages import pages_bp
    from .routes.aircraft import aircraft_bp
    from .routes.satellites import satellites_bp
    from .routes.cameras import cameras_bp
    from .routes.stats import stats_bp
    from .routes.diagnostics import diagnostics_bp
    from .routes.ships import ships_bp
    from .routes.infrastructure import infrastructure_bp
    from .routes.computers import computers_bp

    app.register_blueprint(pages_bp)
    app.register_blueprint(aircraft_bp, url_prefix="/api/aircraft")
    app.register_blueprint(satellites_bp, url_prefix="/api/satellites")
    app.register_blueprint(cameras_bp, url_prefix="/api/cameras")
    app.register_blueprint(stats_bp, url_prefix="/api/stats")
    app.register_blueprint(diagnostics_bp, url_prefix="/api/diagnostics")
    app.register_blueprint(ships_bp, url_prefix="/api/ships")
    app.register_blueprint(infrastructure_bp, url_prefix="/api/infrastructure")
    app.register_blueprint(computers_bp, url_prefix="/api/computers")

    # Pré-carrega TLEs / aeronaves / câmeras assim que o servidor sobe,
    # para o usuário não esperar 20–60s no primeiro clique nas camadas.
    threading.Thread(target=_warmup_caches, daemon=True).start()

    return app
