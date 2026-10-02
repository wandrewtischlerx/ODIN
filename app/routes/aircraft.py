from flask import Blueprint, jsonify

from ..services.aircraft_service import get_aircraft
from ..services.aircraft_details_service import get_aircraft_details, get_aircraft_route

aircraft_bp = Blueprint("aircraft", __name__)


@aircraft_bp.route("/")
def list_aircraft():
    aircraft, source, total_available = get_aircraft()
    return jsonify({
        "source": source,
        "is_live": source != "demo",
        "count": len(aircraft),
        "total": total_available,
        "aircraft": aircraft,
    })


@aircraft_bp.route("/<icao24>/details")
def aircraft_details(icao24):
    details, source = get_aircraft_details(icao24)
    return jsonify({
        "source": source,
        "is_live": source == "adsbdb_live",
        "details": details,
    })


@aircraft_bp.route("/route/<callsign>")
def aircraft_route(callsign):
    route, source = get_aircraft_route(callsign)
    return jsonify({
        "source": source,
        "is_live": source == "adsbdb_live",
        "route": route,
    })
