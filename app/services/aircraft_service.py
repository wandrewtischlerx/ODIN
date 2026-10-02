"""
Servico de aeronaves em tempo real.

BUG CORRIGIDO (bugfix 2026-09): antes, o fallback para OpenSky so era
acionado se a busca inteira falhasse (combined vazio). Como /mil e /ladd
(globais) quase sempre retornam alguma coisa, o dict "combined" nunca
ficava vazio mesmo quando a varredura civil por hubs falhava inteira
(rate-limit do host unico) - entao o resultado ficava dominado por
aeronaves militares sem nunca cair no fallback civil. Agora: (1) usamos
DOIS espelhos comunitarios independentes (adsb.lol e adsb.fi) para cada
hub, dobrando a chance de sucesso por regiao sem dobrar carga em nenhum
host; (2) o OpenSky Network e sempre tentado tambem (best-effort) e
mesclado, nao so usado como ultimo recurso; (3) o "source" reportado
agora e composto (ex.: "adsb_lol+adsb_fi+opensky"), refletindo de fato
o que contribuiu.

Fontes (todas publicas, sem chave):
  1) adsb.lol  (https://api.adsb.lol/v2)      — /mil, /ladd, /point/.../...
  2) adsb.fi   (https://opendata.adsb.fi/api) — /v2/mil, /v2/lat/.../lon/.../dist/...
     (mesma rede de feeders comunitarios ADS-B, infraestrutura separada
     de adsb.lol; compativel com o formato ADSBExchange v2)
  3) airplanes.live (https://api.airplanes.live/v2) — /v2/mil,
     /v2/point/.../.../... — 3o espelho comunitario independente, mesmo
     formato ADSBExchange-compativel, infraestrutura separada dos dois
     acima. Adicionado para aumentar a cobertura militar (mais um /mil
     global) e civil por hub (mais uma chance de nao cair em rate-limit).
  4) OpenSky Network (https://opensky-network.org) — snapshot global
     unico, usado sempre em paralelo (best-effort) para preencher
     buracos de cobertura civil dos hubs

Se todas falharem → demo.
"""

import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from .http_headers import DEFAULT_HEADERS

ADSB_LOL_BASE = "https://api.adsb.lol/v2"
ADSB_FI_BASE = "https://opendata.adsb.fi/api/v2"
AIRPLANES_LIVE_BASE = "https://api.airplanes.live/v2"
OPEN_SKY_URL = "https://opensky-network.org/api/states/all"

# Hubs de alto trafego civil (aeroportos / corredores). Raio max 250 nm.
# Ampliado para cobrir America do Sul, America do Norte (leste/oeste do
# Canada e Mexico), Africa e mais da Asia - a lista antiga deixava
# regioes inteiras sem nenhum ponto de amostragem civil.
HUB_POINTS = [
    # América do Norte (densidade alta)
    (40.7, -74.0), (34.0, -118.2), (41.9, -87.6), (33.7, -84.4),
    (29.9, -95.4), (25.8, -80.2), (37.6, -122.4), (47.6, -122.3),
    (43.7, -79.6), (19.4, -99.1), (45.5, -73.6), (49.2, -123.1),
    (32.9, -97.0), (39.9, -75.2), (38.9, -77.0), (42.4, -71.0),
    (33.4, -112.0), (36.1, -115.2), (35.2, -80.9), (30.3, -97.7),
    (44.9, -93.2), (39.8, -104.9), (45.6, -122.6), (61.2, -149.9),
    # Caribe / ilhas Atlântico / Pacífico
    (18.4, -66.1), (25.0, -77.3), (12.1, -68.9), (21.3, -157.9),
    (64.1, -21.9), (28.5, -16.3), (32.3, -64.8), (-17.6, -149.6),
    (18.4, -69.9), (13.1, -59.5),
    # Europa (mais hubs = mais civil)
    (51.5, -0.1), (48.8, 2.3), (50.1, 8.7), (52.5, 13.4),
    (41.9, 12.5), (52.4, 4.9), (40.4, -3.7), (55.8, 37.6),
    (59.6, 17.9), (60.2, 24.9), (38.7, -9.1), (53.3, -6.2),
    (48.1, 11.6), (45.5, 9.2), (41.3, 2.1), (50.4, 30.5),
    (47.5, 19.0), (44.5, 26.1), (35.3, 25.1), (37.9, 23.7),
    (55.6, 12.6), (59.9, 10.7), (47.3, 8.5), (50.0, 14.5),
    (51.1, 17.0), (54.7, 25.3), (56.9, 24.1), (41.7, 44.8),
    # Ásia / Oriente Médio
    (35.7, 139.7), (31.2, 121.5), (22.3, 114.2), (1.3, 103.8),
    (28.6, 77.2), (25.2, 55.3), (13.7, 100.5), (37.5, 127.0),
    (41.0, 28.9), (24.9, 46.7), (19.1, 72.9), (3.1, 101.7),
    (14.6, 121.0), (-6.1, 106.8), (35.2, 136.9), (39.9, 116.4),
    (22.6, 113.9), (25.0, 121.5), (23.7, 90.4), (33.7, 73.0),
    (24.7, 46.8), (21.7, 39.2), (32.1, 34.8), (35.0, 135.8),
    (43.1, 131.9), (55.0, 82.9), (56.8, 60.6),
    # América do Sul (Brasil prioritário + vizinhos)
    (-23.5, -46.6), (-22.9, -43.2), (-15.8, -47.9), (-12.9, -38.5),
    (-3.0, -60.0), (-8.1, -34.9), (-30.0, -51.2), (-25.5, -49.2),
    (-16.7, -49.3), (-3.7, -38.5), (-1.4, -48.5), (-9.9, -67.8),
    (-34.6, -58.4), (-33.4, -70.7), (-12.0, -77.0), (4.7, -74.1),
    (-0.2, -78.5), (-25.3, -57.6), (-17.8, -63.2), (10.5, -66.9),
    (-34.8, -56.2), (-33.0, -71.5), (-23.4, -70.6),
    # Interior do Brasil (raio de 250nm cobre bem menos area por ponto
    # perto do equador/tropicos do que parece no mapa - sem esses pontos
    # intermediarios, faixas inteiras do interior ficavam sem nenhum
    # ponto de amostragem entre as capitais acima).
    (-15.6, -56.1),   # Cuiabá/MT
    (-20.5, -54.6),   # Campo Grande/MS
    (-10.2, -48.3),   # Palmas/TO
    (-19.9, -43.9),   # Belo Horizonte/MG (faltava - so tinha via BR-040)
    (-21.2, -47.8),   # Ribeirão Preto/SP (interior paulista)
    (-23.3, -51.2),   # Londrina/PR
    (-19.8, -40.3),   # Vitória/ES
    (-5.8, -35.2),    # Natal/RN
    (-7.1, -34.9),    # João Pessoa/PB
    (-9.4, -40.5),    # Petrolina/PE (semiárido/interior NE)
    (-2.5, -44.3),    # São Luís/MA
    (-5.1, -42.8),    # Teresina/PI
    (-27.6, -48.5),   # Florianópolis/SC
    (-19.1, -57.6),   # Corumbá/MS (fronteira Bolívia/Pantanal)
    (-8.8, -63.9),    # Porto Velho/RO
    (2.8, -60.7),     # Boa Vista/RR
    (-1.5, -48.4),    # Belém/PA (já coberto acima, mantido p/ densidade)
    (-16.3, -52.3),   # Rio Verde/GO (agro, tráfego regional denso)
    # África / Oceania
    (-26.1, 28.2), (30.1, 31.4), (6.5, 3.4), (-1.3, 36.8),
    (-33.9, 18.4), (33.9, -6.9), (36.8, 10.2), (14.7, -17.5),
    (-33.9, 151.2), (-27.4, 153.1), (-37.7, 144.8), (-31.9, 115.9),
    (-36.8, 174.8), (-41.3, 174.8), (-6.3, 106.8),
    # Corredores oceânicos (NAT / Atlântico / Pacífico)
    (45.0, -40.0), (50.0, -30.0), (55.0, -20.0), (40.0, -50.0),
    (35.0, -45.0), (30.0, -40.0), (25.0, -35.0), (20.0, -30.0),
    (15.0, -45.0), (10.0, -30.0), (0.0, -30.0), (-10.0, -20.0),
    (-20.0, -10.0), (-30.0, 0.0), (5.0, -15.0), (48.0, -15.0),
    (20.0, -160.0), (25.0, -170.0), (30.0, 170.0), (10.0, -150.0),
    (-15.0, -150.0), (0.0, -160.0), (35.0, -150.0),
]

RADIUS_NM = 250
# Antes 2000: com todos os hubs + OpenSky o volume real passa disso
# em horários de pico; o Cesium aguenta billboards 2D em maior quantidade.
MAX_AIRCRAFT = None  # sem limite de plotagem
# Cache curto: front atualiza com mais frequencia; dead-reckoning local
# so e confiavel por poucos segundos com heading/speed ADS-B.
_CACHE_TTL_SECONDS = 20
# Ate quando uma aeronave que NAO veio na varredura mais recente ainda
# fica no mapa, usando a ultima posicao/dado conhecidos (ver bugfix em
# get_aircraft() logo abaixo). Generoso o bastante pra cobrir 2-3 ciclos
# de rate-limit passageiro num host, curto o bastante pra nao deixar
# aviao "fantasma" parado num lugar onde ele ja nao esta mais.
_STALE_KEEP_SECONDS = 75

_cache = {"timestamp": 0, "data": None, "source": "demo"}
# id -> (feature, ultima_vez_que_veio_de_uma_fonte_real_epoch)
_last_seen = {}
_cache_lock = threading.Lock()

_session = requests.Session()
_session.headers.update(DEFAULT_HEADERS)

_AIRLINE_PREFIXES = ["WTX", "AAL", "UAL", "DLH", "AFR", "BAW", "TAM", "GLO", "AZU"]


# Categoria de emissor ADS-B (campo "category", padrao DO-260B/ADSBExchange):
# A7 = rotorcraft/helicoptero. E o unico jeito confiavel de saber que um
# contato e um helicoptero (nao da pra adivinhar por velocidade/altitude -
# um heliponto no solo ou um heli parado tem gs=0 igual avioes parados).
_HELICOPTER_CATEGORIES = {"A7"}
# Prefixos de designador de tipo ICAO (campo "t") conhecidos como
# helicoptero, usados so como fallback quando a fonte nao manda "category"
# (acontece com frequencia no /mil - militar raramente reporta categoria
# ADS-B "civil" mesmo sendo um helicoptero de verdade).
_HELICOPTER_TYPE_PREFIXES = (
    "H60", "UH60", "AH64", "CH47", "MI8", "MI17", "MI24", "MI28", "KA52",
    "EC35", "EC45", "EC30", "EC20", "EC13", "AS35", "AS50", "AS65",
    "A109", "A139", "A169", "A189", "B06", "B407", "B412", "B429", "B430",
    "R22", "R44", "R66", "S76", "S92", "H125", "H130", "H145", "H160",
    "SQ36",  # Esquilo/Squirrel
)


def _looks_like_helicopter(ac, category):
    if category in _HELICOPTER_CATEGORIES:
        return True
    type_code = (ac.get("t") or "").strip().upper()
    if type_code and type_code.startswith(_HELICOPTER_TYPE_PREFIXES):
        return True
    desc = (ac.get("desc") or "").upper()
    if "HELICOPTER" in desc or "ROTORCRAFT" in desc:
        return True
    return False


def _adsbx_style_to_feature(ac, is_military_confirmed=False):
    hex_id = ac.get("hex")
    lat, lon = ac.get("lat"), ac.get("lon")
    if not hex_id or lat is None or lon is None:
        return None

    alt_baro = ac.get("alt_baro")
    on_ground = alt_baro == "ground"
    altitude_ft = 0 if on_ground else (alt_baro if isinstance(alt_baro, (int, float)) else 0)
    ground_speed_kt = ac.get("gs") or 0

    db_flags = ac.get("dbFlags") or 0
    if isinstance(db_flags, int) and (db_flags & 1):
        is_military_confirmed = True

    category = (ac.get("category") or "").strip().upper() or None

    return {
        "id": hex_id,
        "callsign": (ac.get("flight") or hex_id).strip().upper() or hex_id.upper(),
        "lat": lat,
        "lon": lon,
        "altitude_m": round(altitude_ft * 0.3048) if isinstance(altitude_ft, (int, float)) else 0,
        "altitude_ft": round(altitude_ft) if isinstance(altitude_ft, (int, float)) else 0,
        "velocity_ms": round(ground_speed_kt * 0.514444, 1),
        "speed_kmh": round(ground_speed_kt * 1.852),
        # track (COG) e o correto para dead-reckoning; true/mag heading
        # so como fallback — heading 0 falsificado fazia avioes "voarem leste".
        "heading": ac.get("track")
        if ac.get("track") is not None
        else (ac.get("true_heading") if ac.get("true_heading") is not None else ac.get("mag_heading")),
        "on_ground": on_ground,
        "origin_country": None,
        "vertical_rate": ac.get("baro_rate") or ac.get("geom_rate"),
        "is_military_confirmed": bool(is_military_confirmed),
        "category": category,
        "aircraft_type": (ac.get("t") or None),
        "is_helicopter": _looks_like_helicopter(ac, category),
    }


def _get_json(base, path, timeout=5, retries=1):
    """Timeouts curtos: a UI precisa responder rápido no primeiro clique."""
    url = f"{base}{path}"
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = _session.get(url, timeout=timeout)
            if resp.status_code == 429:
                # rate limit — uma espera curta e tenta de novo
                time.sleep(1.0 + attempt)
                last_err = RuntimeError("HTTP 429")
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.3)
    raise last_err or RuntimeError(f"falha ao consultar {url}")


def _fetch_point_adsb_lol(lat, lon):
    payload = _get_json(ADSB_LOL_BASE, f"/point/{lat}/{lon}/{RADIUS_NM}")
    return payload.get("ac") or []


def _fetch_point_adsb_fi(lat, lon):
    payload = _get_json(ADSB_FI_BASE, f"/lat/{lat}/lon/{lon}/dist/{RADIUS_NM}")
    return payload.get("ac") or []


def _fetch_point_airplanes_live(lat, lon):
    # airplanes.live e' um 3o espelho comunitario independente (mesmo
    # formato ADSBExchange-compativel de adsb.lol/adsb.fi, infra
    # separada) — mais uma chance de cobrir a regiao se os outros dois
    # estiverem com rate-limit ou fora do ar no momento.
    payload = _get_json(AIRPLANES_LIVE_BASE, f"/point/{lat}/{lon}/{RADIUS_NM}")
    return payload.get("ac") or []


def _fetch_point_any_host(lat_lon):
    """
    Tenta o mesmo hub em TRES espelhos comunitarios independentes
    (adsb.lol, adsb.fi e airplanes.live) EM PARALELO e mescla o que vier.

    BUGFIX (2026-09): antes os tres eram tentados em SEQUENCIA (um so
    comeca depois que o anterior termina ou estoura o timeout). No pior
    caso (o primeiro host lento/com rate-limit) isso somava ate ~15s so
    para UM hub, e o hub inteiro tinha grande chance de estourar o
    timeout do future que o chama (fut.result(timeout=9) em
    _fetch_community_adsb_hubs_only) - jogando fora TODOS os avioes
    daquele hub, nao so os do host lento. Agora os tres saem ao mesmo
    tempo: o pior caso passa a ser o timeout de UM host (~5-6s), nao a
    soma dos tres, e um host lento nao derruba os outros dois.

    Retorna (features, hosts_que_responderam).
    """
    lat, lon = lat_lon
    combined = {}
    hosts_ok = set()
    fetchers = (
        (_fetch_point_adsb_lol, "adsb_lol"),
        (_fetch_point_adsb_fi, "adsb_fi"),
        (_fetch_point_airplanes_live, "airplanes_live"),
    )
    with ThreadPoolExecutor(max_workers=len(fetchers)) as ex:
        futs = {ex.submit(fetcher, lat, lon): tag for fetcher, tag in fetchers}
        for fut in as_completed(futs):
            tag = futs[fut]
            try:
                acs = fut.result(timeout=6)
            except Exception:
                continue
            got_any = False
            for ac in acs:
                feature = _adsbx_style_to_feature(ac, is_military_confirmed=False)
                # BUGFIX: antes descartava toda aeronave com on_ground=True,
                # ou seja, qualquer aviao parado/taxiando no patio nunca
                # aparecia - inclusive militares, que passam boa parte do
                # tempo parados em base (justamente onde o filtro cortava
                # mais). Mantemos on_ground no dado (o front decide como
                # exibir) em vez de jogar fora a aeronave inteira.
                if feature:
                    combined[feature["id"]] = feature
                    got_any = True
            if got_any:
                hosts_ok.add(tag)
    return list(combined.values()), hosts_ok


def _fetch_community_adsb_hubs_only(max_batches=None, inter_batch_sleep=0.0):
    """Varre hubs civis em adsb.lol + adsb.fi (sem /mil — ja feito em paralelo).

    Todos os HUB_POINTS sao consultados em paralelo (workers altos).
    max_batches ainda existe por compat, mas o caminho padrao passa None
    e cobre a lista inteira — o limite antigo (3 lotes) deixava Asia,
    America do Sul e Oceania quase sem cobertura civil.
    """
    combined = {}
    contributed = set()
    points = list(HUB_POINTS)
    if max_batches is not None:
        # Compat: interpreta como "ate N*12 hubs" (antigo tamanho de lote)
        cap = max(12, int(max_batches) * 12)
        points = points[:cap]
    workers = min(24, max(8, len(points)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_point_any_host, pt): pt for pt in points}
        for fut in as_completed(futs):
            try:
                features, hosts_ok = fut.result(timeout=9) or ([], set())
            except Exception:
                features, hosts_ok = [], set()
            contributed |= hosts_ok
            for feature in features:
                if feature["id"] not in combined:
                    combined[feature["id"]] = feature
    return combined, contributed


def _fetch_community_adsb():
    """Compat: mil + hubs (usado se necessario). Preferir get_aircraft novo."""
    combined = {}
    contributed = set()
    for base, path, force_mil, tag in (
        (ADSB_LOL_BASE, "/mil", True, "adsb_lol"),
        (ADSB_FI_BASE, "/mil", True, "adsb_fi"),
        (AIRPLANES_LIVE_BASE, "/mil", True, "airplanes_live"),
        (ADSB_LOL_BASE, "/ladd", False, "adsb_lol"),
    ):
        try:
            payload = _get_json(base, path, timeout=8, retries=1)
            for ac in payload.get("ac") or []:
                feature = _adsbx_style_to_feature(ac, is_military_confirmed=force_mil)
                # BUGFIX: mesmo motivo do _fetch_point_any_host - nao
                # descarta mais aeronaves paradas no chao.
                if feature:
                    combined[feature["id"]] = feature
                    contributed.add(tag)
        except Exception:
            pass
    hub, tags = _fetch_community_adsb_hubs_only()
    for fid, f in hub.items():
        combined.setdefault(fid, f)
    contributed |= tags
    return combined, contributed


def _state_to_feature(state):
    icao24, callsign, origin_country, _, _, lon, lat, baro_alt, on_ground, \
        velocity, heading, vert_rate, *_rest = state
    if lat is None or lon is None:
        return None
    return {
        "id": icao24,
        "callsign": (callsign or "").strip() or icao24.upper(),
        "lat": lat,
        "lon": lon,
        "altitude_m": baro_alt if baro_alt is not None else 0,
        "altitude_ft": round((baro_alt or 0) * 3.28084),
        "velocity_ms": velocity if velocity is not None else 0,
        "speed_kmh": round((velocity or 0) * 3.6),
        "heading": heading,  # pode ser None — front nao extrapola sem rumo
        "on_ground": bool(on_ground),
        "origin_country": origin_country,
        "vertical_rate": vert_rate,
        "is_military_confirmed": False,
        # OpenSky (states/all publico) nao manda categoria de emissor nem
        # tipo de aeronave - sem esses dois campos nao da pra saber se e
        # um helicoptero (nao dava pra inferir por velocidade/altitude
        # sem arriscar falso positivo/negativo). Fica None em vez de
        # assumir "nao e helicoptero".
        "category": None,
        "aircraft_type": None,
        "is_helicopter": None,
    }


def _fetch_opensky():
    # OpenSky: uma unica chamada global. Timeout curto para não travar o 1º paint.
    resp = _session.get(OPEN_SKY_URL, timeout=10)
    resp.raise_for_status()
    states = (resp.json() or {}).get("states") or []
    aircraft = []
    for state in states:
        feature = _state_to_feature(state)
        # BUGFIX: idem - OpenSky tambem tinha aeronaves paradas
        # (on_ground=True) descartadas antes de chegar no globo.
        if feature:
            aircraft.append(feature)
    if not aircraft:
        raise ValueError("OpenSky vazio")
    return aircraft


def _generate_demo_aircraft(count=200):
    aircraft = []
    for i in range(count):
        prefix = random.choice(_AIRLINE_PREFIXES)
        aircraft.append({
            "id": f"demo-{i}",
            "callsign": f"{prefix}{random.randint(100, 999)}",
            "lat": random.uniform(-60, 75),
            "lon": random.uniform(-180, 180),
            "altitude_m": random.uniform(8000, 12500),
            "altitude_ft": round(random.uniform(8000, 12500) * 3.28084),
            "velocity_ms": random.uniform(180, 260),
            "speed_kmh": round(random.uniform(650, 900)),
            "heading": random.uniform(0, 360),
            "on_ground": False,
            "origin_country": "DEMO DATA",
            "vertical_rate": 0,
            "is_military_confirmed": False,
        })
    return aircraft


def get_aircraft():
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["timestamp"]) < _CACHE_TTL_SECONDS:
            return (
                _cache["data"],
                _cache["source"],
                _cache.get("total_available", len(_cache["data"])),
            )

    # Caminho rápido: OpenSky + /mil em paralelo com timeouts curtos.
    # Hubs civis entram só com poucos lotes no 1º request (não bloqueia a UI);
    # o restante preenche nos refreshes seguintes via cache.
    combined = {}
    contributed = set()

    def _job_opensky():
        return _fetch_opensky(), "opensky"

    def _job_mil():
        # BUGFIX PRINCIPAL (2026-09) - "militares somem do nada": as 4
        # chamadas abaixo eram feitas em SEQUENCIA com timeout=5 e
        # retries=0 (sem nova tentativa nem em caso de HTTP 429). Bastava
        # UMA fonte estar lenta ou com rate-limit num dado instante pra
        # atrasar/zerar a leitura militar inteira daquele ciclo - e como
        # get_aircraft() reconstroi "combined" do zero a cada chamada
        # (sem aproveitar o ciclo anterior), o resultado visivel era a
        # frota militar inteira sumindo do globo por um refresh e
        # voltando no seguinte, de forma aparentemente aleatoria.
        # Agora as 4 fontes saem em paralelo (pior caso = timeout de UMA
        # fonte, nao a soma das quatro) e com 1 retry em caso de 429,
        # igual ao resto do arquivo.
        local = {}
        tags = set()
        jobs = (
            (ADSB_LOL_BASE, "/mil", True, "adsb_lol"),
            (ADSB_FI_BASE, "/mil", True, "adsb_fi"),
            (AIRPLANES_LIVE_BASE, "/mil", True, "airplanes_live"),
            (ADSB_LOL_BASE, "/ladd", False, "adsb_lol"),
        )

        def _one(base, path, force_mil, tag):
            payload = _get_json(base, path, timeout=6, retries=1)
            return payload.get("ac") or [], force_mil, tag

        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = {ex.submit(_one, *job): job for job in jobs}
            for fut in as_completed(futs):
                try:
                    acs, force_mil, tag = fut.result(timeout=8)
                except Exception:
                    continue
                for ac in acs:
                    feature = _adsbx_style_to_feature(ac, is_military_confirmed=force_mil)
                    # BUGFIX: o endpoint /mil devolve TODA a frota militar
                    # rastreavel, incluindo a que esta parada em base/patio
                    # (a maior parte, a qualquer momento) - descartar
                    # on_ground=True aqui apagava boa parte da frota
                    # militar do globo. Mantido o campo on_ground no dado
                    # para o front rotular corretamente ("NO SOLO").
                    if feature:
                        local[feature["id"]] = feature
                        tags.add(tag)
        return list(local.values()), tags

    def _job_hubs_all():
        # Todos os hubs em paralelo (adsb.lol + adsb.fi). Antes max_batches=3
        # cortava Asia / America do Sul / Oceania e o globo ficava "vazio".
        return _fetch_community_adsb_hubs_only(max_batches=None, inter_batch_sleep=0)

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_os = ex.submit(_job_opensky)
        f_mil = ex.submit(_job_mil)
        f_hubs = ex.submit(_job_hubs_all)
        try:
            # OpenSky + mil sao a base rapida; hubs entram best-effort.
            acs, _ = f_os.result(timeout=10)
            for f in acs:
                combined[f["id"]] = f
            contributed.add("opensky")
        except Exception:
            pass
        try:
            # Timeout ajustado pra cima (era 8s): agora que _job_mil roda
            # as 4 fontes em paralelo (era sequencial) o pior caso ficou
            # mais previsivel (~8s internos), entao o timeout externo
            # precisa de uma folga em vez de cortar bem em cima.
            acs, tags = f_mil.result(timeout=10)
            for f in acs:
                combined.setdefault(f["id"], f)
            contributed |= tags
        except Exception:
            pass
        try:
            # Timeout ajustado pra cima (era 10s): o fetch por hub agora
            # tambem e paralelo internamente (adsb.lol/adsb.fi/
            # airplanes.live ao mesmo tempo por ponto), entao o tempo
            # total pra varrer todos os HUB_POINTS caiu bastante - mas
            # ainda precisa de folga pra nao descartar o lote inteiro de
            # hubs (que e a maior fonte de cobertura civil) por poucos
            # segundos de latencia de rede.
            hub_combined, hub_tags = f_hubs.result(timeout=18)
            for fid, f in hub_combined.items():
                combined.setdefault(fid, f)
            contributed |= hub_tags
        except Exception:
            pass

    if not combined and not _last_seen:
        aircraft = _generate_demo_aircraft()
        source = "demo"
        total_available = len(aircraft)
        with _cache_lock:
            _cache["data"] = aircraft
            _cache["source"] = source
            _cache["timestamp"] = now
            _cache["total_available"] = total_available
        return aircraft, source, total_available

    # BUGFIX (2026-09) - "aviões (principalmente militares) somem do
    # nada": ate aqui, cada chamada de get_aircraft() reconstruia a lista
    # do ZERO a partir so do que as fontes devolveram NESTE ciclo. Como
    # cada fonte (adsb.lol, adsb.fi, airplanes.live, OpenSky) pode falhar
    # ou dar rate-limit isoladamente por alguns segundos, uma aeronave
    # que estava no mapa podia simplesmente nao vir numa resposta e
    # desaparecer na hora - e reaparecer no ciclo seguinte, dando a
    # impressao de sumico aleatorio (mais visivel nos militares porque
    # vem de so 3 fontes /mil, contra 4 fontes pra cobertura civil).
    #
    # Agora o resultado de cada ciclo e MESCLADO com o que foi visto nos
    # ultimos _STALE_KEEP_SECONDS: uma aeronave que nao apareceu nesta
    # varredura continua no mapa (com a ultima posicao real conhecida)
    # ate ficar realmente obsoleta, em vez de sumir a cada falha pontual
    # de rede. So military e adicionado). Isso e complementar ao
    # "missingStreak" que ja existe no front (aircraft.js) - aquele
    # suaviza por poucos refreshes; este resolve o problema na raiz,
    # no backend, onde os dados sao de fato perdidos.
    with _cache_lock:
        for fid, feature in combined.items():
            _last_seen[fid] = (feature, now)
        # Remove quem ja passou do prazo de tolerancia.
        expired = [fid for fid, (_, seen_at) in _last_seen.items() if now - seen_at > _STALE_KEEP_SECONDS]
        for fid in expired:
            del _last_seen[fid]
        aircraft = [feature for feature, _ in _last_seen.values()]

    total_available = len(aircraft)
    if not combined:
        # Nenhuma fonte respondeu neste ciclo, mas ainda temos aeronaves
        # dentro da janela de tolerancia - mantem a ultima fonte conhecida
        # em vez de rotular como "demo" (o dado ainda e real, so esta
        # um pouco atrasado).
        source = _cache.get("source") or "demo"
    else:
        # Fonte composta e ordenada de forma estavel, refletindo o que
        # realmente contribuiu nesta atualizacao (ex.: "adsb_lol+opensky"
        # se o adsb.fi estiver fora do ar no momento).
        order = ["adsb_lol", "adsb_fi", "airplanes_live", "opensky"]
        source = "+".join(t for t in order if t in contributed) or "demo"

    with _cache_lock:
        _cache["data"] = aircraft
        _cache["source"] = source
        _cache["timestamp"] = now
        _cache["total_available"] = total_available

    return aircraft, source, total_available
