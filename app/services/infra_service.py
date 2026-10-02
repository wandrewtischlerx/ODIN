"""
Camada de infraestrutura física (energia + internet) — ver INFRAFISICA.md
na raiz do projeto para o plano completo em blocos.

Diferença importante em relação a aircraft_service/ship_service/etc: nada
aqui se move. Cabo submarino, datacenter e usina de energia ficam no
mesmo lugar por anos, então o cache pode ser generoso (horas/dias) em
vez dos poucos segundos usados nas camadas de posição em tempo real.

Este módulo traz:
  - `cached()`      — cache genérico em memória com TTL, para ser
                       reusado por qualquer sub-camada de infraestrutura
                       (Bloco 0 do plano).
  - `get_power_plants()` — Bloco 2: usinas de hidrelétrica, nuclear,
                       carvão, solar e eólica, a partir do Global Power
                       Plant Database (WRI).
  - `get_submarine_cables()` / `get_cable_landing_points()` — Bloco 1:
                       cabos submarinos e seus pontos de aterrissagem,
                       a partir da API pública da TeleGeography.
  - `get_datacenters()` — Bloco 3: datacenters a partir do OpenStreetMap
                       (Overpass API), com fallback para uma pequena
                       lista curada à mão dos "megacampi" mais
                       conhecidos publicamente.
"""

import json
import os
import threading
import time

import requests

from .http_headers import DEFAULT_HEADERS

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_DATA_DIR = os.path.join(_APP_DIR, "static", "data")

_cache_lock = threading.Lock()
_cache = {}  # key -> (expires_at_monotonic, value)


def cached(key, ttl_seconds, loader):
    """
    Cache genérico em memória com TTL, para as sub-camadas de
    infraestrutura (que mudam pouco) reusarem em vez de cada uma
    reimplementar seu próprio dict de cache — como já acontece hoje em
    ship_service.py / camera_service.py, mas aqui fatorado uma vez só.

    `loader` é uma função sem argumentos, chamada de novo apenas quando
    o valor não está em cache ou o TTL expirou.
    """
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry[0] > now:
            return entry[1]

    value = loader()

    with _cache_lock:
        _cache[key] = (now + ttl_seconds, value)
    return value


# ---------------------------------------------------------------------
# Bloco 2 — Usinas de energia (Global Power Plant Database / WRI)
# ---------------------------------------------------------------------

# Rótulos em pt-BR para os 5 tipos cobertos (ver INFRAFISICA.md, Bloco 2:
# por que só esses 5 e por que uma fonte única cobre todos).
FUEL_LABELS = {
    "hydro": "Hidrelétrica",
    "nuclear": "Nuclear",
    "coal": "Carvão",
    "solar": "Solar",
    "wind": "Eólica",
}

_POWER_PLANTS_FILE = os.path.join(_STATIC_DATA_DIR, "global_power_plants.json")
_POWER_PLANTS_TTL = 6 * 3600  # dataset estático versionado no repo; recarregar a cada 6h já é generoso
_POWER_PLANTS_SOURCE_LIVE = "wri_gppd_v1.3.0"


def _load_power_plants_from_disk():
    """
    Lê app/static/data/global_power_plants.json — extraído do CSV
    público do Global Power Plant Database (WRI + Google + KTH + Global
    Energy Observatory, CC-BY-4.0), já filtrado para os 5 tipos de fuel
    pedidos e com só os campos usados pela UI (ver comentário completo
    em INFRAFISICA.md, Bloco 2).
    """
    with open(_POWER_PLANTS_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("plants", [])


def get_power_plants(fuel_filter=None):
    """
    Retorna (plants, source, counts_by_fuel).

    fuel_filter: None (devolve os 5 tipos) ou um set/list com um
    subconjunto de {"hydro", "nuclear", "coal", "solar", "wind"}.
    """
    try:
        plants = cached("power_plants_gppd", _POWER_PLANTS_TTL, _load_power_plants_from_disk)
        source = _POWER_PLANTS_SOURCE_LIVE
    except Exception:
        # Arquivo ausente/corrompido: não derruba a rota, só volta vazio
        # com source="demo" — mesmo contrato das outras camadas quando
        # todas as fontes falham.
        plants, source = [], "demo"

    counts_by_fuel = {}
    for p in plants:
        counts_by_fuel[p["fuel"]] = counts_by_fuel.get(p["fuel"], 0) + 1

    if fuel_filter:
        wanted = set(fuel_filter)
        plants = [p for p in plants if p.get("fuel") in wanted]

    return plants, source, counts_by_fuel


# ---------------------------------------------------------------------
# Bloco 1 — Cabos submarinos de internet (TeleGeography)
# ---------------------------------------------------------------------
#
# Fonte: API pública v3 da TeleGeography, a mesma que alimenta
# submarinecablemap.com (ver INFRAFISICA.md, Bloco 1). Sem chave, dois
# GeoJSON:
#   - cable-geo.json          -> um MultiLineString por cabo (rota)
#   - landing-point-geo.json  -> pontos de aterrissagem (ponto)
#
# Ao contrário das usinas (Bloco 2, dump CSV versionado porque não há
# API viva), aqui existe uma API HTTP de verdade, então seguimos o
# MESMO padrão de fetch ao vivo + cache + fallback demo já usado em
# ship_service.py (_fetch_digitraffic, _fetch_kystverket, etc.) em vez
# de um arquivo estático: `cached()` (Bloco 0) guarda o resultado por
# `_CABLE_TTL` (24h — dataset muda pouco, ver cabeçalho deste arquivo)
# e, se o fetch falhar (rede fora do ar, TeleGeography reestruturar a
# API, etc.), cai para um pequeno catálogo demo com cabos REAIS
# (coordenadas tiradas da própria API em vez de inventadas), com
# `source="demo"` — mesmo contrato de "is_live" das outras camadas.
_CABLE_GEO_URL = "https://www.submarinecablemap.com/api/v3/cable/cable-geo.json"
_LANDING_POINT_URL = "https://www.submarinecablemap.com/api/v3/landing-point/landing-point-geo.json"
_CABLE_TTL = 24 * 3600
_CABLE_SOURCE_LIVE = "telegeography_submarine_cable_map"

# Uso não comercial, atribuição obrigatória — ver INFRAFISICA.md Bloco 1
# e a legenda no frontend (infrastructure.js / index.html).
CABLE_ATTRIBUTION = "TeleGeography Submarine Cable Map — uso não comercial"


def _fetch_geojson(url):
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=12)
    resp.raise_for_status()
    return resp.json()


def _load_submarine_cables_live():
    data = _fetch_geojson(_CABLE_GEO_URL)
    cables = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        if geom.get("type") != "MultiLineString":
            continue
        coords = geom.get("coordinates") or []
        if not coords:
            continue
        cables.append({
            "id": props.get("id") or props.get("feature_id"),
            "name": props.get("name") or props.get("id") or "Cabo sem nome",
            "color": props.get("color") or "#939597",
            # Lista de polilinhas [[ [lon,lat], [lon,lat], ... ], ...] —
            # um cabo pode ter mais de um segmento (ramais, trechos
            # cortados na antimeridiana já vêm assim da própria fonte).
            "coordinates": coords,
        })
    if not cables:
        raise ValueError("cable-geo.json sem features utilizáveis")
    return cables


def _load_cable_landing_points_live():
    data = _fetch_geojson(_LANDING_POINT_URL)
    points = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        points.append({
            "id": props.get("id"),
            "name": props.get("name") or props.get("id") or "Ponto de aterrissagem",
            "lon": coords[0],
            "lat": coords[1],
        })
    if not points:
        raise ValueError("landing-point-geo.json sem features utilizáveis")
    return points


# Catálogo demo: subconjunto pequeno mas REAL, coletado da própria API
# da TeleGeography (não são coordenadas inventadas) — usado só quando o
# fetch ao vivo falha, mesmo espírito do `_generate_demo_ships()` em
# ship_service.py, mas aqui com dados reais em vez de sintéticos porque
# rota de cabo submarino não faz sentido "simular".
_DEMO_CABLES = [
    {"id": "staonuk", "name": "Sta’O’Nuk", "color": "#939597", "coordinates": [
        [[-124.15878359999995, 47.00867866177272], [-124.79922827506617, 47.00867866177272], [-125.84411562102707, 47.18890573821204], [-131.48142441885645, 49.181312320441506], [-138.6055621008633, 49.53159507974127], [-151.1998186532859, 50.167261162927154], [-179.9997982511102, 50.167261162927154]],
        [[179.99994672169302, 50.167261162927154], [172.79995182223692, 50.167261162927154], [160.19996074818866, 47.1973959908226], [149.37742517921646, 40.3249057978393], [142.13017422114757, 37.05812312726112], [141.80360860127047, 36.68462112355202], [141.10965669810133, 36.43274008037108], [140.6124746247436, 36.383483735312474]],
    ]},
    {"id": "teide", "name": "Teide", "color": "#939597", "coordinates": [
        [[-17.890913089969356, 27.820205885436238], [-17.702471921560843, 27.621708263143205], [-17.108172720353956, 27.564309487941923], [-16.452122916453824, 27.680590343643605], [-16.256250920272315, 28.098786704839473], [-16.36231417284318, 28.37762364282845]],
    ]},
    {"id": "sharm-el-sheikhtaba", "name": "Sharm El Sheikh–Taba", "color": "#939597", "coordinates": [
        [[34.89486951608693, 29.492576422604227], [34.78439867969573, 29.296805502199817], [34.51762978332711, 28.313851747133643], [34.46137982377105, 27.9516384773656], [34.33400486710986, 27.911373381384173]],
    ]},
    {"id": "reunion", "name": "ReuNION", "color": "#939597", "coordinates": [
        [[55.33647030000018, -21.045262300000438], [54.95892964466648, -20.824682350205684], [53.90885639337331, -20.839909118415903], [51.80621090026917, -23.18985662156474], [47.700040444783745, -25.74212709449577], [44.98915396913622, -26.78539478930014], [38.6741916502968, -27.91659434139477], [33.78107002428003, -29.074394963309043], [32.89292207694332, -29.13953294145049], [31.757961738301827, -28.950559666538012]],
    ]},
    {"id": "aurora", "name": "Aurora", "color": "#939597", "coordinates": [
        [[-74.04709330840446, 40.12349265823708], [-71.12532771113655, 40.75554434597573], [-68.46369557420283, 41.557676585114876], [-61.3815229064093, 42.546605873348796], [-50.4618892767703, 45.26889399691159], [-39.60007920054038, 48.534742361946115], [-16.586438673353253, 56.091844540481155], [-9.114897168304458, 59.34724471319289], [-5.397578190961047, 59.893988261257114], [-1.73046992030393, 59.689474249320014], [1.951219620083589, 58.728777781439675], [4.377121965303638, 57.75082493621763], [5.575460910635191, 56.75030516581843], [6.44128060978026, 56.275851416688575], [7.170737552312092, 55.96752601048291], [7.680308988056977, 55.84318584148108], [8.329168335478972, 55.75165023178103]],
    ]},
    {"id": "ugarit-2", "name": "UGARIT 2", "color": "#939597", "coordinates": [
        [[33.61060042587536, 34.82728147271538], [33.963136162543634, 34.878765011308865], [35.89779880560243, 34.89170328553848]],
    ]},
    {"id": "canoa", "name": "Canoa", "color": "#939597", "coordinates": [
        [[-68.43815728182744, 18.621371316104735], [-68.13101636915388, 19.216024565759717], [-66.36563406940026, 20.652116622049867], [-63.89988049678528, 25.774798230354047], [-64.19661251510644, 31.343711707508767], [-64.65917995948635, 32.36157723537831]],
    ]},
    {"id": "olaluz", "name": "OlaLuz", "color": "#939597", "coordinates": [
        [[-68.43815728182744, 18.621371316104735], [-68.3256510796958, 19.104405475930452], [-68.30571941434663, 20.534032078631704], [-68.98435677254143, 23.493926500615107], [-76.99069153622787, 27.53868135607389], [-77.83674196226877, 27.570883949425436], [-79.64986933934534, 27.638389595699074], [-80.59005720000005, 28.033771027694833]],
    ]},
    {"id": "alisios", "name": "Alisios", "color": "#939597", "coordinates": [
        [[-79.7534792659471, 9.437721984870015], [-78.46075662030401, 10.99869151709461], [-74.31778083208685, 13.044203873110378], [-69.8999012463142, 15.194790433018051], [-68.51237722984521, 17.753367672551875], [-68.15304567969605, 18.30445211017829], [-68.43815728182744, 18.621371316104735]],
        [[-79.54654941253821, 8.934106573765973], [-79.64986933934534, 8.190543417795496], [-79.36679707273034, 7.293759751736291], [-79.93571598572393, 5.159614438819354], [-82.21109799429291, 2.503761349568526], [-83.3190147029117, 0.57451117034074], [-83.77146065844866, -3.289115312063002], [-83.62301100707693, -6.652671276676418], [-77.85610611183004, -19.305384072361306], [-74.73170876151242, -27.135039105338063], [-72.6202165753024, -31.876617130326856], [-71.62043502747198, -33.04554123247811]],
    ]},
    {"id": "fnix", "name": "Fénix", "color": "#939597", "coordinates": [
        [[-68.89264695986267, 12.09043961830498], [-68.41647886028052, 11.574086682917548], [-67.770158499965, 10.977491881247163], [-67.38737802680521, 10.88496358239351], [-66.88962837881974, 10.603529760437182]],
    ]},
    {"id": "fastnet", "name": "Fastnet", "color": "#939597", "coordinates": [
        [[-75.09166944401296, 38.30238308417878], [-74.19809461209168, 39.00102999916448], [-71.12532771113655, 40.37468131106347], [-68.43239142746295, 40.887476848816455], [-61.14942221526864, 41.91423508180767], [-50.43701185338662, 43.917160146656805], [-39.638632614540874, 46.752600009064196], [-23.43847180732781, 50.46128383902831], [-16.23846770188924, 50.33411200456198], [-10.798185016539508, 50.466767648492606], [-9.002397248000438, 51.56737616999966]],
    ]},
]

# Idem: pontos de aterrissagem reais (mesma coleta), cobrindo os cabos
# demo acima e mais alguns para o catálogo demo não ficar vazio demais.
_DEMO_LANDING_POINTS = [
    {"id": "owia-saint-vincent-and-the-grenadines", "name": "Owia, Saint Vincent and the Grenadines", "lon": -61.14277795835044, "lat": 13.373100524281316},
    {"id": "ratmalana-sri-lanka", "name": "Ratmalana, Sri Lanka", "lon": 79.88927545353178, "lat": 6.820387603783256},
    {"id": "hermosa-beach-ca-united-states", "name": "Hermosa Beach, CA, United States", "lon": -118.39954892055475, "lat": 33.862232632157735},
    {"id": "porthcurno-united-kingdom", "name": "Porthcurno, United Kingdom", "lon": -5.654511602696961, "lat": 50.043048657721435},
    {"id": "estepona-spain", "name": "Estepona, Spain", "lon": -5.145869384899948, "lat": 36.42710220192439},
    {"id": "fujairah-united-arab-emirates", "name": "Fujairah, United Arab Emirates", "lon": 56.33372573425405, "lat": 25.12169311729696},
    {"id": "penang-malaysia", "name": "Penang, Malaysia", "lon": 100.40982400223058, "lat": 5.368393581488473},
    {"id": "songkhla-thailand", "name": "Songkhla, Thailand", "lon": 100.5951000000003, "lat": 7.198819999999446},
    {"id": "island-park-ny-united-states", "name": "Island Park, NY, United States", "lon": -73.6559282736054, "lat": 40.60030327678829},
    {"id": "danang-vietnam", "name": "Danang, Vietnam", "lon": 108.21474366875417, "lat": 16.051559827539638},
    {"id": "tuas-singapore", "name": "Tuas, Singapore", "lon": 103.64707112328632, "lat": 1.338192522185158},
    {"id": "tuckerton-nj-united-states", "name": "Tuckerton, NJ, United States", "lon": -74.33791021235805, "lat": 39.60388495499803},
    {"id": "boca-raton-fl-united-states", "name": "Boca Raton, FL, United States", "lon": -80.08894373592818, "lat": 26.350323169523403},
    {"id": "grover-beach-ca-united-states", "name": "Grover Beach, CA, United States", "lon": -120.62142234655879, "lat": 35.1206334382351},
    {"id": "tijuana-mexico", "name": "Tijuana, Mexico", "lon": -117.03822175993173, "lat": 32.53084977596535},
    {"id": "takapuna-new-zealand", "name": "Takapuna, New Zealand", "lon": 174.76791917811025, "lat": -36.78796527793357},
    {"id": "valparaso-chile", "name": "Valparaíso, Chile", "lon": -71.62048049678572, "lat": -33.045765558769304},
    {"id": "lurin-peru", "name": "Lurin, Peru", "lon": -76.87428536869109, "lat": -12.278527239265857},
    {"id": "rio-de-janeiro-brazil", "name": "Rio de Janeiro, Brazil", "lon": -43.209563123346165, "lat": -22.90339400756494},
    {"id": "santos-brazil", "name": "Santos, Brazil", "lon": -46.32806677354495, "lat": -23.961845467703107},
    {"id": "las-toninas-argentina", "name": "Las Toninas, Argentina", "lon": -56.695491069788275, "lat": -36.47252952471871},
    {"id": "fort-amador-panama", "name": "Fort Amador, Panama", "lon": -79.54673220597367, "lat": 8.934109660849998},
    {"id": "punta-cana-dominican-republic", "name": "Punta Cana, Dominican Republic", "lon": -68.43825618860332, "lat": 18.621279512611874},
    {"id": "riohacha-colombia", "name": "Riohacha, Colombia", "lon": -72.95270611552462, "lat": 11.482908943707109},
    {"id": "hillsboro-or-united-states", "name": "Hillsboro, OR, United States", "lon": -122.98980067645105, "lat": 45.522898824562965},
    {"id": "kahe-point-hi-united-states", "name": "Kahe Point, HI, United States", "lon": -158.1305815657343, "lat": 21.35398165191293},
    {"id": "suva-fiji", "name": "Suva, Fiji", "lon": 178.43744782858172, "lat": -18.123812280017187},
    {"id": "savona-italy", "name": "Savona, Italy", "lon": 8.483759631619103, "lat": 44.305540140669045},
    {"id": "baby-beach-aruba", "name": "Baby Beach, Aruba", "lon": -69.87868485568913, "lat": 12.414141621401608},
    {"id": "halifax-ns-canada", "name": "Halifax, NS, Canada", "lon": -63.573952993896306, "lat": 44.64579771640326},
    {"id": "lynn-ma-united-states", "name": "Lynn, MA, United States", "lon": -70.95026612782192, "lat": 42.46366906639672},
    {"id": "dublin-ireland", "name": "Dublin, Ireland", "lon": -6.248261182079236, "lat": 53.348045408252375},
    {"id": "southport-united-kingdom", "name": "Southport, United Kingdom", "lon": -3.006368947416131, "lat": 53.64793071295795},
    {"id": "bude-united-kingdom", "name": "Bude, United Kingdom", "lon": -4.544404967232005, "lat": 50.82811048000128},
    {"id": "san-juan-pr-united-states", "name": "San Juan, PR, United States", "lon": -66.10666604344867, "lat": 18.465839112933374},
    {"id": "naha-japan", "name": "Naha, Japan", "lon": 127.68055019149426, "lat": 26.21241384638718},
    {"id": "toucheng-taiwan", "name": "Toucheng, Taiwan", "lon": 121.80145279380189, "lat": 24.863592858849902},
    {"id": "karachi-pakistan", "name": "Karachi, Pakistan", "lon": 67.02854237669561, "lat": 24.88937409747322},
    {"id": "haramous-djibouti", "name": "Haramous, Djibouti", "lon": 43.16166943911836, "lat": 11.57367892559012},
    {"id": "yeroskipos-cyprus", "name": "Yeroskipos, Cyprus", "lon": 32.46655514198169, "lat": 34.76641137968897},
    {"id": "marmaris-turkey", "name": "Marmaris, Turkey", "lon": 28.25366750143074, "lat": 36.85524944302755},
    {"id": "mazara-del-vallo-italy", "name": "Mazara del Vallo, Italy", "lon": 12.591276253065702, "lat": 37.650130188288585},
    {"id": "sesimbra-portugal", "name": "Sesimbra, Portugal", "lon": -9.1027513474316, "lat": 38.44269349871853},
    {"id": "goonhilly-downs-united-kingdom", "name": "Goonhilly Downs, United Kingdom", "lon": -5.174531473969396, "lat": 50.0248026869269},
    {"id": "sylt-germany", "name": "Sylt, Germany", "lon": 8.383369077736644, "lat": 54.89850565849031},
    {"id": "dakar-senegal", "name": "Dakar, Senegal", "lon": -17.451915354680473, "lat": 14.686597713745336},
    {"id": "nassau-bahamas", "name": "Nassau, Bahamas", "lon": -77.34025183547105, "lat": 25.067038149721757},
    {"id": "duynefontein-south-africa", "name": "Duynefontein, South Africa", "lon": 18.449914853508062, "lat": -33.69332250414625},
    {"id": "mombasa-kenya", "name": "Mombasa, Kenya", "lon": 39.67280003701128, "lat": -4.053206761989876},
    {"id": "khark-island-iran", "name": "Khark Island, Iran", "lon": 50.31205031256539, "lat": 29.245773309269826},
    {"id": "nanhui-china", "name": "Nanhui, China", "lon": 121.92506915087795, "lat": 30.864713861582327},
    {"id": "toronto-on-canada", "name": "Toronto, ON, Canada", "lon": -79.38531972935779, "lat": 43.64855718648997},
    {"id": "sao-tome-sao-tome-and-principe", "name": "Sao Tome, Sao Tome and Principe", "lon": 6.733297177107941, "lat": 0.333291349734918},
    {"id": "malabo-equatorial-guinea", "name": "Malabo, Equatorial Guinea", "lon": 8.783368794372972, "lat": 3.749792920040749},
    {"id": "sydney-nsw-australia", "name": "Sydney, NSW, Australia", "lon": 151.20704719697792, "lat": -33.86969726258813},
    {"id": "perth-wa-australia", "name": "Perth, WA, Australia", "lon": 115.85721872350325, "lat": -31.95343894398052},
    {"id": "darwin-nt-australia", "name": "Darwin, NT, Australia", "lon": 130.8431456073319, "lat": -12.467490679762426},
    {"id": "whenuapai-new-zealand", "name": "Whenuapai, New Zealand", "lon": 174.6233880304975, "lat": -36.788806016342754},
    {"id": "nelson-new-zealand", "name": "Nelson, New Zealand", "lon": 173.28395005547594, "lat": -41.2722550869257},
]


def get_submarine_cables():
    """Retorna (cables, source)."""
    try:
        cables = cached("submarine_cables_live", _CABLE_TTL, _load_submarine_cables_live)
        source = _CABLE_SOURCE_LIVE
    except Exception:
        # Rede indisponível (ex.: ambiente de desenvolvimento sem saída
        # para submarinecablemap.com) ou a API mudou de formato: não
        # derruba a rota, volta pro catálogo demo — mesmo contrato das
        # demais camadas quando a(s) fonte(s) ao vivo falham.
        cables, source = _DEMO_CABLES, "demo"
    return cables, source


def get_cable_landing_points():
    """Retorna (landing_points, source)."""
    try:
        points = cached("cable_landing_points_live", _CABLE_TTL, _load_cable_landing_points_live)
        source = _CABLE_SOURCE_LIVE
    except Exception:
        points, source = _DEMO_LANDING_POINTS, "demo"
    return points, source


# ---------------------------------------------------------------------
# Bloco 3 — Datacenters (OpenStreetMap / Overpass API)
# ---------------------------------------------------------------------
#
# Não existe uma "TeleGeography grátis" equivalente para datacenters
# (a TeleGeography tem um Data Center Research Map, mas é pago). A
# fonte aberta viável é o OpenStreetMap via Overpass API, consultando
# `telecom=data_center` e `building=data_center` em nós e ways (bbox
# global — cobertura mundial, mas bem desigual por região, ver nota na
# resposta e no HUD).
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_DATACENTER_TTL = 24 * 3600
_DATACENTER_SOURCE_LIVE = "openstreetmap_overpass"

# `out center` em vez de `out geom`/`out body`: só precisamos de 1
# ponto por elemento (centróide, no caso de ways/relations) pra plotar
# no globo — pedir a geometria completa de cada prédio marcado no OSM
# custaria bem mais banda/tempo sem nenhum ganho para essa camada.
_OVERPASS_DATACENTER_QUERY = (
    "[out:json][timeout:50];"
    '(node["telecom"="data_center"];'
    'way["telecom"="data_center"];'
    'node["building"="data_center"];'
    'way["building"="data_center"];'
    ");"
    "out center;"
)

DATACENTER_ATTRIBUTION = "© colaboradores do OpenStreetMap (openstreetmap.org/copyright)"
DATACENTER_COVERAGE_NOTE = (
    "Cobertura no OpenStreetMap é desigual por região — EUA e Europa "
    "têm muito mais datacenters mapeados do que Ásia/África/América "
    "Latina, o que reflete quem mapeou, não onde os datacenters "
    "realmente estão."
)


def _load_datacenters_live():
    resp = requests.post(
        _OVERPASS_URL,
        data={"data": _OVERPASS_DATACENTER_QUERY},
        headers=DEFAULT_HEADERS,
        timeout=55,
    )
    resp.raise_for_status()
    data = resp.json()

    points = []
    seen_ids = set()
    for el in data.get("elements") or []:
        el_type = el.get("type")
        if el_type == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            # way/relation: Overpass devolve o centróide em "center"
            # por causa do `out center` acima.
            center = el.get("center") or {}
            lat, lon = center.get("lat"), center.get("lon")
        if lat is None or lon is None:
            continue

        osm_id = f"{el_type}/{el.get('id')}"
        if osm_id in seen_ids:
            continue
        seen_ids.add(osm_id)

        tags = el.get("tags") or {}
        points.append({
            "id": osm_id,
            "name": tags.get("name") or tags.get("operator") or "Datacenter",
            "operator": tags.get("operator"),
            "lat": lat,
            "lon": lon,
            "approx": False,
        })

    if not points:
        raise ValueError("Overpass sem elementos utilizáveis para data_center")
    return points


# Fallback quando o Overpass falhar (fora do ar, sobrecarregado —
# Overpass é notoriamente instável sob carga — ou rede sem saída pra
# overpass-api.de). Diferente do fallback de cabos (Bloco 1), aqui NÃO
# temos uma amostra real do Overpass à mão para usar como catálogo
# demo: então, em vez de inventar coordenadas de datacenter, usamos a
# ideia do "Bloco 3b" que o plano já previa como extensão opcional —
# uma pequena lista curada à mão dos megacampi de nuvem/IA mais
# divulgados publicamente pelas próprias empresas e pela imprensa
# especializada. Coordenadas aqui são aproximadas (nível de
# cidade/região, não o endereço exato do prédio) — daí `approx: True`
# e `source="demo_curated"` em vez do "demo" genérico, pra deixar
# claro que isso não é uma amostra do dataset real, e sim uma lista
# manual e sabidamente incompleta.
_DEMO_CURATED_DATACENTERS = [
    {"id": "curated/google-the-dalles", "name": "Google Data Center — The Dalles, OR", "operator": "Google", "lat": 45.6087, "lon": -121.1786, "approx": True},
    {"id": "curated/google-council-bluffs", "name": "Google Data Center — Council Bluffs, IA", "operator": "Google", "lat": 41.1544, "lon": -95.8608, "approx": True},
    {"id": "curated/google-eemshaven", "name": "Google Data Center — Eemshaven", "operator": "Google", "lat": 53.4390, "lon": 6.8288, "approx": True},
    {"id": "curated/meta-prineville", "name": "Meta Data Center — Prineville, OR", "operator": "Meta", "lat": 44.2998, "lon": -120.8397, "approx": True},
    {"id": "curated/meta-lulea", "name": "Meta Data Center — Luleå (Node Pole)", "operator": "Meta", "lat": 65.6027, "lon": 22.1569, "approx": True},
    {"id": "curated/meta-odense", "name": "Meta Data Center — Odense", "operator": "Meta", "lat": 55.4038, "lon": 10.4024, "approx": True},
    {"id": "curated/microsoft-quincy", "name": "Microsoft Azure Data Center — Quincy, WA", "operator": "Microsoft", "lat": 47.2343, "lon": -119.8524, "approx": True},
    {"id": "curated/microsoft-boydton", "name": "Microsoft Azure Data Center — Boydton, VA", "operator": "Microsoft", "lat": 36.6676, "lon": -78.3875, "approx": True},
    {"id": "curated/aws-ashburn", "name": "AWS — Ashburn, VA (\"Data Center Alley\")", "operator": "Amazon Web Services", "lat": 39.0438, "lon": -77.4874, "approx": True},
    {"id": "curated/equinix-singapore", "name": "Equinix SG3 — Jurong", "operator": "Equinix", "lat": 1.3208, "lon": 103.6980, "approx": True},
    {"id": "curated/guian-new-area", "name": "Cluster de datacenters — Gui'an New Area", "operator": None, "lat": 26.5, "lon": 106.7, "approx": True},
    {"id": "curated/dublin-cluster", "name": "Cluster de datacenters — Dublin", "operator": None, "lat": 53.3498, "lon": -6.2603, "approx": True},
]


def get_datacenters():
    """Retorna (datacenters, source)."""
    try:
        points = cached("datacenters_live", _DATACENTER_TTL, _load_datacenters_live)
        source = _DATACENTER_SOURCE_LIVE
    except Exception:
        points, source = _DEMO_CURATED_DATACENTERS, "demo_curated"
    return points, source
