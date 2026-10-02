"""
Servico de detalhes de satelites - DADOS REAIS via SatNOGS DB.

Fonte primaria: https://db.satnogs.org/api/satellites/ (catalogo publico,
sem chave). Fallback para CelesTrak SATCAT se disponivel.

A classificacao "militar" e heuristica por palavras-chave no nome.
"""

import threading
import time

import requests

from .http_headers import DEFAULT_HEADERS

SATNOGS_SAT_URL = "https://db.satnogs.org/api/satellites/"
SATCAT_URL = "https://celestrak.org/satcat/records.php"
SATNOGS_MEDIA_BASE = "https://db.satnogs.org"

_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 24 * 60 * 60

_MILITARY_NAME_HINTS = [
    "usa ", "nrol", "milstar", "wgs ", "aehf", "sbirs", "dsp ", "dmsp",
    "cosmos", "kosmos", "yaogan", "shijian", "gaofen", "luch", "gssap",
    "keyhole", "lacrosse", "onyx", "trumpet", "orion", "mentor",
]

_COUNTRY_MAP = {
    "US": ("Estados Unidos", "us"),
    "USA": ("Estados Unidos", "us"),
    "RU": ("Rússia", "ru"),
    "RUS": ("Rússia", "ru"),
    "CIS": ("Rússia / CEI", "ru"),
    "CN": ("China", "cn"),
    "PRC": ("China", "cn"),
    "UK": ("Reino Unido", "gb"),
    "GB": ("Reino Unido", "gb"),
    "FR": ("França", "fr"),
    "DE": ("Alemanha", "de"),
    "GER": ("Alemanha", "de"),
    "JP": ("Japão", "jp"),
    "JPN": ("Japão", "jp"),
    "IN": ("Índia", "in"),
    "IND": ("Índia", "in"),
    "CA": ("Canadá", "ca"),
    "BR": ("Brasil", "br"),
    "BRAZ": ("Brasil", "br"),
    "AR": ("Argentina", "ar"),
    "AU": ("Austrália", "au"),
    "AUS": ("Austrália", "au"),
    "KR": ("Coreia do Sul", "kr"),
    "ROK": ("Coreia do Sul", "kr"),
    "IL": ("Israel", "il"),
    "ISRA": ("Israel", "il"),
    "ESA": ("Agência Espacial Europeia (ESA)", None),
}


def _satnogs_image_url(raw_image):
    """
    O campo "image" da SatNOGS DB (fotos enviadas pela comunidade,
    sobretudo cubesats) vem como caminho relativo (ex.:
    "media/satellites/XXXX.jpg"), nao URL completa. O codigo nunca lia
    esse campo - o painel de detalhes nao tinha como mostrar foto
    nenhuma mesmo quando ela existia na base. Aqui so resolve o caminho
    relativo contra o host da SatNOGS DB; se já vier absoluto, usa como
    está.
    """
    if not raw_image:
        return None
    raw_image = str(raw_image).strip()
    if not raw_image:
        return None
    if raw_image.startswith("http://") or raw_image.startswith("https://"):
        return raw_image
    return f"{SATNOGS_MEDIA_BASE}/{raw_image.lstrip('/')}"


def _looks_military(object_name):
    name = (object_name or "").lower()
    return any(hint in name for hint in _MILITARY_NAME_HINTS)


def _country_info(code_or_name):
    if not code_or_name:
        return "Desconhecido", None, None
    key = str(code_or_name).strip().upper()
    entry = _COUNTRY_MAP.get(key)
    if entry:
        name, iso2 = entry
        flag = f"https://flagcdn.com/48x36/{iso2}.png" if iso2 else None
        return name, iso2, flag
    # try as free-form name
    return str(code_or_name), None, None


def _from_satnogs(norad_id):
    # SatNOGS allows filter by norad_cat_id
    resp = requests.get(
        SATNOGS_SAT_URL,
        params={"format": "json", "norad_cat_id": norad_id},
        headers=DEFAULT_HEADERS,
        timeout=12,
    )
    resp.raise_for_status()
    records = resp.json()
    if not isinstance(records, list) or not records:
        return None

    rec = records[0]
    name = rec.get("name") or rec.get("names") or f"NORAD {norad_id}"
    countries = rec.get("countries") or ""
    country_code = countries.split(",")[0].strip() if countries else None
    owner_name, _, flag_url = _country_info(country_code)

    launched = rec.get("launched")
    if launched and "T" in str(launched):
        launched = str(launched).split("T")[0]

    return {
        "norad_id": norad_id,
        "object_name": name,
        "object_type": "Carga útil (satélite)" if rec.get("status") == "in orbit" else (rec.get("status") or "—"),
        "owner_name": owner_name or rec.get("operator") or "—",
        "owner_code": country_code,
        "flag_url": flag_url,
        "launch_date": launched,
        "launch_site": None,
        "period_min": None,
        "inclination_deg": None,
        "apogee_km": None,
        "perigee_km": None,
        "ops_status": rec.get("status"),
        "is_military": _looks_military(name),
        "classification_is_heuristic": True,
        "website": rec.get("website") or None,
        "image_url": _satnogs_image_url(rec.get("image")),
    }


def _from_celestrak(norad_id):
    resp = requests.get(
        SATCAT_URL,
        params={"CATNR": norad_id, "FORMAT": "JSON"},
        headers=DEFAULT_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    records = resp.json()
    if not isinstance(records, list):
        records = [records] if records else []
    if not records:
        return None

    record = records[0]
    owner_code = record.get("OWNER")
    owner_name, _, flag_url = _country_info(owner_code)
    object_name = record.get("OBJECT_NAME")
    type_map = {
        "PAY": "Carga útil (satélite)",
        "R/B": "Estágio de foguete",
        "DEB": "Detrito espacial",
        "UNK": "Desconhecido",
    }

    return {
        "norad_id": norad_id,
        "object_name": object_name,
        "object_type": type_map.get(record.get("OBJECT_TYPE"), record.get("OBJECT_TYPE")),
        "owner_name": owner_name,
        "owner_code": owner_code,
        "flag_url": flag_url,
        "launch_date": record.get("LAUNCH_DATE"),
        "launch_site": record.get("LAUNCH_SITE"),
        "period_min": record.get("PERIOD"),
        "inclination_deg": record.get("INCLINATION"),
        "apogee_km": record.get("APOGEE"),
        "perigee_km": record.get("PERIGEE"),
        "ops_status": record.get("OPS_STATUS_CODE"),
        "is_military": _looks_military(object_name),
        "classification_is_heuristic": True,
        # CelesTrak SATCAT nao tem foto - so a SatNOGS DB (_from_satnogs)
        # fornece esse campo.
        "image_url": None,
    }


def get_satellite_details(norad_id):
    norad_id = str(norad_id).strip()
    if not norad_id.isdigit():
        return None, "invalid"

    now = time.time()
    with _cache_lock:
        cached = _cache.get(norad_id)
        if cached and (now - cached["timestamp"]) < _CACHE_TTL_SECONDS:
            return cached["data"], cached["source"]

    details = None
    source = "unavailable"

    try:
        details = _from_satnogs(norad_id)
        if details:
            source = "satnogs_live"
    except Exception:
        details = None

    if details is None:
        try:
            details = _from_celestrak(norad_id)
            if details:
                source = "celestrak_satcat_live"
        except Exception:
            details = None

    if details is None and source == "unavailable":
        source = "not_found"

    with _cache_lock:
        _cache[norad_id] = {"data": details, "source": source, "timestamp": now}

    return details, source
