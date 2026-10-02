from flask import Blueprint, jsonify

from ..services.ship_service import get_ships, get_ship_source_counts

ships_bp = Blueprint("ships", __name__)


@ships_bp.route("/")
def list_ships():
    ships, source, total_available = get_ships()
    stats = get_ship_source_counts()
    return jsonify({
        "source": source,
        "is_live": source != "demo",
        "count": len(ships),
        "total": total_available,
        # Quantos navios cada fonte trouxe / por que a fonte falhou.
        # E' o que responde "por que o Atlantico esta vazio?" sem
        # precisar adivinhar.
        "source_counts": stats.get("counts", {}),
        "source_errors": stats.get("errors", {}),
        "ships": ships,
    })
