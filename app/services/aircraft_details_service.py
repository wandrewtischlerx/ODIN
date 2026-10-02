"""
Servico de detalhes de aeronaves - DADOS REAIS via adsbdb.com.

Fonte: https://api.adsbdb.com (publica, gratuita, sem chave) - projeto
"adsbdb" que agrega dados de registro de aeronaves (fabricante, modelo,
operador registrado, pais de origem) e, quando disponivel, uma foto real
da aeronave (via PlaneBase / planespotters).

IMPORTANTE sobre a classificacao "militar" x "civil": nenhuma API
publica e gratuita fornece essa classificacao de forma confiavel e
universal. O campo "is_military" aqui e uma HEURISTICA baseada em
palavras-chave no operador/fabricante/tipo (ex.: "air force", "navy",
"fuerza aerea"). Pode errar - a interface deve deixar claro que e uma
estimativa, nao um fato verificado.
"""

import threading
import time

import requests
from .http_headers import DEFAULT_HEADERS

ADSBDB_URL = "https://api.adsbdb.com/v0/aircraft/{icao24}"
ADSBDB_CALLSIGN_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"

_cache = {}
_route_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 24 * 60 * 60  # dados de registro de aeronave nao mudam com frequencia
_ROUTE_CACHE_TTL_SECONDS = 6 * 60 * 60  # rotas mudam com menos frequência

_MILITARY_KEYWORDS = [
    "air force", "air national guard", "navy", "army", "marine corps",
    "luftwaffe", "royal air force", " raf", "aeronautica militare",
    "armee de l'air", "armée de l'air", "fuerza aerea", "força aérea",
    "forca aerea", "ejercito", "ejército", "armada", "marina militare",
    "gendarmerie", "coast guard", "ministry of defence", "ministry of defense",
    "department of defense", "usaf", "us air force", "nato", "military",
    "policia federal", "polícia federal", "state department",
]


def _looks_military(*fields):
    text = " ".join(f for f in fields if f).lower()
    return any(keyword in text for keyword in _MILITARY_KEYWORDS)


def _flag_url(iso2):
    if not iso2 or len(iso2) != 2:
        return None
    return f"https://flagcdn.com/48x36/{iso2.lower()}.png"


def get_aircraft_details(icao24):
    icao24 = (icao24 or "").strip().lower()
    if not icao24:
        return None, "invalid"

    now = time.time()
    with _cache_lock:
        cached = _cache.get(icao24)
        if cached and (now - cached["timestamp"]) < _CACHE_TTL_SECONDS:
            return cached["data"], cached["source"]

    try:
        resp = requests.get(ADSBDB_URL.format(icao24=icao24), headers=DEFAULT_HEADERS, timeout=8)
        if resp.status_code == 404:
            with _cache_lock:
                _cache[icao24] = {"data": None, "source": "not_found", "timestamp": now}
            return None, "not_found"

        resp.raise_for_status()
        payload = resp.json()
        aircraft = (payload.get("response") or {}).get("aircraft")
        if not aircraft:
            with _cache_lock:
                _cache[icao24] = {"data": None, "source": "not_found", "timestamp": now}
            return None, "not_found"

        iso2 = aircraft.get("registered_owner_country_iso_name")
        details = {
            "icao24": icao24,
            "registration": aircraft.get("registration"),
            "manufacturer": aircraft.get("manufacturer"),
            "type": aircraft.get("type"),
            "icao_type": aircraft.get("icao_type"),
            "registered_owner": aircraft.get("registered_owner"),
            "country_name": aircraft.get("registered_owner_country_name"),
            "country_iso2": iso2,
            "flag_url": _flag_url(iso2),
            "photo_url": aircraft.get("url_photo") or aircraft.get("url_photo_thumbnail"),
            "is_military": _looks_military(
                aircraft.get("registered_owner"),
                aircraft.get("manufacturer"),
                aircraft.get("type"),
            ),
            "classification_is_heuristic": True,
        }
        source = "adsbdb_live"
    except Exception:
        details = None
        source = "unavailable"

    with _cache_lock:
        _cache[icao24] = {"data": details, "source": source, "timestamp": now}

    return details, source


def get_aircraft_route(callsign):
    """
    Rota provável (origem / destino) associada ao callsign via adsbdb.
    Não é o plano de voo do momento — é o mapeamento callsign→rota
    conhecido na base pública.
    """
    callsign = (callsign or "").strip().upper()
    if not callsign:
        return None, "invalid"

    now = time.time()
    with _cache_lock:
        cached = _route_cache.get(callsign)
        if cached and (now - cached["timestamp"]) < _ROUTE_CACHE_TTL_SECONDS:
            return cached["data"], cached["source"]

    try:
        resp = requests.get(
            ADSBDB_CALLSIGN_URL.format(callsign=callsign),
            headers=DEFAULT_HEADERS,
            timeout=8,
        )
        if resp.status_code == 404:
            with _cache_lock:
                _route_cache[callsign] = {
                    "data": None,
                    "source": "not_found",
                    "timestamp": now,
                }
            return None, "not_found"

        resp.raise_for_status()
        payload = resp.json()
        fr = (payload.get("response") or {}).get("flightroute")
        if not fr:
            with _cache_lock:
                _route_cache[callsign] = {
                    "data": None,
                    "source": "not_found",
                    "timestamp": now,
                }
            return None, "not_found"

        route = {
            "callsign": fr.get("callsign") or callsign,
            "callsign_icao": fr.get("callsign_icao"),
            "callsign_iata": fr.get("callsign_iata"),
            "airline": fr.get("airline"),
            "origin": fr.get("origin"),
            "destination": fr.get("destination"),
            "midpoint": fr.get("midpoint"),
        }
        source = "adsbdb_live"
    except Exception:
        route = None
        source = "unavailable"

    with _cache_lock:
        _route_cache[callsign] = {
            "data": route,
            "source": source,
            "timestamp": now,
        }

    return route, source
