"""
Servico de satelites - DADOS REAIS via SatNOGS DB (TLE) + SGP4.

Fonte primaria: https://db.satnogs.org/api/tle/ — TLE publicos atualizados,
sem chave de API. Posicoes calculadas com o propagador SGP4 (biblioteca
"sgp4"), o mesmo modelo usado na industria a partir de TLE.

Fontes secundarias / fallback (CelesTrak, sem chave):
  - GROUP=active (catálogo amplo)
  - GROUP=stations (ISS, CSS, etc.)
  - GROUP=visual (objetos brilhantes)
  - GROUP=weather (NOAA, GOES, MetOp…)
  - GROUP=gnss (GPS / Galileo / GLONASS / BeiDou)
  - GROUP=resource (earth observation)
As listas são mescladas por NORAD id (deduplicação) e amostradas até
MAX_SATELLITES para performance do globo.

Se todas falharem, tenta o ultimo catalogo bom salvo em disco (ver
_load_disk_cache); so em ultimo caso os dados sao simulados — o campo
"source" sempre informa a origem real.

Observacao tecnica: a conversao TEME -> lat/lon usa rotacao GMST e
o elipsoide WGS84 (com refinamento iterativo da latitude geodesica),
o MESMO modelo usado no cliente (biblioteca satellite.js, funcao
eciToGeodetic) — servidor e navegador agora concordam bit a bit.
"""

import json
import logging
import math
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from .http_headers import DEFAULT_HEADERS
from .satellite_details_service import _looks_military
from sgp4.api import Satrec, jday

logger = logging.getLogger(__name__)

SATNOGS_TLE_URL = "https://db.satnogs.org/api/tle/?format=json&limit=25000"
CELESTRAK_BASE = "https://celestrak.org/NORAD/elements/gp.php"
# Grupos CelesTrak (sem chave). Ordem: preferência de cobertura.
_CELESTRAK_GROUPS = (
    "active",
    "stations",
    "visual",
    "weather",
    "noaa",
    "goes",
    "gnss",
    "gps-ops",
    "galileo",
    "beidou",
    "resource",
    "science",
    "amateur",
    "cubesat",
    "starlink",
    "oneweb",
    "iridium-NEXT",
    "military",
)
# Timeout por grupo (segundos). "active" e "starlink" sao catalogos
# gigantes (>10 mil e >7 mil objetos respectivamente) gerados
# dinamicamente pelo PHP do CelesTrak - sob carga eles podem demorar
# bem mais que os outros grupos (confirmado com curl: GROUP=stations
# responde em <1s, GROUP=active as vezes nao responde nem em 15s).
# Grupos pequenos usam um timeout curto pra nao segurar o resto do
# lote esperando uma rota que provavelmente nao vai responder mesmo.
_GROUP_TIMEOUTS = {
    "active": 25,
    "starlink": 20,
}
_DEFAULT_GROUP_TIMEOUT = 8

# Antes 2000: numero baixo demais herdado de um limite generico de
# performance "3D" - mas satelites sao billboards 2D simples (igual as
# cameras, ver camera_service.py), nao entidades 3D pesadas, e o
# calculo de posicao agora roda no NAVEGADOR (SGP4 client-side via
# satellite.js, ver satellites.js), nao mais no servidor a cada
# request. O gargalo real de "faltam satelites ativos" nunca foi fonte
# de dados (CelesTrak GROUP=active sozinho ja tem mais de 10 mil
# objetos) - era esse teto cortando a amostra antes de chegar no globo.
# Subimos bastante o teto; o front ajusta a frequencia do calculo local
# de acordo com a quantidade real recebida (ver SGP4 tick adaptativo em
# satellites.js), entao mais satelites nao trava o navegador.
MAX_SATELLITES = None  # sem limite de catalogo/plotagem
EARTH_RADIUS_KM = 6371.0  # usado so como fallback/aprox rapida (orbit_class)

# WGS84 (mesmos parametros usados pela satellite.js no navegador)
_WGS84_A_KM = 6378.137
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)

_cache = {
    "timestamp": 0,
    "satrecs": None,  # lista de (name, Satrec, norad_id, line1, line2)
    "source": "demo",
    "catalog_total": 0,  # total de objetos únicos no catálogo ANTES do teto de amostragem
}
_cache_lock = threading.Lock()
_TLE_REFRESH_SECONDS = 6 * 60 * 60

# ---------- Cache em disco (rede de seguranca quando as APIs falham) ----------
# O CelesTrak (e ocasionalmente o SatNOGS) pode ficar lento/instavel por
# minutos ou horas. Em vez de cair direto pro modo demo quando isso
# acontece, guardamos o ultimo catalogo bom em disco e o servimos (mesmo
# desatualizado por algumas horas - um TLE nao fica inutil de um dia pro
# outro) enquanto tentamos atualizar de novo em segundo plano.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_DISK_CACHE_FILE = os.path.join(_CACHE_DIR, "tle_cache.json")

_NAME_PREFIXES = ["WTX-SAT", "OBS", "COMM", "NAV", "RES"]


def _parse_tle_text(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    satrecs = []
    i = 0
    while i + 2 < len(lines):
        name, line1, line2 = lines[i], lines[i + 1], lines[i + 2]
        if line1.startswith("1 ") and line2.startswith("2 "):
            try:
                sat = Satrec.twoline2rv(line1, line2)
                satrecs.append((name.strip(), sat, sat.satnum, line1, line2))
            except Exception:
                pass
        i += 3
    return satrecs


def _fetch_satnogs():
    try:
        resp = requests.get(SATNOGS_TLE_URL, headers=DEFAULT_HEADERS, timeout=8)
        resp.raise_for_status()
    except Exception as exc:
        # logger.warning (nao .exception): se a rede local nao alcanca o
        # host (firewall/proxy/antivirus bloqueando, ConnectTimeout), nao
        # tem motivo pra' imprimir a stack trace inteira do urllib3 a
        # cada tentativa - so o motivo da falha, uma linha.
        logger.warning("SatNOGS TLE: falha na requisicao (%s): %s", SATNOGS_TLE_URL, exc)
        raise
    records = resp.json()
    if not isinstance(records, list) or not records:
        logger.warning("SatNOGS TLE: resposta vazia/invalida (%d bytes)", len(resp.content or b""))
        raise ValueError("SatNOGS retornou lista vazia")

    satrecs = []
    for rec in records:
        line1 = rec.get("tle1")
        line2 = rec.get("tle2")
        name = (rec.get("tle0") or "").strip() or f"NORAD-{rec.get('norad_cat_id')}"
        norad = rec.get("norad_cat_id")
        if not line1 or not line2:
            continue
        try:
            sat = Satrec.twoline2rv(line1, line2)
            satrecs.append((name, sat, norad if norad is not None else sat.satnum, line1, line2))
        except Exception:
            continue

    if not satrecs:
        logger.warning("SatNOGS TLE: %d registros recebidos, 0 TLEs validos apos parse", len(records))
        raise ValueError("Nenhum TLE valido do SatNOGS")

    catalog_total = len(satrecs)
    logger.info("SatNOGS TLE: %d satelites carregados com sucesso", catalog_total)
    return satrecs, catalog_total


def _fetch_celestrak_group(group, timeout):
    try:
        resp = requests.get(
            CELESTRAK_BASE,
            params={"GROUP": group, "FORMAT": "tle"},
            headers=DEFAULT_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as exc:
        # Mesmo motivo do _fetch_satnogs acima: uma linha com o motivo,
        # nao a stack trace completa do urllib3/requests por grupo (com
        # ~18 grupos, uma rede que nao alcanca celestrak.org vira uma
        # parede de trace inutil no log a cada carregamento).
        logger.warning("CelesTrak GROUP=%s: falha na requisicao: %s", group, exc)
        raise
    parsed = _parse_tle_text(resp.text)
    if not parsed:
        logger.warning("CelesTrak GROUP=%s: 0 TLEs apos parse (%d bytes)", group, len(resp.content or b""))
    return parsed


def _fetch_celestrak():
    """
    Busca TODOS os grupos em paralelo, cada um com seu proprio timeout
    (ver _GROUP_TIMEOUTS). Cada grupo falha de forma isolada - nao ha
    mais um timeout GLOBAL no as_completed que derrubava o lote inteiro
    (TimeoutError: N of M futures unfinished) so porque um ou dois
    grupos grandes (tipicamente "active") demoravam mais que o previsto.
    Cada requisicao individual ja e limitada pelo timeout do requests.get,
    entao o as_completed aqui so espera cada future terminar (com sucesso
    ou erro) por conta propria - o pior caso realista e limitado pelo
    maior timeout configurado (hoje 25s, para "active").
    """
    by_norad = {}
    ok_groups = []

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {
            ex.submit(_fetch_celestrak_group, g, _GROUP_TIMEOUTS.get(g, _DEFAULT_GROUP_TIMEOUT)): g
            for g in _CELESTRAK_GROUPS
        }
        for fut in as_completed(futs):
            group = futs[fut]
            try:
                batch = fut.result()
            except Exception:
                logger.warning("CelesTrak GROUP=%s: descartado (ver erro acima)", group)
                continue
            ok_groups.append(group)
            for name, sat, norad, line1, line2 in batch:
                if norad not in by_norad:
                    by_norad[norad] = (name, sat, norad, line1, line2)

    all_satrecs = list(by_norad.values())
    if not all_satrecs:
        logger.error("CelesTrak: todos os grupos falharam, catalogo vazio")
        raise ValueError("CelesTrak retornou lista vazia")

    catalog_total = len(all_satrecs)
    failed = [g for g in _CELESTRAK_GROUPS if g not in ok_groups]
    if failed:
        logger.warning("CelesTrak: %d/%d grupos falharam (%s) - seguindo com os demais",
                        len(failed), len(_CELESTRAK_GROUPS), ", ".join(failed))
    logger.info("CelesTrak: %d satelites unicos carregados (%d/%d grupos ok)",
                catalog_total, len(ok_groups), len(_CELESTRAK_GROUPS))
    return all_satrecs, catalog_total


def _gmst_radians(jd, fr):
    jd_ut1 = jd + fr
    t = (jd_ut1 - 2451545.0) / 36525.0
    gmst_deg = (
        280.46061837
        + 360.98564736629 * (jd_ut1 - 2451545.0)
        + 0.000387933 * t * t
        - (t ** 3) / 38710000.0
    )
    return math.radians(gmst_deg % 360.0)


def _ecef_to_geodetic_wgs84(x_ecef, y_ecef, z_ecef):
    """
    ECEF -> geodesica (lat/lon/altitude) sobre o elipsoide WGS84, com
    refinamento iterativo da latitude (algoritmo padrao, o mesmo usado
    pela biblioteca satellite.js no navegador em eciToGeodetic/ecfToGeodetic).

    Antes: o servidor tratava a Terra como esfera perfeita de raio
    6371km. Isso diverge do WGS84 (raio equatorial 6378.137km, achatamento
    nos polos) e gerava erro real de posicao - maior perto dos polos,
    mas mensuravel (alguns km) mesmo perto do equador - e, mais grave,
    DIVERGIA do calculo feito no navegador (satellite.js ja usa WGS84),
    entao a mesma orbita aparecia em lugares levemente diferentes se
    calculada no servidor vs no cliente.
    """
    lon = math.atan2(y_ecef, x_ecef)
    r_xy = math.sqrt(x_ecef ** 2 + y_ecef ** 2)

    # Chute inicial (esferico) e depois refina iterativamente.
    lat = math.atan2(z_ecef, r_xy * (1.0 - _WGS84_E2))
    C = _WGS84_A_KM
    for _ in range(8):
        sin_lat = math.sin(lat)
        C = _WGS84_A_KM / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
        lat_new = math.atan2(z_ecef + C * _WGS84_E2 * sin_lat, r_xy)
        if abs(lat_new - lat) < 1e-12:
            lat = lat_new
            break
        lat = lat_new

    cos_lat = math.cos(lat)
    if abs(cos_lat) > 1e-10:
        altitude_km = r_xy / cos_lat - C
    else:
        # polos: formula acima degenera (cos_lat ~ 0)
        altitude_km = abs(z_ecef) - _WGS84_A_KM * (1.0 - _WGS84_F)

    return math.degrees(lat), math.degrees(lon), altitude_km


def _teme_to_geodetic(r_km, jd, fr):
    x, y, z = r_km
    theta = _gmst_radians(jd, fr)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    x_ecef = x * cos_t + y * sin_t
    y_ecef = -x * sin_t + y * cos_t
    z_ecef = z

    return _ecef_to_geodetic_wgs84(x_ecef, y_ecef, z_ecef)


def _classify_orbit(altitude_km):
    if altitude_km < 2000:
        return "LEO"
    if altitude_km < 35000:
        return "MEO"
    return "GEO"


def _propagate_real(satrecs):
    now = datetime.now(timezone.utc)
    jd, fr = jday(now.year, now.month, now.day, now.hour, now.minute, now.second + now.microsecond / 1e6)

    result = []
    for name, sat, norad, *_rest in satrecs:
        error_code, r, v = sat.sgp4(jd, fr)
        if error_code != 0:
            continue

        lat, lon, altitude_km = _teme_to_geodetic(r, jd, fr)
        velocity_kms = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)

        result.append({
            "id": f"norad-{norad}",
            "norad_id": norad,
            "name": name,
            "orbit_class": _classify_orbit(altitude_km),
            "altitude_km": round(max(altitude_km, 0)),
            "velocity_kms": round(velocity_kms, 2),
            "inclination_deg": round(math.degrees(sat.inclo), 1),
            "lat": round(lat, 4),
            "lon": round(((lon + 180) % 360) - 180, 4),
            "status": "TRACKING",
            # Heuristica por nome (mesma logica de satellite_details_service) -
            # nao e uma classificacao oficial/confirmada, so um indicativo pra
            # permitir filtrar satelites com nomes tipicos de uso militar.
            "is_military": _looks_military(name),
        })

    return result


# ---------- Cache em disco ----------

def _save_disk_cache(satrecs, source, catalog_total, timestamp):
    """Salva o catalogo atual em disco (escrita atomica via arquivo temp +
    replace) para servir de rede de seguranca quando CelesTrak/SatNOGS
    falharem na proxima atualizacao.

    No Windows o Flask debug (reloader) sobe 2 processos que podem
    gravar o mesmo tle_cache.json ao mesmo tempo; antivirus/indexador
    tambem seguram o arquivo. os.replace ai' estoura WinError 32
    (PermissionError). Mitigacoes:
      1) nome .tmp unico por processo (pid + thread + time) — evita
         dois writers no mesmo .tmp;
      2) retries curtos no replace;
      3) fallback: escrita direta no destino se o replace continuar
         falhando (melhor cache um pouco "sujo" do que nenhum).
    """
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        payload = {
            "timestamp": timestamp,
            "source": source,
            "catalog_total": catalog_total,
            "satellites": [
                {"name": name, "norad": norad, "line1": line1, "line2": line2}
                for name, _sat, norad, line1, line2 in satrecs
            ],
        }
        # Nome unico: evita colisao entre o processo pai e o filho do
        # reloader do Flask (e entre threads de refresh concorrentes).
        tmp_path = (
            f"{_DISK_CACHE_FILE}.{os.getpid()}.{threading.get_ident()}."
            f"{int(time.time() * 1000)}.tmp"
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                pass

        last_err = None
        for attempt in range(5):
            try:
                os.replace(tmp_path, _DISK_CACHE_FILE)
                last_err = None
                break
            except PermissionError as exc:
                # WinError 32: arquivo em uso por outro processo.
                last_err = exc
                time.sleep(0.05 * (attempt + 1))
            except OSError as exc:
                last_err = exc
                time.sleep(0.05 * (attempt + 1))

        if last_err is not None:
            # Fallback nao-atomico: sobrescreve o destino direto.
            # No Windows isso ainda pode falhar se o destino estiver
            # aberto em modo exclusivo, mas costuma funcionar quando
            # o replace e' que travava.
            try:
                with open(_DISK_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                logger.warning(
                    "Cache TLE: replace falhou (%s); gravado direto em %s",
                    last_err, _DISK_CACHE_FILE,
                )
            except Exception:
                logger.exception(
                    "Cache TLE: falha ao salvar catalogo em disco (%s)",
                    _DISK_CACHE_FILE,
                )
            finally:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
        else:
            # Limpa qualquer .tmp orfao deste processo (best-effort).
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
    except Exception:
        logger.exception("Cache TLE: falha ao salvar catalogo em disco (%s)", _DISK_CACHE_FILE)


def _load_disk_cache():
    """Le o ultimo catalogo salvo em disco, se existir e for valido.
    Retorna (satrecs, source, catalog_total, timestamp) ou None."""
    try:
        with open(_DISK_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("Cache TLE: arquivo em disco corrompido/ilegivel (%s)", _DISK_CACHE_FILE)
        return None

    satrecs = []
    for item in payload.get("satellites", []):
        try:
            sat = Satrec.twoline2rv(item["line1"], item["line2"])
            satrecs.append((item["name"], sat, item["norad"], item["line1"], item["line2"]))
        except Exception:
            continue

    if not satrecs:
        return None

    age_hours = (time.time() - payload.get("timestamp", 0)) / 3600.0
    logger.warning(
        "Cache TLE: usando catalogo salvo em disco (%d satelites, %.1fh de idade) como rede de seguranca",
        len(satrecs), age_hours,
    )
    return satrecs, payload.get("source", "disk_cache"), payload.get("catalog_total", len(satrecs)), payload.get("timestamp", 0)


# ---------- Orquestracao do cache em memoria (com refresh em segundo plano) ----------

def _refresh_cache_sync(now):
    """Tenta atualizar o cache: CelesTrak -> SatNOGS -> cache em disco -> demo.
    Chamada tanto de forma sincrona (1a carga, sem nada em memoria/disco)
    quanto de dentro de uma thread de background (cache existente mas
    desatualizado)."""
    satrecs = None
    source = None
    catalog_total = 0

    try:
        satrecs, catalog_total = _fetch_celestrak()
        source = "celestrak_multi"
    except Exception:
        logger.warning("get_satellites: CelesTrak falhou, tentando SatNOGS como fallback")
        try:
            satrecs, catalog_total = _fetch_satnogs()
            source = "satnogs_live"
        except Exception:
            logger.error("get_satellites: CelesTrak e SatNOGS falharam - tentando cache em disco")
            disk = _load_disk_cache()
            if disk is not None:
                satrecs, source, catalog_total, _disk_ts = disk
            else:
                logger.error("get_satellites: sem cache em disco disponivel - caindo para modo demo")

    with _cache_lock:
        if satrecs:
            _cache["satrecs"] = satrecs
            _cache["source"] = source
            _cache["timestamp"] = now
            _cache["catalog_total"] = catalog_total
        else:
            # Nao apaga um catalogo anterior que ainda esteja em memoria
            # (mesmo desatualizado) so porque a atualizacao falhou - so
            # cai pra demo se realmente nao houver nada utilizavel ainda.
            if _cache["satrecs"] is None:
                _cache["source"] = "demo"
                _cache["catalog_total"] = 0

    # So grava em disco catalogos que vieram de uma fonte viva (nao
    # reescreve o disco com o proprio conteudo que acabou de ser lido dele).
    if source in ("celestrak_multi", "satnogs_live") and satrecs:
        _save_disk_cache(satrecs, source, catalog_total, now)


def _kick_background_refresh():
    def _job():
        try:
            _refresh_cache_sync(time.time())
        except Exception:
            logger.exception("Erro inesperado no refresh de TLE em segundo plano")

    threading.Thread(target=_job, daemon=True, name="tle-refresh").start()


def _ensure_cache_ready():
    """Garante que _cache tenha algo utilizavel antes de servir uma
    requisicao, SEM bloquear desnecessariamente:
      - cache fresco em memoria -> nao faz nada.
      - cache em memoria existe mas esta velho -> serve o que tem e
        dispara a atualizacao em background (nao bloqueia a request atual).
      - cache vazio (cold start) -> tenta cache em disco primeiro (rapido,
        local) para responder ja nessa request; dispara atualizacao real
        em background. So bloqueia de verdade (fetch sincrono) se nem
        memoria nem disco tiverem nada ainda.
    """
    now = time.time()
    with _cache_lock:
        satrecs = _cache["satrecs"]
        is_fresh = satrecs is not None and (now - _cache["timestamp"]) < _TLE_REFRESH_SECONDS

    if is_fresh:
        return

    if satrecs is not None:
        _kick_background_refresh()
        return

    disk = _load_disk_cache()
    if disk is not None:
        d_satrecs, d_source, d_catalog_total, d_timestamp = disk
        with _cache_lock:
            _cache["satrecs"] = d_satrecs
            _cache["source"] = d_source
            _cache["timestamp"] = d_timestamp
            _cache["catalog_total"] = d_catalog_total
        _kick_background_refresh()
        return

    # Nada em memoria, nada em disco: primeira execucao do app.
    # Aqui precisa mesmo ser sincrono - nao ha nada pra servir enquanto isso.
    _refresh_cache_sync(now)


# ---------- Fallback simulado ----------

_ORBIT_CLASSES = [
    {"name": "LEO", "altitude_km": (400, 900), "velocity_kms": (7.4, 7.8)},
    {"name": "MEO", "altitude_km": (5000, 20000), "velocity_kms": (3.5, 6.0)},
    {"name": "GEO", "altitude_km": (35786, 35786), "velocity_kms": (3.07, 3.07)},
]

_demo_seed = None


def _build_demo_seed(count=60):
    seed = []
    for i in range(count):
        orbit = random.choice(_ORBIT_CLASSES)
        seed.append({
            "id": f"demo-sat-{i}",
            "name": f"{random.choice(_NAME_PREFIXES)}-{100 + i}",
            "orbit_class": orbit["name"],
            "altitude_km": round(random.uniform(*orbit["altitude_km"])),
            "velocity_kms": round(random.uniform(*orbit["velocity_kms"]), 2),
            "inclination_deg": round(random.uniform(0, 98), 1),
            "phase": random.uniform(0, 2 * math.pi),
            "raan": random.uniform(0, 2 * math.pi),
        })
    return seed


def _demo_position(sat, t):
    # Antes: dividia por altitude apenas, como se a orbita girasse em
    # torno da propria altitude e nao do centro da Terra. Isso inflava
    # o contraste entre classes de orbita: LEO (altitude ~600km) e GEO
    # (altitude ~35786km) tem raios orbitais reais de ~6971km e
    # ~42157km - uma razao de so ~6x - mas a formula antiga comparava
    # 600 com 35786, uma razao de ~60x, 10x maior que o real. Na
    # pratica podia fazer um satelite rotulado com velocidade MENOR
    # girar mais rapido na tela que um rotulado com velocidade MAIOR.
    # Fisicamente correto: velocidade angular = v_tangencial / raio,
    # onde raio = raio da Terra + altitude (raio orbital real).
    orbital_radius_km = EARTH_RADIUS_KM + sat["altitude_km"]
    angular_speed = sat["velocity_kms"] / max(orbital_radius_km, 1) * 40
    angle = sat["phase"] + t * angular_speed
    lat = math.degrees(math.asin(math.sin(math.radians(sat["inclination_deg"])) * math.sin(angle)))
    lon = math.degrees(sat["raan"] + angle) % 360
    if lon > 180:
        lon -= 360
    return round(lat, 4), round(lon, 4)


def _generate_demo_satellites():
    global _demo_seed
    if _demo_seed is None:
        _demo_seed = _build_demo_seed()

    t = time.time() / 60.0
    result = []
    for sat in _demo_seed:
        lat, lon = _demo_position(sat, t)
        result.append({
            "id": sat["id"],
            "norad_id": None,
            "name": sat["name"],
            "orbit_class": sat["orbit_class"],
            "altitude_km": sat["altitude_km"],
            "velocity_kms": sat["velocity_kms"],
            "inclination_deg": sat["inclination_deg"],
            "lat": lat,
            "lon": lon,
            "status": "TRACKING",
            "is_military": False,
        })
    return result


def get_satellites():
    _ensure_cache_ready()

    with _cache_lock:
        source = _cache["source"]
        satrecs = _cache["satrecs"]

    if source in ("satnogs_live", "celestrak_live", "celestrak_multi", "disk_cache") and satrecs:
        try:
            positions = _propagate_real(satrecs)
            if positions:
                return positions, source
        except Exception:
            logger.exception("get_satellites: falha ao propagar SGP4 sobre %d satrecs em cache", len(satrecs))

    return _generate_demo_satellites(), "demo"


# ---------- TLE cru para propagacao no CLIENTE (SGP4 no navegador) ----------
#
# OTIMIZACAO (2026-09): antes o front pedia /api/satellites/ a cada 5s so
# pra mover os pontos - cada request recalculava SGP4 no servidor pra ate
# MAX_SATELLITES orbitas e reserializava tudo em JSON, so pra atualizar uma
# posicao que e 100% previsivel matematicamente a partir do TLE (que so
# muda de fato a cada poucas horas). Agora o servidor entrega o TLE cru
# (mesmo cache de 6h de _cache acima) e o navegador roda o SGP4 localmente
# a cada frame via satellite.js, sem nenhuma chamada de rede extra. Isso
# elimina ~99% do trafego/round-trips dessa camada sem perder precisao -
# a posicao calculada no cliente e a MESMA formula (SGP4 a partir do TLE),
# so que sem esperar o proximo polling.
def get_satellites_tle():
    """
    Garante que o cache de TLEs esteja pronto para servir (reaproveita a
    mesma logica de _ensure_cache_ready de get_satellites - memoria fresca
    -> memoria velha + refresh em background -> disco + refresh em
    background -> so ai busca sincrona) e devolve a lista crua de
    elementos orbitais + metadados leves, para o SGP4 do navegador.
    """
    _ensure_cache_ready()

    with _cache_lock:
        source = _cache["source"]
        satrecs = _cache["satrecs"]
        catalog_total = _cache.get("catalog_total") or 0

    if source not in ("satnogs_live", "celestrak_live", "celestrak_multi", "disk_cache") or not satrecs:
        return [], "demo", 0

    entries = []
    for item in satrecs:
        if len(item) < 5:
            # tupla antiga (sem linhas cruas) - nao deveria acontecer, mas
            # nao quebra: so pula esse satelite da lista de TLE cru.
            continue
        name, sat, norad, line1, line2 = item
        entries.append({
            "id": f"norad-{norad}",
            "norad_id": norad,
            "name": name,
            "line1": line1,
            "line2": line2,
            "inclination_deg": round(math.degrees(sat.inclo), 1),
            "orbit_class": _classify_orbit(
                # aproximacao rapida de altitude media a partir do semi-eixo
                # (nao precisa propagar aqui - so pra rotulo/icone inicial;
                # a posicao real vem do SGP4 no navegador)
                max((sat.a * EARTH_RADIUS_KM) - EARTH_RADIUS_KM, 0)
            ),
            "is_military": _looks_military(name),
        })

    return entries, source, catalog_total
