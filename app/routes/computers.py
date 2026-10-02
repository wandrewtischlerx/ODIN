from flask import Blueprint, jsonify

from ..services.computer_service import (
    get_computers,
    get_scan_status,
    get_computer_details,
)

computers_bp = Blueprint("computers", __name__)


@computers_bp.route("/")
def list_computers():
    computers, source = get_computers()
    return jsonify({
        "source": source,
        "is_live": source == "scan_ativo",
        "count": len(computers),
        "computers": computers,
        "status": get_scan_status(),
    })


@computers_bp.route("/rescan", methods=["POST"])
def rescan_computers():
    """Força uma nova varredura, ignorando o cache de 30min."""
    computers, source = get_computers(force_rescan=True)
    return jsonify({
        "source": source,
        "is_live": source == "scan_ativo",
        "count": len(computers),
        "computers": computers,
        "status": get_scan_status(),
    })


@computers_bp.route("/status")
def scan_status():
    return jsonify(get_scan_status())


@computers_bp.route("/<ip>/details")
def computer_details(ip):
    details = get_computer_details(ip)
    if not details:
        return jsonify({"error": "sem histórico para este IP"}), 404
    return jsonify(details)
