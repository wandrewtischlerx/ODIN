"""
Rota de diagnostico: pensada pra funcionar como os "olhos" do Claude no
ambiente real do usuario, ja que a sandbox de desenvolvimento nao tem
acesso de rede aos hosts externos usados aqui (adsb.lol, adsb.fi,
gisdata.dot.ca.gov etc). Rode localmente e cole o JSON de resposta na
conversa - ele traz tudo que costuma ser preciso pra depurar sem
precisar de acesso de rede direto:

  1) "connectivity"    — teste bruto de cada endpoint (OK/falha, status
                          HTTP, latencia, motivo do erro)
  2) "schema_probes"   — busca ao vivo nas fontes mais sensiveis a
                          mudanca de formato (adsb.lol, adsb.fi,
                          Caltrans) e mostra os campos brutos
                          encontrados vs. os campos que o codigo espera
                          - qualquer divergencia aparece aqui na hora
  3) "layers"          — chama as MESMAS funcoes que a aplicacao usa de
                          verdade (get_aircraft/get_ships/get_cameras),
                          com contagens por fonte/tipo e uma amostra de
                          registros reais ja processados

Acesse em http://localhost:5000/api/diagnostics/
Atencao: por chamar as funcoes reais das camadas (item 3), pode levar
alguns segundos (a varredura de aeronaves por hubs e a parte mais
lenta) - e normal, nao e travamento.
"""

import time

import requests
from flask import Blueprint, jsonify

from ..services.http_headers import DEFAULT_HEADERS
from ..services import aircraft_service, ship_service, camera_service

diagnostics_bp = Blueprint("diagnostics", __name__)

_TARGETS = [
    ("adsb.lol (aeronaves, militar+civil)", "https://api.adsb.lol/v2/mil"),
    ("adsb.fi (aeronaves, espelho civil)", "https://opendata.adsb.fi/api/v2/mil"),
    ("OpenSky Network (aeronaves, mesclado)", "https://opensky-network.org/api/states/all"),
    ("SatNOGS TLE (satelites)", "https://db.satnogs.org/api/tle/?format=json&limit=1"),
    ("SatNOGS satelites (detalhes)", "https://db.satnogs.org/api/satellites/?format=json&norad_cat_id=25544"),
    ("CelesTrak GP (satelites, reserva)", "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle"),
    ("NYC DOT (cameras)", "https://webcams.nyctmc.org/api/cameras"),
    ("Caltrans CCTV (cameras, California)", "https://gisdata.dot.ca.gov/arcgis/rest/services/CHhighway/CCTV/FeatureServer/0/query?where=1=1&outFields=district&resultRecordCount=1&f=json"),
    ("adsbdb (detalhes de aeronave)", "https://api.adsbdb.com/v0/aircraft/a9cee9"),
    ("Digitraffic AIS (navios, Finlândia)", "https://meri.digitraffic.fi/api/ais/v1/locations"),
    ("Kystverket AIS (navios, Noruega)", "https://kystdatahuset.no/ws/api/ais/realtime/geojson"),
    ("Hormuz AIS (Golfo Pérsico)", "https://hormuz.data-tracking.net/api/ships"),
    ("Open Waters / aiscast (navios, multi-região)", "https://ais.openwaters.io/v1/vessels?bbox=58,8,62,12"),
    ("CelesTrak weather (satélites)", "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle"),
    ("CelesTrak visual (satélites)", "https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle"),
    ("Digitraffic weathercam (câmeras FI)", "https://tie.digitraffic.fi/api/weathercam/v1/stations"),
    ("Florida 511 cameras", "https://www.fl511.com/map/mapIcons/Cameras"),
    ("OpenCCTV markers", "https://opencctv.org/api/cameras/markers"),
    ("NOAA NDBC VOS (navios, oceano aberto)", "https://www.ndbc.noaa.gov/ship_obs.php?uom=M&time=2"),
    ("Natural Earth: fronteiras de país (linha)", "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_boundary_lines_land.geojson"),
    ("Natural Earth: fronteiras de estado (linha)", "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces_lines.geojson"),
]

# Campos que o codigo de parsing espera encontrar em cada fonte. Usados
# so pra apontar divergencias no probe de schema - nao afetam o
# funcionamento real do app.
_EXPECTED_ADSB_FIELDS = ["hex", "lat", "lon", "alt_baro", "gs", "dbFlags", "flight", "track", "baro_rate", "geom_rate"]
_EXPECTED_CALTRANS_FIELDS = ["latitude", "longitude", "locationName", "nearbyPlace", "district", "route", "inService", "currentImageURL", "index_"]


def _test_one(name, url):
    start = time.time()
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=8)
        elapsed = round(time.time() - start, 2)
        return {
            "name": name,
            "url": url,
            "ok": resp.ok,
            "http_status": resp.status_code,
            "elapsed_seconds": elapsed,
            "error": None if resp.ok else f"HTTP {resp.status_code}",
        }
    except requests.exceptions.SSLError as e:
        return {"name": name, "url": url, "ok": False, "http_status": None,
                "elapsed_seconds": round(time.time() - start, 2), "error": f"Erro de SSL/certificado: {e}"}
    except requests.exceptions.ConnectTimeout:
        return {"name": name, "url": url, "ok": False, "http_status": None,
                "elapsed_seconds": round(time.time() - start, 2), "error": "Timeout ao conectar (servidor nao respondeu a tempo)"}
    except requests.exceptions.ConnectionError as e:
        return {"name": name, "url": url, "ok": False, "http_status": None,
                "elapsed_seconds": round(time.time() - start, 2), "error": f"Erro de conexao (DNS, firewall ou rede indisponivel): {e}"}
    except Exception as e:
        return {"name": name, "url": url, "ok": False, "http_status": None,
                "elapsed_seconds": round(time.time() - start, 2), "error": f"Erro inesperado: {e}"}


def _probe_adsb_schema(name, base, path):
    """Busca uma amostra crua de aeronaves e mostra os campos encontrados,
    pra comparar com o que _adsbx_style_to_feature() em aircraft_service.py
    espera (_EXPECTED_ADSB_FIELDS). Se algum host mudar o formato da API,
    isso aparece aqui na hora, sem precisar inspecionar o codigo."""
    start = time.time()
    try:
        resp = requests.get(f"{base}{path}", headers=DEFAULT_HEADERS, timeout=10)
        elapsed = round(time.time() - start, 2)
        resp.raise_for_status()
        data = resp.json()
        ac_list = data.get("ac") or []
        sample = ac_list[0] if ac_list else None
        keys_found = sorted(sample.keys()) if isinstance(sample, dict) else []
        missing = [k for k in _EXPECTED_ADSB_FIELDS if sample and k not in sample]
        return {
            "name": name, "ok": True, "elapsed_seconds": elapsed,
            "top_level_keys": sorted(data.keys()),
            "aircraft_count_returned": len(ac_list),
            "sample_aircraft_raw": sample,
            "keys_found_in_sample": keys_found,
            "expected_keys_missing": missing,
        }
    except Exception as e:
        return {"name": name, "ok": False, "error": str(e)}


def _probe_caltrans_schema():
    """Busca 3 registros crus do FeatureServer da Caltrans e mostra os
    nomes de campo reais (attributes) e a geometria, pra comparar com o
    que camera_service._fetch_caltrans_cameras() espera
    (_EXPECTED_CALTRANS_FIELDS). Essa era a parte de maior incerteza da
    implementacao, ja que o schema foi confirmado so por pesquisa, nunca
    testado ao vivo."""
    start = time.time()
    try:
        resp = requests.get(
            camera_service.CALTRANS_CCTV_URL,
            params={"where": "1=1", "outFields": "*", "resultRecordCount": 3, "f": "json"},
            headers=DEFAULT_HEADERS, timeout=15,
        )
        elapsed = round(time.time() - start, 2)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return {"name": "Caltrans CCTV (schema)", "ok": False, "error": data["error"]}
        features = data.get("features") or []
        sample_attrs = features[0].get("attributes") if features else None
        sample_geom = features[0].get("geometry") if features else None
        keys_found = sorted(sample_attrs.keys()) if isinstance(sample_attrs, dict) else []
        missing = [k for k in _EXPECTED_CALTRANS_FIELDS if sample_attrs and k not in sample_attrs]
        return {
            "name": "Caltrans CCTV (schema)", "ok": True, "elapsed_seconds": elapsed,
            "feature_count_in_sample": len(features),
            "sample_attributes_raw": sample_attrs,
            "sample_geometry_raw": sample_geom,
            "keys_found_in_sample": keys_found,
            "expected_keys_missing": missing,
        }
    except Exception as e:
        return {"name": "Caltrans CCTV (schema)", "ok": False, "error": str(e)}


def _layer_summary():
    """Chama as MESMAS funcoes que a aplicacao usa de verdade (o mesmo
    cache de 90s vale aqui tambem), entao os numeros abaixo sao
    exatamente o que o globo esta mostrando (ou mostraria no proximo
    refresh)."""
    out = {}

    try:
        # BUGFIX: get_aircraft() devolve 3 valores (aircraft, source,
        # total_available) - so 2 nomes aqui gerava "too many values to
        # unpack" em TODA chamada, engolido pelo except abaixo. Isso
        # quebrava silenciosamente a secao de aeronaves do
        # /api/diagnostics/ (sempre caia em {"error": ...}).
        aircraft, source, total_available = aircraft_service.get_aircraft()
        military = sum(1 for a in aircraft if a.get("is_military_confirmed"))
        out["aircraft"] = {
            "source": source,
            "sources_contributing": source.split("+"),
            "count": len(aircraft),
            "total_available": total_available,
            "military_count": military,
            "civil_count": len(aircraft) - military,
            "sample": aircraft[:2],
        }
    except Exception as e:
        out["aircraft"] = {"error": str(e)}

    try:
        # BUGFIX: get_ships() devolve 3 valores (ships, source, total).
        # Desempacotar em 2 levantava ValueError e o bloco inteiro caia
        # no except - o diagnostico de navios NUNCA funcionou, sempre
        # devolvia {"error": "too many values to unpack"}.
        ships, source, total_available = ship_service.get_ships()
        stats = ship_service.get_ship_source_counts()
        by_region = {}
        for s in ships:
            region = s.get("source_region") or "?"
            by_region[region] = by_region.get(region, 0) + 1
        out["ships"] = {
            "source": source,
            "sources_contributing": source.split("+"),
            "count": len(ships),
            "total_available": total_available,
            "count_by_region": by_region,
            # Quanto cada fonte trouxe e por que as outras falharam:
            # e' o que separa "fonte fora do ar" de "fonte no ar, mas
            # so cobre a costa dela".
            "count_by_source": stats.get("counts", {}),
            "errors_by_source": stats.get("errors", {}),
            "sample": ships[:2],
            "aisstream_api_key_configured": bool(ship_service.AISSTREAM_API_KEY),
            "aisstream_lib_installed": ship_service._HAS_WEBSOCKET,
        }
    except Exception as e:
        out["ships"] = {"error": str(e)}

    try:
        cameras, source, global_total = camera_service.get_cameras()
        index_report = camera_service.get_camera_index_report()
        # Testa a extracao de HLS contra 1 camera de amostra real - e' o
        # jeito de eu ver, sem rede pra skylinewebcams.com/etc, se o
        # regex bate com o formato real da pagina de quem roda isso local.
        stream_probe = camera_service.get_stream_probe_sample(cameras)
        by_src = {}
        for c in cameras:
            key = c.get("source") or "?"
            by_src[key] = by_src.get(key, 0) + 1
        out["cameras"] = {
            # Indexacao por fonte: total, quantas tem imagem e o motivo
            # das que nao tem. "sem_imagem_indexada" alto = parser desta
            # fonte esta incompleto (problema nosso, nao da camera).
            "index_by_source": index_report,
            "stream_probe": stream_probe,
            "source": source,
            "sources_contributing": source.split("+"),
            "count": len(cameras),
            "global_total": global_total,
            "count_by_source": by_src,
            "sample": cameras[:2],
        }
    except Exception as e:
        out["cameras"] = {"error": str(e)}

    return out


@diagnostics_bp.route("/")
def run_diagnostics():
    connectivity = [_test_one(name, url) for name, url in _TARGETS]

    schema_probes = [
        _probe_adsb_schema("adsb.lol (schema, /mil)", aircraft_service.ADSB_LOL_BASE, "/mil"),
        _probe_adsb_schema("adsb.fi (schema, /mil)", aircraft_service.ADSB_FI_BASE, "/mil"),
        _probe_caltrans_schema(),
    ]

    layers = _layer_summary()

    return jsonify({
        "summary": {
            "connectivity_total": len(connectivity),
            "connectivity_ok": sum(1 for r in connectivity if r["ok"]),
            "connectivity_failed": sum(1 for r in connectivity if not r["ok"]),
        },
        "connectivity": connectivity,
        "schema_probes": schema_probes,
        "layers": layers,
        "notes": [
            "AISstream.io (navios, cobertura global) nao aparece em "
            "'connectivity': e um WebSocket, nao um GET simples. Ver "
            "'layers.ships.aisstream_api_key_configured' e "
            "'aisstream_lib_installed' pra saber o estado dele. Exige a "
            "variavel de ambiente AISSTREAM_API_KEY (cadastro gratuito em "
            "https://aisstream.io) pra ficar ativo.",
            "'layers' chama as mesmas funcoes reais da aplicacao (mesmo "
            "cache de 90s) - os numeros ali sao exatamente o que o globo "
            "esta exibindo agora.",
        ],
    })
