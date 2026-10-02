"""
Servico de trafego aquático (navios) em tempo real - fontes abertas.

Fontes REST sem chave (sempre ativas):
  1) Digitraffic / Fintraffic (Finlandia) — Mar Báltico / norte da Europa
     https://meri.digitraffic.fi/api/ais/v1/locations + /vessels
     Licença: CC BY 4.0
  2) Kystdatahuset / Kystverket (Noruega) — águas norueguesas + satélite
     https://kystdatahuset.no/ws/api/ais/realtime/geojson
     Licença: NLOD 2.0
  3) Hormuz Ship Monitor — Golfo Pérsico / Estreito de Ormuz
     https://hormuz.data-tracking.net/api/ships
     Licença: CC BY 4.0
  4) Open Waters / aiscast — agregador open-source (BarentsWatch, AISHub,
     Digitraffic, receivers voluntários). REST GeoJSON sem chave em
     bboxes regionais de alta densidade (Europa N, Canal da Mancha,
     Singapura, Costa Leste EUA, Mediterrâneo, etc.).
     https://ais.openwaters.io/v1/vessels?bbox=...
     Licença: por fonte (open data / NLOD / etc.)

  5) NOAA / NDBC - relatorios VOS (Voluntary Observing Ships)
     https://www.ndbc.noaa.gov/ship_obs.php
     ESTA E' A UNICA FONTE AQUI COM COBERTURA DE OCEANO ABERTO SEM
     CADASTRO. Nao e' AIS: sao navios mercantes/de pesquisa que enviam
     boletim meteorologico por satelite/radio no meio do Atlantico, do
     Pacifico e do Indico. Traz posicao (lat/lon) e indicativo de
     chamada, mas NAO traz MMSI, rumo nem velocidade - por isso esses
     navios aparecem parados e sem curso. Sao algumas centenas por
     janela de 2h, nao milhares. Dominio publico (NOAA).

POR QUE O MEIO DO ATLANTICO/PACIFICO CONTINUA QUASE VAZIO:
AIS terrestre so alcanca ~40-70 km da antena receptora, entao TODAS as
fontes AIS gratuitas (inclusive as 4 acima) sao costeiras por
construcao - nao e' bug do codigo. Ver um navio no meio do oceano exige
AIS por SATELITE (Spire, ORBCOMM, exactEarth/MarineTraffic S-AIS), que
e' produto pago em todos os provedores. As duas unicas formas gratuitas
de ter algo no oceano aberto sao: (a) VOS/NOAA, implementado acima, e
(b) AISstream.io com cadastro gratis (abaixo), que agrega receptores
voluntarios pelo mundo e cobre bem mais costas do que as 4 fontes
regionais - inclusive Brasil.

Fonte opcional (cobertura GLOBAL de verdade), so ativa se configurada:
  5) AISstream.io — WebSocket de AIS mundial, gratuito mas exige
     cadastro (https://aisstream.io) pra gerar uma API key. Nao existe
     fonte AIS global sem cadastro (confirmado: AISHub exige estacao
     receptora propria; MarineTraffic/VesselFinder/Datalastic sao
     pagos). Defina a variavel de ambiente AISSTREAM_API_KEY pra
     habilitar - sem ela, o app funciona normalmente so com as fontes
     regionais acima. Com ela, uma thread de fundo mantem uma conexao
     WebSocket viva e um cache em memoria dos navios vistos nos ultimos
     10 minutos, mesclado aqui com o mesmo padrao das fontes REST.

As fontes ativas sao combinadas, removendo duplicatas por MMSI. Se
TODAS falharem (ou nenhuma estiver configurada/alcancavel), cai para
dados demo (source="demo").
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from .http_headers import DEFAULT_HEADERS

try:
    import websocket  # pacote "websocket-client"; opcional
    _HAS_WEBSOCKET = True
except ImportError:
    _HAS_WEBSOCKET = False

DIGITRAFFIC_LOC = "https://meri.digitraffic.fi/api/ais/v1/locations"
DIGITRAFFIC_VES = "https://meri.digitraffic.fi/api/ais/v1/vessels"
KYST_GEOJSON = "https://kystdatahuset.no/ws/api/ais/realtime/geojson"
HORMUZ_SHIPS = "https://hormuz.data-tracking.net/api/ships"
IFREMER_POSITIONS = "https://localisation.flotteoceanographique.fr/api/v2/positions"
OPENWATERS_VESSELS = "https://ais.openwaters.io/v1/vessels"
# NOAA/NDBC — boletins de navios voluntarios (VOS). Unica fonte aqui com
# cobertura de oceano aberto sem cadastro. HTML com tabela em <pre>.
NDBC_SHIP_OBS_URL = "https://www.ndbc.noaa.gov/ship_obs.php"
AISSTREAM_WS_URL = "wss://stream.aisstream.io/v0/stream"
AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY", "").strip()
_AISSTREAM_MAX_AGE_SECONDS = 10 * 60  # descarta navios sem atualizacao ha mais de 10 min

# Bboxes de alta densidade para Open Waters (minLat,minLon,maxLat,maxLon).
# Limite anônimo ~100 sq deg; cada bbox fica bem abaixo disso.
_OPENWATERS_BBOXES = (
    ("58,8,62,12", "Norte Europa / Skagerrak"),       # ~16 sq deg
    ("50,-2,52,3", "Canal da Mancha"),                # ~10
    ("1,103,2,105", "Singapura / Malaca"),            # ~2
    ("40,-75,42,-72", "NY / Costa Leste EUA"),        # ~6
    ("35,138,36,140", "Tóquio / Baía"),               # ~2
    ("24,54,27,57", "Golfo Pérsico / Ormuz"),         # ~9
    ("30,31,32,33", "Canal de Suez"),                 # ~2
    ("51,3,53,5", "Roterdã / Norte"),                 # ~4
    ("-34,18,-33,19", "Cidade do Cabo"),              # ~1
    ("22,113,23,115", "Hong Kong / Pearl River"),     # ~2
    ("51,-5,56,2", "Mar do Norte / UK"),
    ("36,-6,44,5", "Mediterrâneo Oeste"),
    ("-5,105,5,120", "Java Sea / Indonésia"),
    ("30,120,40,140", "Mar da China / Japão"),
    ("-40,145,-33,155", "SE Austrália"),
    ("18,-80,27,-65", "Caribe"),
    ("48,-125,50,-122", "Vancouver / Seattle"),
    ("29,-96,30,-94", "Golfo do México / Houston"),
    # --- Acrescentados: America do Sul, Africa, Asia e Pacifico. As
    # fontes AIS gratuitas sao costeiras (ver docstring do modulo),
    # entao a unica forma de aparecer navio perto do Brasil e' pedir a
    # bbox do Brasil explicitamente. Cada bbox roda em paralelo, entao
    # acrescentar mais nao deixa a camada mais lenta.
    ("-25,-47,-22,-42", "Santos / Rio de Janeiro (BR)"),
    ("-21,-41,-19,-38", "Vitória / Espírito Santo (BR)"),
    ("-14,-39,-11,-37", "Salvador / Bahia (BR)"),
    ("-9,-36,-7,-34", "Recife / Suape (BR)"),
    ("-3,-49,-1,-47", "Belém / foz do Amazonas (BR)"),
    ("-33,-54,-31,-51", "Rio Grande / Sul (BR)"),
    ("-36,-58,-34,-55", "Rio da Prata (AR/UY)"),
    ("-34,-73,-32,-71", "Valparaíso (CL)"),
    ("8,-80,10,-78", "Canal do Panamá (Caribe)"),
    ("7,-80,9,-78", "Canal do Panamá (Pacífico)"),
    ("33,-119,34,-117", "Los Angeles / Long Beach (US)"),
    ("35,-6,37,-4", "Estreito de Gibraltar"),
    ("40,28,42,30", "Bósforo / Mármara"),
    ("5,2,7,5", "Golfo da Guiné / Lagos"),
    ("-31,30,-29,32", "Durban (ZA)"),
    ("18,72,20,74", "Mumbai (IN)"),
    ("-38,174,-36,176", "Auckland (NZ)"),
)

MAX_SHIPS = None  # sem limite de plotagem

_SHIP_TYPE_NAMES = {
    0: "Desconhecido",
    20: "Wing in ground",
    30: "Pesca",
    31: "Rebocador",
    32: "Rebocador",
    33: "Dragagem",
    34: "Mergulho",
    35: "Militar",
    36: "Veleiro",
    37: "Lazer",
    40: "Alta velocidade",
    50: "Piloto",
    51: "SAR",
    52: "Rebocador",
    53: "Portuário",
    54: "Antipoluição",
    55: "Aplicação da lei",
    58: "Médico",
    59: "Especial",
    60: "Passageiros",
    70: "Carga",
    71: "Carga (perigosa)",
    72: "Carga (perigosa)",
    73: "Carga (perigosa)",
    74: "Carga (perigosa)",
    80: "Petroleiro",
    81: "Petroleiro (perigoso)",
    82: "Petroleiro (perigoso)",
    83: "Petroleiro (perigoso)",
    84: "Petroleiro (perigoso)",
    90: "Outro",
}

_cache = {"timestamp": 0, "data": None, "source": "demo"}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 60


def _clean_speed(kn):
    """AIS usa 102.3 / 1023 como 'nao disponivel'; limita a valores realistas."""
    try:
        kn = float(kn or 0)
    except (TypeError, ValueError):
        return 0.0
    if kn < 0 or kn > 50:  # >50 kn e extremo ate para ferries rapidos
        return 0.0
    return kn


def _ship_type_label(code):
    if code is None:
        return "Desconhecido"
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code)
    if code in _SHIP_TYPE_NAMES:
        return _SHIP_TYPE_NAMES[code]
    base = (code // 10) * 10
    return _SHIP_TYPE_NAMES.get(base, f"Tipo {code}")


def _fetch_digitraffic():
    loc_resp = requests.get(DIGITRAFFIC_LOC, headers=DEFAULT_HEADERS, timeout=8)
    loc_resp.raise_for_status()
    loc = loc_resp.json()
    features = loc.get("features") or []

    meta = {}
    try:
        ves_resp = requests.get(DIGITRAFFIC_VES, headers=DEFAULT_HEADERS, timeout=6)
        if ves_resp.ok:
            for v in ves_resp.json() or []:
                mmsi = v.get("mmsi")
                if mmsi is not None:
                    meta[int(mmsi)] = v
    except Exception:
        pass

    ships = []
    for f in features:
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        mmsi = props.get("mmsi") or f.get("mmsi")
        if mmsi is None:
            continue
        mmsi = int(mmsi)
        info = meta.get(mmsi) or {}
        sog = _clean_speed(props.get("sog"))
        ships.append({
            "id": f"mmsi-{mmsi}",
            "mmsi": mmsi,
            "name": (info.get("name") or "").strip() or f"MMSI {mmsi}",
            "lat": lat,
            "lon": lon,
            "speed_kn": sog,
            "speed_kmh": round(sog * 1.852, 1),
            "course": props.get("cog") or props.get("heading") or 0,
            "heading": props.get("heading") or props.get("cog") or 0,
            "ship_type": _ship_type_label(info.get("shipType")),
            "ship_type_code": info.get("shipType"),
            "destination": (info.get("destination") or "").strip() or None,
            "imo": info.get("imo") or None,
            "callsign": (info.get("callSign") or "").strip() or None,
            "flag": None,
            "source_region": "Báltico / Fintraffic",
        })
    if not ships:
        raise ValueError("Digitraffic vazio")
    return ships


def _fetch_kystverket():
    resp = requests.get(KYST_GEOJSON, headers=DEFAULT_HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features") or []
    ships = []
    for f in features:
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        # LineString: last point is most recent
        if geom.get("type") == "LineString" and coords:
            lon, lat = coords[-1][0], coords[-1][1]
        elif geom.get("type") == "Point" and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
        else:
            continue
        mmsi = props.get("mmsi")
        if mmsi is None:
            continue
        mmsi = int(mmsi)
        speed = _clean_speed(props.get("speed"))
        ships.append({
            "id": f"mmsi-{mmsi}",
            "mmsi": mmsi,
            "name": (props.get("name") or "").strip() or f"MMSI {mmsi}",
            "lat": lat,
            "lon": lon,
            "speed_kn": speed,
            "speed_kmh": round(speed * 1.852, 1),
            "course": props.get("cog") or 0,
            "heading": props.get("cog") or 0,
            "ship_type": _ship_type_label(props.get("ship_type") or props.get("shipType")),
            "ship_type_code": props.get("ship_type") or props.get("shipType"),
            "destination": (props.get("destination") or "").strip() or None,
            "imo": props.get("imo") or None,
            "callsign": (props.get("callsign") or props.get("callSign") or "").strip() or None,
            "flag": None,
            "source_region": "Noruega / Kystverket",
        })
    if not ships:
        raise ValueError("Kystverket vazio")
    return ships


def _fetch_hormuz():
    resp = requests.get(HORMUZ_SHIPS, headers=DEFAULT_HEADERS, timeout=8)
    resp.raise_for_status()
    raw = resp.json()
    if not isinstance(raw, list):
        raise ValueError("Hormuz formato inesperado")
    ships = []
    for s in raw:
        lat, lon = s.get("latitude"), s.get("longitude")
        if lat is None or lon is None:
            continue
        mmsi = s.get("mmsi")
        if mmsi is None:
            continue
        try:
            mmsi = int(mmsi)
        except (TypeError, ValueError):
            continue
        speed = _clean_speed(s.get("speed"))
        ships.append({
            "id": f"mmsi-{mmsi}",
            "mmsi": mmsi,
            "name": (s.get("name") or "").strip() or f"MMSI {mmsi}",
            "lat": lat,
            "lon": lon,
            "speed_kn": speed,
            "speed_kmh": round(speed * 1.852, 1),
            "course": s.get("course") or 0,
            "heading": s.get("course") or 0,
            "ship_type": s.get("ship_category") or "Desconhecido",
            "ship_type_code": None,
            "destination": (s.get("destination") or "").strip() or None,
            "imo": None,
            "callsign": None,
            "flag": s.get("flag"),
            "source_region": "Golfo Pérsico / Ormuz",
        })
    if not ships:
        raise ValueError("Hormuz vazio")
    return ships


def _fetch_openwaters_bbox(bbox, region_label):
    """Busca uma única bbox do Open Waters. Roda em paralelo (ver
    _fetch_openwaters) — antes cada bbox era buscada em sequência, o
    que somava o tempo de TODAS as 18 chamadas (podendo levar minutos
    se alguma travar até o timeout) para retornar UMA camada."""
    resp = requests.get(
        OPENWATERS_VESSELS,
        params={"bbox": bbox},
        headers=DEFAULT_HEADERS,
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features") or []
    out = {}
    for f in features:
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        mmsi = props.get("mmsi") or f.get("id")
        if mmsi is None:
            continue
        try:
            mmsi = int(mmsi)
        except (TypeError, ValueError):
            continue
        speed = _clean_speed(props.get("sog"))
        src = props.get("source") or "openwaters"
        out[mmsi] = {
            "id": f"mmsi-{mmsi}",
            "mmsi": mmsi,
            "name": (props.get("name") or "").strip() or f"MMSI {mmsi}",
            "lat": lat,
            "lon": lon,
            "speed_kn": speed,
            "speed_kmh": round(speed * 1.852, 1),
            "course": props.get("cog") or 0,
            "heading": props.get("heading") or props.get("cog") or 0,
            "ship_type": _ship_type_label(props.get("type")),
            "ship_type_code": props.get("type"),
            "destination": None,
            "imo": None,
            "callsign": None,
            "flag": None,
            "source_region": f"OpenWaters ({region_label} / {src})",
        }
    return out


def _fetch_openwaters():
    """Agrega várias bboxes de alta densidade via ais.openwaters.io (sem chave).
    As bboxes são buscadas EM PARALELO (ThreadPoolExecutor) — antes eram
    18 chamadas HTTP em sequência, cada uma podendo levar até 12s até
    dar timeout, somando minutos no pior caso para uma única camada."""
    combined = {}
    any_ok = False
    with ThreadPoolExecutor(max_workers=len(_OPENWATERS_BBOXES)) as ex:
        futs = {
            ex.submit(_fetch_openwaters_bbox, bbox, region_label): (bbox, region_label)
            for bbox, region_label in _OPENWATERS_BBOXES
        }
        for fut in as_completed(futs):
            try:
                out = fut.result()
                combined.update(out)
                any_ok = True
            except Exception:
                continue
    if not any_ok or not combined:
        raise ValueError("OpenWaters vazio ou inacessível")
    return list(combined.values())



_NDBC_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{3,9}$")


def _fetch_ndbc_vos():
    """
    NOAA/NDBC - navios voluntarios (VOS) que mandam boletim
    meteorologico de onde estao, inclusive no MEIO do Atlantico e do
    Pacifico. E' a unica fonte deste arquivo que nao depende de antena
    AIS em terra (ver docstring do modulo).

    A pagina devolve HTML com uma tabela de texto fixo dentro de <pre>:

        SHIP    LAT    LON  YYYY MM DD hh mm  WDIR WSPD ...
        ZCEF6   38.2  -48.7 2026 09 16 00 00   270  12 ...

    O parser e' proposital-mente tolerante: pega a 1a coluna como
    indicativo de chamada e as duas seguintes como lat/lon, e ignora
    qualquer linha que nao encaixe (cabecalho, rodape, mudanca de
    layout). Se a NOAA mudar o formato, esta fonte simplesmente some -
    nao derruba as outras.

    Limitacoes honestas: VOS nao tem MMSI, rumo nem velocidade, entao
    esses navios entram com speed/course zerados e id "vos-<callsign>".
    """
    resp = requests.get(
        NDBC_SHIP_OBS_URL,
        params={"uom": "M", "time": "2"},  # metrico, ultimas 2 horas
        headers=DEFAULT_HEADERS,
        timeout=12,
    )
    resp.raise_for_status()
    text = resp.text

    block = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.S | re.I)
    body = block.group(1) if block else text
    body = re.sub(r"<[^>]+>", " ", body)

    ships = {}
    for raw_line in body.splitlines():
        parts = raw_line.split()
        if len(parts) < 5:
            continue
        call = parts[0].strip().upper()
        if not _NDBC_CALLSIGN_RE.match(call):
            continue
        try:
            lat = float(parts[1])
            lon = float(parts[2])
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        if lat == 0 and lon == 0:
            continue
        # Mesma embarcacao pode reportar varias vezes em 2h; a primeira
        # linha da pagina e' a mais recente.
        if call in ships:
            continue
        ships[call] = {
            "id": f"vos-{call}",
            "mmsi": None,
            "name": f"Navio {call}",
            "lat": lat,
            "lon": lon,
            "speed_kn": 0.0,
            "speed_kmh": 0.0,
            "course": 0,
            "heading": 0,
            "ship_type": "Navio observador (VOS)",
            "ship_type_code": None,
            "destination": None,
            "imo": None,
            "callsign": call,
            "flag": None,
            "source_region": "VOS / NOAA (oceano aberto)",
        }

    if not ships:
        raise ValueError("NDBC VOS vazio ou layout mudou")
    return list(ships.values())


def _fetch_ifremer():
    """Frota oceanográfica francesa (IFREMER) — poucos navios, cobertura oceânica real."""
    resp = requests.get(IFREMER_POSITIONS, headers=DEFAULT_HEADERS, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features") if isinstance(data, dict) else data
    if not isinstance(features, list):
        raise ValueError("IFREMER formato inesperado")
    ships = []
    for f in features:
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        vessel = props.get("vessel") or {}
        name = (vessel.get("name") or vessel.get("id") or "IFREMER").strip()
        vid = vessel.get("id") or name
        ships.append({
            "id": f"ifremer-{vid}",
            "mmsi": None,
            "name": name,
            "lat": lat,
            "lon": lon,
            "speed_kn": 0.0,
            "speed_kmh": 0.0,
            "course": 0,
            "heading": 0,
            "ship_type": "Pesquisa / Oceanográfico",
            "ship_type_code": None,
            "destination": None,
            "imo": None,
            "callsign": None,
            "flag": "FR",
            "source_region": "IFREMER (frota FR)",
        })
    if not ships:
        raise ValueError("IFREMER vazio")
    return ships

# ---------- AISstream.io (opcional, cobertura global, exige API key) ----------

_aisstream_lock = threading.Lock()
_aisstream_ships = {}  # mmsi -> {"data": {...}, "seen": timestamp}
_aisstream_thread_started = False


def _aisstream_on_message(_ws, message):
    try:
        msg = json.loads(message)
    except Exception:
        return
    if msg.get("MessageType") != "PositionReport":
        return
    meta = msg.get("MetaData") or {}
    report = (msg.get("Message") or {}).get("PositionReport") or {}
    mmsi = meta.get("MMSI")
    lat = meta.get("latitude")
    lon = meta.get("longitude")
    if mmsi is None or lat is None or lon is None:
        return
    try:
        mmsi = int(mmsi)
    except (TypeError, ValueError):
        return
    sog = _clean_speed(report.get("Sog"))
    ship = {
        "id": f"mmsi-{mmsi}",
        "mmsi": mmsi,
        "name": (meta.get("ShipName") or "").strip() or f"MMSI {mmsi}",
        "lat": lat,
        "lon": lon,
        "speed_kn": sog,
        "speed_kmh": round(sog * 1.852, 1),
        "course": report.get("Cog") or report.get("TrueHeading") or 0,
        "heading": report.get("TrueHeading") or report.get("Cog") or 0,
        "ship_type": "Desconhecido",
        "ship_type_code": None,
        "destination": None,
        "imo": None,
        "callsign": None,
        "flag": None,
        "source_region": "AISstream.io (global)",
    }
    with _aisstream_lock:
        _aisstream_ships[mmsi] = {"data": ship, "seen": time.time()}


def _aisstream_on_open(ws):
    ws.send(json.dumps({
        "APIKey": AISSTREAM_API_KEY,
        "BoundingBoxes": [[[-90, -180], [90, 180]]],
        "FilterMessageTypes": ["PositionReport"],
    }))


def _aisstream_worker():
    """Thread de fundo: conecta, escuta, reconecta com backoff se cair.
    Qualquer erro fica contido aqui - nunca derruba o processo Flask."""
    backoff = 5
    while True:
        try:
            ws = websocket.WebSocketApp(
                AISSTREAM_WS_URL,
                on_open=_aisstream_on_open,
                on_message=_aisstream_on_message,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            pass
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def _ensure_aisstream_started():
    global _aisstream_thread_started
    if _aisstream_thread_started or not AISSTREAM_API_KEY or not _HAS_WEBSOCKET:
        return
    with _aisstream_lock:
        if _aisstream_thread_started:
            return
        threading.Thread(target=_aisstream_worker, daemon=True).start()
        _aisstream_thread_started = True


def _get_aisstream_ships():
    if not AISSTREAM_API_KEY or not _HAS_WEBSOCKET:
        return []
    _ensure_aisstream_started()
    now = time.time()
    with _aisstream_lock:
        return [v["data"] for v in _aisstream_ships.values() if (now - v["seen"]) < _AISSTREAM_MAX_AGE_SECONDS]


def _generate_demo_ships(count=80):
    import random
    names = ["ATLANTIC STAR", "PACIFIC GLORY", "NORDIC WAVE", "CORAL QUEEN", "AZURE TRADER"]
    types = ["Carga", "Petroleiro", "Passageiros", "Pesca", "Rebocador"]
    regions = [
        (59.0, 10.0), (60.5, 22.0), (25.5, 55.0), (1.3, 103.8),
        (35.0, 139.0), (-23.0, -43.0), (40.7, -74.0), (51.5, 1.0),
    ]
    ships = []
    for i in range(count):
        base_lat, base_lon = random.choice(regions)
        ships.append({
            "id": f"demo-ship-{i}",
            "mmsi": 200000000 + i,
            "name": f"{random.choice(names)} {i}",
            "lat": base_lat + random.uniform(-3, 3),
            "lon": base_lon + random.uniform(-5, 5),
            "speed_kn": random.uniform(0, 18),
            "speed_kmh": round(random.uniform(0, 33), 1),
            "course": random.uniform(0, 360),
            "heading": random.uniform(0, 360),
            "ship_type": random.choice(types),
            "ship_type_code": None,
            "destination": None,
            "imo": None,
            "callsign": None,
            "flag": None,
            "source_region": "DEMO",
        })
    return ships


def get_ships():
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["timestamp"]) < _CACHE_TTL_SECONDS:
            return (
                _cache["data"],
                _cache["source"],
                _cache.get("total_available", len(_cache["data"])),
            )

    combined = {}
    sources_ok = []

    # Todas as fontes REST em paralelo — antes rodavam uma apos a outra
    # (soma de todos os timeouts no pior caso), o que fazia o 1º clique
    # na camada de navios demorar bem mais que aeronaves/satelites/
    # cameras (que ja usam ThreadPoolExecutor). Agora o tempo total e
    # aproximadamente o da fonte mais lenta, nao a soma de todas.
    fetchers = (
        ("digitraffic", _fetch_digitraffic),
        ("kystverket", _fetch_kystverket),
        ("hormuz", _fetch_hormuz),
        ("openwaters", _fetch_openwaters),
        ("ndbc_vos", _fetch_ndbc_vos),
        ("ifremer", _fetch_ifremer),
    )
    # Quantos navios cada fonte devolveu (e o erro, se falhou). Serve pro
    # /api/diagnostics/ e pro /api/ships/: sem isso nao da' pra saber se
    # "poucos navios" e' fonte fora do ar ou apenas a cobertura real
    # daquela fonte.
    counts = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=len(fetchers)) as ex:
        futs = {ex.submit(fetcher): name for name, fetcher in fetchers}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                batch = fut.result()
                for s in batch:
                    key = s.get("mmsi") if s.get("mmsi") is not None else s.get("id")
                    if key is not None:
                        combined[key] = s
                counts[name] = len(batch)
                sources_ok.append(name)
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"[:200]
                counts[name] = 0
                continue

    # AISstream (global) so entra se AISSTREAM_API_KEY estiver configurada;
    # roda em thread de fundo continua, entao aqui e so leitura do cache.
    aisstream_ships = _get_aisstream_ships()
    counts["aisstream"] = len(aisstream_ships)
    if not AISSTREAM_API_KEY:
        errors["aisstream"] = "AISSTREAM_API_KEY nao configurada (cadastro gratis em aisstream.io)"
    elif not _HAS_WEBSOCKET:
        errors["aisstream"] = "pacote websocket-client nao instalado"
    if aisstream_ships:
        for s in aisstream_ships:
            combined[s["mmsi"]] = s
        sources_ok.append("aisstream")

    if combined:
        ships = list(combined.values())
        total_available = len(ships)
        # Ordem estavel independente da ordem de chegada das threads.
        order = ["digitraffic", "kystverket", "hormuz", "openwaters", "ndbc_vos", "ifremer", "aisstream"]
        source = "+".join(t for t in order if t in sources_ok)
        is_live_source = source
    else:
        ships = _generate_demo_ships()
        total_available = len(ships)
        source = "demo"
        is_live_source = "demo"

    with _cache_lock:
        _cache["data"] = ships
        _cache["source"] = source
        _cache["timestamp"] = now
        _cache["total_available"] = total_available
        _cache["source_counts"] = counts
        _cache["source_errors"] = errors

    return ships, source, total_available


def get_ship_source_counts():
    """Quantos navios vieram de cada fonte na ultima coleta, e o erro de
    quem falhou. Usado por /api/ships/ e /api/diagnostics/."""
    with _cache_lock:
        return {
            "counts": dict(_cache.get("source_counts") or {}),
            "errors": dict(_cache.get("source_errors") or {}),
        }
