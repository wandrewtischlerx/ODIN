from flask import Blueprint, jsonify

from ..services.aircraft_service import get_aircraft
from ..services.satellite_service import get_satellites
from ..services.camera_service import get_cameras, get_opencctv_stats
from ..services.ship_service import get_ships
from ..services.computer_service import get_computers

stats_bp = Blueprint("stats", __name__)


@stats_bp.route("/")
def summary():
    """
    Totais globais para o HUD — no boot, sem depender de ligar cada camada.
    Plotagem no Cesium continua sob demanda e por viewport.
    """
    aircraft, aircraft_source, aircraft_total = get_aircraft()
    satellites, satellites_source = get_satellites()
    ships, ships_source, ships_total = get_ships()
    cameras, cameras_source, cameras_global = get_cameras()
    computers, computers_source = get_computers()
    ocv = get_opencctv_stats()
    ocv_total = int(ocv.get("total_markers", 0) or 0)
    # HUD: catálogo completo, nunca o tamanho da amostra plotada.
    global_total = int(cameras_global or 0)
    if ocv_total and global_total < ocv_total:
        global_total = ocv_total

    return jsonify({
        "aircraft": {
            "count": len(aircraft),
            "total": aircraft_total,
            "source": aircraft_source,
            "is_live": aircraft_source != "demo",
        },
        "satellites": {
            "count": len(satellites),
            "source": satellites_source,
            "is_live": satellites_source != "demo",
        },
        "ships": {
            "count": len(ships),
            "total": ships_total,
            "source": ships_source,
            "is_live": ships_source != "demo",
        },
        "cameras": {
            "count": len(cameras),
            "plotted": len(cameras),
            "global_total": global_total,
            "opencctv_total": ocv_total,
            "source": cameras_source,
            "is_live": cameras_source != "demo",
        },
        "computers": {
            "count": len(computers),
            "source": computers_source,
            "is_live": computers_source == "scan_ativo",
        },
    })
