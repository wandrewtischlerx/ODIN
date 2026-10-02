from flask import Blueprint, jsonify, request

from ..services.infra_service import (
    get_power_plants,
    FUEL_LABELS,
    get_submarine_cables,
    get_cable_landing_points,
    CABLE_ATTRIBUTION,
    get_datacenters,
    DATACENTER_ATTRIBUTION,
    DATACENTER_COVERAGE_NOTE,
)

infrastructure_bp = Blueprint("infrastructure", __name__)

_VALID_FUELS = set(FUEL_LABELS.keys())


@infrastructure_bp.route("/power-plants")
def list_power_plants():
    """
    Usinas de energia (hidrelétrica, nuclear, carvão, solar, eólica).
    Filtro opcional: ?type=hydro,nuclear (lista separada por vírgula).
    Sem filtro, devolve os 5 tipos juntos.
    """
    raw_types = request.args.get("type", "")
    fuel_filter = None
    if raw_types:
        requested = {t.strip().lower() for t in raw_types.split(",") if t.strip()}
        fuel_filter = requested & _VALID_FUELS

    plants, source, counts_by_fuel = get_power_plants(fuel_filter)
    return jsonify({
        "source": source,
        "is_live": source != "demo",
        "count": len(plants),
        # Contagem por tipo sobre o TOTAL do dataset (não sobre o
        # filtro aplicado) — é o que a UI usa para mostrar quantas
        # usinas de cada tipo existem, mesmo com só algumas ligadas.
        "counts_by_fuel": counts_by_fuel,
        "fuel_labels": FUEL_LABELS,
        "plants": plants,
    })


@infrastructure_bp.route("/cables")
def list_cables():
    """
    Cabos submarinos de internet (Bloco 1 do INFRAFISICA.md), a partir
    da API pública da TeleGeography. Devolve tanto as rotas dos cabos
    (`cables`, MultiLineString por cabo) quanto os pontos de
    aterrissagem (`landing_points`) numa resposta só — o frontend
    desenha os dois juntos e não faz sentido separar em duas
    requisições pra' algo que sempre aparece junto.

    `source`/`is_live` seguem o cabo (fonte ao vivo da TeleGeography ou
    fallback demo); os dois catálogos vêm sempre da mesma fonte.
    """
    cables, source = get_submarine_cables()
    landing_points, _ = get_cable_landing_points()
    return jsonify({
        "source": source,
        "is_live": source != "demo",
        "count": len(cables),
        "cables": cables,
        "landing_point_count": len(landing_points),
        "landing_points": landing_points,
        "attribution": CABLE_ATTRIBUTION,
    })


@infrastructure_bp.route("/datacenters")
def list_datacenters():
    """
    Datacenters (Bloco 3 do INFRAFISICA.md), a partir do OpenStreetMap
    via Overpass API (`telecom=data_center` + `building=data_center`).

    Se o Overpass falhar (fora do ar, sobrecarregado ou rede sem
    saída), cai para uma pequena lista curada à mão dos megacampi de
    nuvem/IA mais divulgados publicamente (`source="demo_curated"`,
    coordenadas aproximadas — ver `_DEMO_CURATED_DATACENTERS` em
    infra_service.py) em vez de inventar dados como se fossem do OSM.
    """
    datacenters, source = get_datacenters()
    return jsonify({
        "source": source,
        "is_live": source == "openstreetmap_overpass",
        "count": len(datacenters),
        "datacenters": datacenters,
        "attribution": DATACENTER_ATTRIBUTION,
        "coverage_note": DATACENTER_COVERAGE_NOTE,
    })
