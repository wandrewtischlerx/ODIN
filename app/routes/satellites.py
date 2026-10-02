from flask import Blueprint, jsonify

from ..services.satellite_service import get_satellites, get_satellites_tle
from ..services.satellite_details_service import get_satellite_details

satellites_bp = Blueprint("satellites", __name__)


@satellites_bp.route("/")
def list_satellites():
    satellites, source = get_satellites()
    return jsonify({
        "source": source,
        "is_live": source in ("satnogs_live", "celestrak_live"),
        "count": len(satellites),
        "satellites": satellites,
    })


@satellites_bp.route("/tle")
def list_satellites_tle():
    """
    Elementos orbitais crus (TLE), para o navegador propagar posicao
    localmente via SGP4 (satellite.js) sem precisar repetir fetch a cada
    poucos segundos. Cache do mesmo TTL de 6h usado no calculo server-side.
    """
    entries, source, catalog_total = get_satellites_tle()
    return jsonify({
        "source": source,
        "is_live": source in ("satnogs_live", "celestrak_multi"),
        "count": len(entries),
        "catalog_total": catalog_total,
        "satellites": entries,
    })


@satellites_bp.route("/<norad_id>/details")
def satellite_details(norad_id):
    details, source = get_satellite_details(norad_id)
    return jsonify({
        "source": source,
        "is_live": source in ("satnogs_live", "celestrak_satcat_live"),
        "details": details,
    })
