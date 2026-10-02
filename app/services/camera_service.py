"""
Servico de cameras publicas - DADOS REAIS, sete fontes independentes,
em cinco paises diferentes.

BUGFIX (2026-09): antes so existia NYC DOT como fonte real - qualquer
instabilidade dela (ou apenas a hotlink-protection das imagens) fazia o
servico inteiro cair pros 8 pontos fixos de demo, que ficavam visiveis
o tempo todo mesmo com a API de NYC no ar mas so as imagens falhando.
Agora ha SEIS fontes reais independentes, combinadas (como ja acontece
em ship_service.py) - so cai pra demo se todas falharem ao mesmo tempo:

  1) NYC DOT (webcams.nyctmc.org/api/cameras) — cameras de transito de
     Nova York, EUA. Protecao de hotlink nas imagens (ver proxy abaixo).
  2) Caltrans CCTV (gisdata.dot.ca.gov, ArcGIS FeatureServer aberto,
     sem chave) — centenas de cameras de rodovia na California, EUA.
     Schema confirmado via
     https://gisdata.dot.ca.gov/arcgis/rest/services/CHhighway/CCTV/FeatureServer/0
     (campos: latitude, longitude, locationName, district, route,
     inService, currentImageURL). Sem protecao de hotlink conhecida,
     mas passa pelo mesmo proxy por consistencia e para evitar mixed
     content/CORS.
  3) UDOT Traffic (udottraffic.utah.gov) — Utah, EUA.
  4) Ontario 511 (511on.ca) — Ontario, CANADA.
  5) Alberta 511 (511.alberta.ca) — Alberta, CANADA.
     (3, 4 e 5 usam a mesma plataforma publica "CARS/511" - endpoint
     /api/v2/get/cameras, JSON, sem chave. Cada camera pode ter mais de
     uma "view"/lente; usamos a primeira view habilitada.)
  6) TfL JamCams (api.tfl.gov.uk/Place/Type/JamCam) — Londres, REINO
     UNIDO. API publica da Transport for London; a URL da imagem vem
     em additionalProperties (key "imageUrl"), um link direto ao S3 da
     TfL, sem protecao de hotlink conhecida.
  7) CET São Paulo (cameras.cetsp.com.br) — BRASIL. Sem API JSON
     publica; as 11 cameras do menu de favoritos foram extraidas do
     array JS embutido no HTML da pagina (gCams) e georreferenciadas
     manualmente pelo cruzamento de vias de cada uma. As imagens vem
     ao vivo do servidor da CET (frames numerados que giram em ciclo,
     reproduzido no proxy).
  8) Clima ao Vivo (climaaovivo.com.br / cmsv2.climaaovivo.com.br) —
     BRASIL. API publica JSON em /api/cameras (~194 cameras de clima
     em dezenas de cidades). Coordenadas vem no JSON; snapshots ao
     vivo em cavsnapshots.S3 com URL assinada, resolvida sob demanda
     via pagina da camera (ou fallback pelo campo desarquivo do SSR).
  9) OpenCCTV (opencctv.org) — agregador global com ~144k–239k markers
     publicos (transito, praia, clima, natureza, etc.). GET
     /api/cameras/markers devolve ids+lat+lng; POST /api/cameras/batch
     (max 50 ids) devolve nome, feed_url, pais, cidade.
     MODO SEM LIMITAÇÃO: devolve o índice COMPLETO (sem amostragem,
     sem filtro de bbox/zoom). O total da Terra fica fixo.
 10) EarthCam Network (earthcam.com/network) — ~300 webcams publicas
     em dezenas de paises (skylines, praias, landmarks, resorts).
     Endpoint: /api/mapsearch/get_locations_network.php?r=ecn&a=fetch
     (sem chave). Snapshots via proxy com Referer earthcam.com.
 11) Webcamera24 (webcamera24.com) — ~4k pontos do mapa global
     (GET /api/v1/cameras/map, sem chave). Cobertura espacial no
     globo; nome/thumbnail/stream por camera nao tem API em lote
     publica (muitos feeds sao YouTube).
 12) SkylineWebcams (skylinewebcams.com) — ~1851 webcams cênicas
     globais (praias, cidades, landmarks) com coordenadas. Base
     embutida em static/data/skylinewebcams.json. Streams usam token
     que expira; páginas HTML e canais YouTube. Thumbnails YouTube
     servidos via proxy; demais com link para a página ao vivo.

SEM LIMITAÇÃO DE QUANTIDADE

Ainda faltam a maioria das cidades do mundo (cada uma publica seu
proprio portal com formato proprio, e a maioria das APIs "globais" de
webcam sao pagas, como a Windy Webcams API, ou exigem cadastro/chave,
como a LTA DataMall de Singapura) - mas a combinacao das fontes
acima + índice completo OpenCCTV + EarthCam + Webcamera24 entrega o
TOTAL DA TERRA disponível nas fontes públicas integradas.

Se TODAS as APIs estiverem indisponiveis, o servico devolve uma lista
vazia (nao inventa cameras fake pra preencher o mapa) - o campo
"source" vira "demo" so como sinalizacao de estado fora do ar, nunca
como uma camera fake de verdade plotada/contada.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from urllib.parse import urljoin, urlsplit, quote, parse_qs
from pathlib import Path

import requests
from .http_headers import DEFAULT_HEADERS

# BUGFIX: o arquivo inteiro usa logger.info/.warning/.exception em varios
# pontos (dedupe, blocklist do YouTube, cameras indisponiveis, cache em
# disco etc.) mas o logger nunca foi criado - qualquer um desses caminhos
# derrubava a rota inteira com "NameError: name 'logger' is not defined"
# (por exemplo assim que o dedupe encontrava 1 duplicata de verdade).
logger = logging.getLogger(__name__)

NYC_CAMERAS_URL = "https://webcams.nyctmc.org/api/cameras"
CALTRANS_CCTV_URL = "https://gisdata.dot.ca.gov/arcgis/rest/services/CHhighway/CCTV/FeatureServer/0/query"
# As tres abaixo sao a mesma plataforma "CARS/511" (Castle Rock ATIS) -
# endpoint e schema JSON identicos, so muda o dominio/pais.
UDOT_CAMERAS_URL = "https://www.udottraffic.utah.gov/api/v2/get/cameras"
ONTARIO511_CAMERAS_URL = "https://511on.ca/api/v2/get/cameras"
ALBERTA511_CAMERAS_URL = "https://511.alberta.ca/api/v2/get/cameras"
TFL_JAMCAM_URL = "https://api.tfl.gov.uk/Place/Type/JamCam"
# Clima ao Vivo (Brasil) — lista publica em cmsv2; snapshots em S3 assinados.
CLIMAAOVIVO_CAMERAS_URL = "https://cmsv2.climaaovivo.com.br/api/cameras"
CLIMAAOVIVO_SITE = "https://www.climaaovivo.com.br"
# Digitraffic / Fintraffic — weathercams da rede viária finlandesa (~800).
DIGITRAFFIC_CAM_URL = "https://tie.digitraffic.fi/api/weathercam/v1/stations"
DIGITRAFFIC_CAM_IMG = "https://weathercam.digitraffic.fi/{preset_id}.jpg"
# Portais 511 no formato mapIcons (Castle Rock / similares) — sem chave.
# Varredura 2026-09: só URLs que devolvem JSON item2[] válido (demais 511
# viraram SPA/HTML ou exigem key — não entram).
_MAPICONS_SOURCES = (
    ("https://www.fl511.com/map/mapIcons/Cameras", "fl511", "Florida 511 (US)"),
    ("https://www.az511.com/map/mapIcons/Cameras", "az511", "Arizona 511 (US)"),
    ("https://www.511ga.org/map/mapIcons/Cameras", "ga511", "Georgia 511 (US)"),
    ("https://drivenc.gov/map/mapIcons/Cameras", "nc511", "North Carolina 511 (US)"),
    ("https://www.nvroads.com/map/mapIcons/Cameras", "nv511", "Nevada 511 (US)"),
    ("https://511.idaho.gov/map/mapIcons/Cameras", "id511", "Idaho 511 (US)"),
    ("https://www.udottraffic.utah.gov/map/mapIcons/Cameras", "utmap", "Utah mapIcons (US)"),
    ("https://www.511pa.com/map/mapIcons/Cameras", "pa511", "Pennsylvania 511 (US)"),
    ("https://www.newengland511.org/map/mapIcons/Cameras", "ne511", "New England 511 (US)"),
    ("https://511.alaska.gov/map/mapIcons/Cameras", "ak511", "Alaska 511 (US)"),
    ("https://www.511la.org/map/mapIcons/Cameras", "la511", "Louisiana 511 (US)"),
    ("https://511ny.org/map/mapIcons/Cameras", "ny511", "New York 511 (US)"),
)
FL511_CAMERAS_URL = _MAPICONS_SOURCES[0][0]  # compat
DELDOT_CAMERAS_URL = "https://tmc.deldot.gov/json/videocamera.json"
# Illinois / Travel Midwest — ArcGIS FeatureServer público (~3.6k CCTV).
IL_ARCGIS_CCTV_URL = (
    "https://services2.arcgis.com/aIrBD8yn1TDTEXoz/arcgis/rest/services/"
    "TrafficCamerasTM_Public/FeatureServer/0/query"
)
# Singapura — data.gov.sg traffic images (sem chave; feed ~instantâneo).
SG_TRAFFIC_IMAGES_URL = "https://api.data.gov.sg/v1/transport/traffic-images"
# EarthCam Network — rede global de webcams publicas (skyline, praia,
# landmarks, resorts, etc.). Endpoint de mapa:
# https://www.earthcam.com/api/mapsearch/get_locations_network.php?r=ecn&a=fetch
# (~300 cameras em dezenas de paises; sem chave).
EARTHCAM_NETWORK_URL = (
    "https://www.earthcam.com/api/mapsearch/get_locations_network.php?r=ecn&a=fetch"
)
# Webcamera24 — mapa global de webcams (~4k pontos com lat/lng).
# GET https://webcamera24.com/api/v1/cameras/map  (sem chave)
# Resposta: { cameras: [ { id, lat, lng } ], ... }. Detalhes ricos
# (nome, thumbnail, stream) exigem slug por camera e nao tem endpoint
# publico em lote; pontos entram no globo para cobertura espacial.
WEBCAMERA24_MAP_URL = "https://webcamera24.com/api/v1/cameras/map"
# OpenTrafficCamMap (crowdsourced USA traffic cams) — dump embutido
# https://github.com/AidanWelch/OpenTrafficCamMap  (~7k, JPEG + HLS)
_OTCM_USA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "data", "opentrafficcammap_usa.json",
)

# Hong Kong Transport Department — CCTV de transito em tempo real,
# dataset publico do data.gov.hk (sem chave). E' a unica fonte de
# China (RAE) que achei com API JSON aberta e sem cadastro; nao existe
# equivalente pra' China continental sem key/licenca ICP (por isso nao
# entrou nenhuma fonte de la' - ver nota no SOURCES.md). Endpoint e
# formato do JSON conferidos pela documentacao publica do dataset; o
# parser abaixo e' defensivo (tenta varios nomes de campo) porque nao
# deu pra' testar contra a API ao vivo neste ambiente - se o schema
# real vier diferente do esperado, o fetcher so' levanta ValueError e
# essa fonte fica de fora silenciosamente, sem quebrar o resto do app.
HK_TD_CCTV_URL = (
    "https://static.data.gov.hk/td/traffic-snapshot-images/code/"
    "Traffic_Camera_Locations_en.json"
)
# ITS Korea (openapi.its.go.kr) — CCTV de rodovia na COREIA DO SUL.
# Endpoint publico NCCTVInfo aceita "key=test" (modo demo do proprio
# portal, sem cadastro) mas com cota/area limitada; pra' cobertura
# completa do pais e uso continuo, o certo e' cadastrar uma chave
# gratuita em https://www.its.go.kr (Coreia do Sul) e trocar
# ITS_KOREA_API_KEY abaixo (ou variavel de ambiente do mesmo nome).
# Bbox cobre o territorio sul-coreano inteiro (o "type=all" pede tanto
# rodovias (ex) quanto vias urbanas (its), mas nem todo deploy aceita
# os dois no mesmo request - se vier vazio, tenta so' "ex").
ITS_KOREA_API_KEY = os.environ.get("ITS_KOREA_API_KEY", "test")
ITS_KOREA_CCTV_URL = "https://openapi.its.go.kr/api/NCCTVInfo"
ITS_KOREA_BBOX = (124.5, 33.0, 131.0, 38.7)  # min_lon, min_lat, max_lon, max_lat
# Haifa (ISRAEL) — dataset municipal aberto de cameras de transito,
# publicado em GeoJSON pelo portal opendata.haifa.muni.il. E' a unica
# fonte israelense com download publico direto (sem chave) que achei;
# cobre so' a cidade de Haifa, nao o pais inteiro (Netivei Israel/MOT
# nao tem API publica aberta conhecida pra rodovias nacionais - ver
# nota no SOURCES.md).
HAIFA_TRAFFIC_CAMERAS_URL = (
    "https://opendata.haifa.muni.il/dataset/traffic_cameras/resource/download"
    "?format=geojson"
)
# Taiwan (國道/rodovias nacionais + 省道/estradas provinciais) —
# Directorate General of Highways (thbapp.thb.gov.tw), API publica JSON
# sem chave. AO CONTRARIO da maioria das fontes acima, ESTA FOI TESTADA
# AO VIVO com sucesso (pesquisa web desta sessao, nao no sandbox de bash
# - thb.gov.tw tambem nao esta' na allowlist de rede daqui): os dois
# endpoints abaixo devolveram milhares de cameras reais em JSON. O
# terceiro (county/vias municipais) devolveu lista vazia no momento do
# teste - mantido mesmo assim, tratado como "sem cameras agora" e nao
# como erro (pode precisar de parametro de cidade, ou os dados voltam
# em outro horario).
# Schema: [{"id", "stakenumber" (nome/km), "gisx" (lon), "gisy" (lat),
#           "html"}] - o campo "html" apesar do nome NAO e' uma pagina:
# e' a URL direta da camera, que pode ser uma imagem JPEG unica por
# requisicao OU um stream MJPEG multipart ("abs2mjpg/bmjpg" na URL
# sugere isso) - nao deu pra confirmar qual dos dois em cada camera
# sem bater na rede deles. get_camera_image() abaixo agora sabe extrair
# um frame unico de um stream multipart/x-mixed-replace, entao funciona
# nos dois casos sem tratamento especial aqui.
TW_THB_FREEWAY_CCTV_URL = "https://thbapp.thb.gov.tw/services/cctv/freeway"
TW_THB_PROVINCIAL_CCTV_URL = "https://thbapp.thb.gov.tw/services/cctv/thb"
TW_THB_COUNTY_CCTV_URL = "https://thbapp.thb.gov.tw/services/cctv/county"
# OpenCCTV — diretorio global de cameras publicas (~144k–239k markers).
OPENCCTV_MARKERS_URL = "https://opencctv.org/api/cameras/markers"
OPENCCTV_BATCH_URL = "https://opencctv.org/api/cameras/batch"
OPENCCTV_BATCH_SIZE = 50
# Amostra espacial para o globo (o indice completo tem ~144k pontos).
MAX_OPENCCTV = 25000  # legado / fallback
# Bounding box aproximado do Brasil (para priorizar na amostra).
_BR_BBOX = (-33.8, -74.0, 5.3, -34.0)  # min_lat, min_lon, max_lat, max_lon
# CET-SP (Cia de Engenharia de Trafego, cidade de Sao Paulo, BRASIL) -
# https://cameras.cetsp.com.br/View/Cam.aspx nao tem API JSON publica:
# a lista de cameras vem embutida como array JS na propria pagina ASPX
# (gCams, ver <script> no HTML) e nao inclui latitude/longitude - so
# pasta (id da camera), titulo/subTitulo/detalhe (nomes das vias) e
# ativa (bool). Por isso as 11 cameras do menu foram extraidas manualmente
# desse array e georreferenciadas (lat/lon) a partir do cruzamento de
# vias descrito em cada uma. As imagens ficam em
# https://cameras.cetsp.com.br/Cams/{pasta}/{n}.jpg , onde n roda de
# apImg ate qtdeImagens (o proprio Cam.js da CET cicla esses frames
# como um mini-timelapse) - reproduzimos esse ciclo no proxy abaixo.
CETSP_BASE_IMG_URL = "https://cameras.cetsp.com.br/Cams/"
CETSP_PAGE_URL = "https://cameras.cetsp.com.br/View/Cam.aspx"
# Antes 150: limite baixo demais deixava o globo com poucas cameras
# mesmo quando as fontes reais tinham muito mais disponivel. O ponto
# fraco de performance do Cesium sao entidades 3D/poligonos, nao
# billboards 2D simples como os das cameras - varios milhares sao
# tranquilos, entao o novo limite e bem mais generoso por fonte.
# Limite por fonte regional (não OpenCCTV): evita milhares de billboards
# de uma só origem; o CONTADOR usa global_total, não a quantidade plotada.
# Antes 800 — cortava Webcamera24 (~4k), Skyline (~1.8k+) e outras fontes
# cênicas no meio. Contador global e mapa já lidam com milhares de
# markers; o teto passa a ser generoso só para evitar abuso de uma fonte
# única que devolva dezenas de milhares (OpenCCTV tem limites próprios).
MAX_CAMERAS_PER_SOURCE = 10000  # FL511 sozinho ja tem ~5k; evita cortar fontes grandes

# As imagens das cameras da NYC DOT tem protecao contra hotlinking: o
# servidor delas verifica o cabecalho Referer/Origin e recusa (403) uma
# tag <img> carregada direto do dominio de outro site. Isso e diferente
# do endpoint JSON /api/cameras (que nao tem essa restricao) - por isso
# a listagem funciona mas a imagem em si nao aparece no navegador do
# usuario. A correcao e o backend buscar a imagem (mandando um Referer
# valido do proprio dominio da NYC, como um navegador faria ao visitar
# o site deles) e repassar os bytes prontos - o <img> do nosso front
# aponta pra rota /api/cameras/image/<id>, nunca direto pro dominio nyctmc.org.
_NYC_IMAGE_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://webcams.nyctmc.org/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
# Caltrans nao tem hotlink-protection conhecida - Referer generico basta.
_CALTRANS_IMAGE_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
# CET-SP tambem nao tem hotlink-protection conhecida, mas manda o
# Referer da propria pagina de cameras por consistencia com as demais.
_CETSP_IMAGE_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": CETSP_PAGE_URL,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
# Clima ao Vivo: snapshots S3 assinados; Referer do site oficial.
_CLIMAAOVIVO_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": CLIMAAOVIVO_SITE + "/",
    "Accept": "application/json, text/html, */*",
}
_CLIMAAOVIVO_IMAGE_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": CLIMAAOVIVO_SITE + "/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
_OPENCCTV_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://opencctv.org/",
    "Origin": "https://opencctv.org",
    "Accept": "application/json",
}
_OPENCCTV_IMAGE_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://opencctv.org/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
_EARTHCAM_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://www.earthcam.com/",
    "Accept": "application/json, text/html, */*",
}
_EARTHCAM_IMAGE_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://www.earthcam.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
# ---------- Indexacao da URL de imagem (generica) ----------
#
# PROBLEMA QUE ISTO RESOLVE: varias fontes (portais 511 no formato
# mapIcons, DelDOT, Webcamera24) eram lidas so' pelas coordenadas e
# gravavam "image_url": None fixo. Como o semaforo pinta de cinza quem
# nao tem imagem, centenas de cameras que FUNCIONAM apareciam como
# desligadas. Nao era a camera estar fora do ar: era o indexador nao
# procurar a imagem.
#
# Em vez de escrever um parser por portal (schemas diferentes, e' o que
# criou o buraco), este extrator varre o objeto JSON inteiro atras de
# qualquer coisa que pareca uma imagem de camera:
#   - <img src="..."> dentro de campos HTML (os portais 511 escondem a
#     imagem no HTML do popup, no campo "description"/"tooltip")
#   - URL terminada em .jpg/.jpeg/.png/.gif/.webp
#   - valor de chave com nome sugestivo (image, img, snapshot, thumb,
#     preview, still, photo) que seja URL ou caminho relativo
# Caminho relativo e' resolvido contra a URL da propria API (urljoin).
#
# Icone de marcador e' filtrado explicitamente - senao o extrator
# indexaria o pin do mapa como se fosse a imagem da camera, que e' um
# erro pior do que nao indexar nada.
_IMG_TAG_RE = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
_IMG_URL_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>()]+?\.(?:jpe?g|png|gif|webp)(?:\?[^\s"'<>()]*)?""", re.I
)
_IMG_KEY_HINTS = (
    "image", "img", "snapshot", "thumb", "photo", "picture",
    "still", "preview", "capture", "frame",
)
_IMG_REJECT = (
    "icon", "marker", "pin", "logo", "sprite", "placeholder", "blank",
    "no-image", "noimage", "no_image", "spacer", "favicon", "avatar",
    "legend", "arrow",
)


# Ausente / vazio NAO entra aqui: valor desconhecido nao e' o mesmo que
# desligado, e a queixa que originou esta correcao foi exatamente camera
# boa sendo marcada como fora do ar.
_FALSY_FLAGS = {"false", "n", "no", "0", "off", "offline", "inactive"}


def _is_falsy_flag(value):
    """True quando o campo indica 'desligado'. Tolerante: a mesma API
    ja' devolveu bool, "true"/"false", "Y"/"N" e 1/0. Valor ausente
    (None) NAO conta como desligado - conta como desconhecido."""
    if value is None:
        return False
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    return str(value).strip().lower() in _FALSY_FLAGS


def _first_id(*values, fallback=None):
    """
    Primeiro valor utilizavel como id. Usa "e' None/vazio?" em vez de
    `a or b`: com `or`, um id legitimo igual a 0 (ou "0") e' descartado
    silenciosamente e a camera some da lista. Varias fontes numeram a
    partir de 0 - era mais um jeito de camera boa nao aparecer.
    """
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return fallback


def _looks_like_marker(url):
    low = str(url).lower()
    return any(bad in low for bad in _IMG_REJECT)


def _extract_image_url(obj, base_url=None, _depth=0):
    """
    Devolve a 1a URL de imagem plausivel dentro de obj (dict/list/str),
    ou None. Prefere campos com nome sugestivo; so depois aceita
    qualquer URL de imagem achada no meio de HTML.
    """
    if _depth > 6:
        return None

    preferred = []
    fallback = []

    def consider(value, key_hinted):
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text:
            return
        # 1) HTML com <img src="...">
        for m in _IMG_TAG_RE.finditer(text):
            cand = m.group(1).strip()
            if cand and not _looks_like_marker(cand):
                (preferred if key_hinted else fallback).append(cand)
        # 2) URL de imagem solta (no HTML ou no proprio valor)
        for m in _IMG_URL_RE.finditer(text):
            cand = m.group(0).strip()
            if cand and not _looks_like_marker(cand):
                (preferred if key_hinted else fallback).append(cand)
        # 3) chave sugestiva + valor que e' URL/caminho, mesmo sem
        #    extensao (muita camera serve .cgi / .ashx / ?id=)
        if key_hinted and not _looks_like_marker(text):
            if text.startswith(("http://", "https://", "//", "/")) and " " not in text:
                preferred.append(text)

    def walk(node, key_hinted, depth):
        if depth > 6:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                hinted = any(h in str(k).lower() for h in _IMG_KEY_HINTS)
                if isinstance(v, (dict, list)):
                    walk(v, hinted or key_hinted, depth + 1)
                else:
                    consider(v, hinted or key_hinted)
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, (dict, list)):
                    walk(v, key_hinted, depth + 1)
                else:
                    consider(v, key_hinted)
        else:
            consider(node, key_hinted)

    walk(obj, False, _depth)

    for cand in preferred + fallback:
        url = cand
        if url.startswith("//"):
            url = "https:" + url
        elif not url.startswith(("http://", "https://")):
            if not base_url:
                continue
            url = urljoin(base_url, url)
        return url
    return None


def _index_image(cam_id, raw_image_url, headers=None):
    """Registra no cache do proxy de onde vem a imagem desta camera."""
    if not raw_image_url:
        return False
    with _image_cache_lock:
        entry = _image_cache.setdefault(cam_id, {})
        entry["source_url"] = raw_image_url
        if headers:
            entry["headers"] = headers
    return True


# ---------- Status de atualizacao (semaforo verde/amarelo/vermelho/cinza) ----------
#
# Cada camera ganha um "status_color" que responde: com que frequencia a
# imagem desta camera realmente muda?
#
#   verde    - atualiza em ate 60 s
#   amarelo  - atualiza entre 60 s e 5 min (meio lenta)
#   vermelho - atualiza em mais de 5 min (muito lenta)
#   cinza    - desligada / sem imagem: a propria fonte marcou a camera
#              como OFFLINE, ou ela nao tem URL de imagem, ou as ultimas
#              tentativas do proxy falharam
#
# O numero vem de dois lugares, nesta ordem:
#
#  1) MEDIDO de verdade. O proxy /api/cameras/image/<id> ja busca a
#     imagem a cada 5 s enquanto o painel da camera esta' aberto. Cada
#     busca gera um hash do conteudo; quando o hash muda, sabemos que a
#     imagem de origem foi atualizada e cronometramos o intervalo. Com
#     2+ intervalos medidos, usamos a mediana. E' o dado real daquela
#     camera, naquele momento.
#     Duas ressalvas honestas: (a) so mede enquanto alguem esta' olhando
#     a camera - por isso a maioria dos pinos usa a estimativa do item 2;
#     (b) como o proxy tem cache de 5 s, o intervalo medido tem
#     granularidade de ~5 s (nunca aponta "1 s" mesmo numa camera que
#     atualize mais rapido).
#
#  2) ESTIMADO pela fonte. Enquanto nao ha medicao, usamos a cadencia
#     tipica publicada/observada de cada portal. E' estimativa, nao
#     promessa - por isso a API devolve "refresh_measured": false nesse
#     caso, e o front mostra o valor como "~".
_NOMINAL_REFRESH_SECONDS = {
    "nyc": 2,            # NYC DOT: frames de transito, quase continuo
    "caltrans": 60,      # Caltrans CCTV: snapshot ~1 min
    "deldot": 60,        # DelDOT
    "cetsp": 60,         # CET-SP: ciclo de frames curtos
    "udot": 120,         # plataforma CARS/511
    "on511": 180,
    "ab511": 180,
    "fl511": 120,        # portais mapIcons
    "az511": 120,
    "ga511": 120,
    "nc511": 120,
    "nv511": 120,
    "id511": 120,
    "utmap": 120,
    "cav": 120,          # Clima ao Vivo
    "earthcam": 120,     # EarthCam Network
    "tfljam": 300,       # TfL JamCams: ~5 min por politica da TfL
    "wc24": 300,         # Webcamera24 (muitos feeds sao stream, nao snapshot)
    "swc": 300,          # SkylineWebcams (stream/token; thumbnail YT quando houver)
    "ocv": 300,          # OpenCCTV: cadencia desconhecida por camera
    "digitraffic": 600,  # weathercams finlandesas: ~10 min
}
_DEFAULT_REFRESH_SECONDS = 300
_REFRESH_GREEN_MAX = 60
_REFRESH_YELLOW_MAX = 300
# Tentativas seguidas falhando pelo proxy antes de pintar de cinza.
_PROBE_FAIL_LIMIT = 2
# So conta como "intervalo entre atualizacoes" se a busca anterior foi
# ha' pouco tempo. Sem isso, abrir a mesma camera de novo 20 min depois
# registraria um falso intervalo de 20 min.
_PROBE_CONTINUITY_SECONDS = 30

_probe_lock = threading.Lock()
_probe = {}  # camera_id -> {hash, last_change, last_fetch, last_ok, fails, intervals}


def _new_probe():
    return {
        "hash": None,
        "last_change": None,
        "last_fetch": 0.0,
        "last_ok": 0.0,
        "fails": 0,
        "intervals": [],
    }


def _record_probe_ok(camera_id, content):
    now = time.time()
    digest = hashlib.md5(content).hexdigest() if content else None
    with _probe_lock:
        st = _probe.setdefault(camera_id, _new_probe())
        continuous = st["last_fetch"] and (now - st["last_fetch"]) <= _PROBE_CONTINUITY_SECONDS
        st["last_fetch"] = now
        st["last_ok"] = now
        st["fails"] = 0
        if digest and digest != st["hash"]:
            if st["last_change"] and continuous:
                delta = now - st["last_change"]
                if 2 <= delta <= 1800:
                    st["intervals"] = (st["intervals"] + [delta])[-5:]
            st["hash"] = digest
            st["last_change"] = now


def _record_probe_fail(camera_id):
    with _probe_lock:
        st = _probe.setdefault(camera_id, _new_probe())
        st["fails"] += 1
        st["last_fetch"] = time.time()


def _apply_status(cam):
    """Anexa status_color / refresh_s / refresh_measured a uma camera.
    Muta o dict no lugar (barato: a listagem ja e' limitada por bbox)."""
    cam_id = str(cam.get("id") or "")
    prefix = cam_id.split("-", 1)[0]
    nominal = _NOMINAL_REFRESH_SECONDS.get(prefix, _DEFAULT_REFRESH_SECONDS)

    with _probe_lock:
        st = _probe.get(cam_id)
        fails = st["fails"] if st else 0
        intervals = list(st["intervals"]) if st else []

    measured = None
    if len(intervals) >= 2:
        ordered = sorted(intervals)
        measured = ordered[len(ordered) // 2]

    status_text = str(cam.get("status") or "").strip().upper()
    # "lite" = marker do indice OpenCCTV: a URL da imagem so e' resolvida
    # no clique, entao ausencia de image_url aqui nao significa desligada.
    # "lite" (OpenCCTV) resolve a URL no clique, entao conta como tendo
    # snapshot. "page_url" NAO conta: fora o Clima ao Vivo, nenhuma
    # fonte com page_url tem resolvedor no proxy - a pagina abre no
    # navegador, mas nao ha' imagem pra mostrar no globo.
    has_snapshot = bool(cam.get("image_url") or cam.get("lite"))
    has_page = bool(cam.get("page_url"))

    # Motivos DIFERENTES, antes embolados num booleano so'. Separar
    # importa porque so' os tres primeiros sao "nao da' pra ver nada":
    #   fonte_offline        a fonte disse que a camera esta fora do ar
    #   proxy_falhou         medido: as ultimas buscas falharam
    #   sem_imagem_indexada  nem imagem nem pagina - buraco de parser
    #   somente_pagina       a camera funciona, mas so' abrindo o site
    #                        da fonte (sem snapshot pra plotar)
    if status_text not in ("ONLINE", "UNKNOWN", ""):
        reason = "fonte_offline"
    elif fails >= _PROBE_FAIL_LIMIT:
        reason = "proxy_falhou"
    elif not has_snapshot and not has_page:
        reason = "sem_imagem_indexada"
    elif not has_snapshot:
        reason = "somente_pagina"
    else:
        reason = None

    # "somente_pagina" nao e' camera desligada, entao NAO vira cinza -
    # fica na cor da cadencia estimada, igual a qualquer outra sem
    # medicao. Pintar as 1.655 cameras do SkylineWebcams sem snapshot
    # de cinza seria mentir que estao fora do ar.
    refresh = measured if measured is not None else nominal
    if reason in ("fonte_offline", "proxy_falhou", "sem_imagem_indexada"):
        color = "cinza"
    elif refresh <= _REFRESH_GREEN_MAX:
        color = "verde"
    elif refresh <= _REFRESH_YELLOW_MAX:
        color = "amarelo"
    else:
        color = "vermelho"

    cam["status_color"] = color
    cam["status_reason"] = reason
    cam["refresh_s"] = int(round(refresh))
    cam["refresh_measured"] = measured is not None
    return cam


# ---------- Transmissao ao vivo (video de verdade, nao so' imagem) ----------
#
# PEDIDO: pra camera que so' tem page_url (ex.: 1.655 das 1.822 do
# SkylineWebcams, status_reason="somente_pagina"), o usuario nao quer
# um link que manda pro site de origem - quer o video tocando dentro do
# proprio WTX Global Monitor.
#
# Duas rotas, dependendo do que a fonte expoe:
#
#  1) YOUTUBE (confiavel, sem scraping): quando o page_url e' um link
#     do YouTube (~167 cameras do SkylineWebcams, e qualquer outra
#     fonte cujo link caia no mesmo padrao), basta montar o iframe
#     oficial de embed com o video id. E' o mecanismo documentado do
#     proprio YouTube, nao depende de raspar nada.
#
#  2) HLS EXTRAIDO DA PAGINA (as ~1.655 restantes do SkylineWebcams,
#     que tocam via um manifesto .m3u8 com token que expira - por isso
#     nao da' pra guardar a URL do stream de antemao, so' pegando a
#     pagina na hora que alguem abre a camera). O extractor busca a
#     pagina e procura, no HTML/JS bruto da resposta, uma URL .m3u8 em
#     texto puro.
#
#     LIMITE HONESTO: isso so' funciona se o site coloca essa URL
#     literalmente no HTML/JS que vem na resposta inicial. Se o site
#     monta essa URL DEPOIS, dentro do navegador, chamando a API deles
#     via JavaScript (tecnica comum bem pra dificultar justamente esse
#     tipo de extracao), a busca nao acha nada e a camera continua
#     caindo em "sem transmissao incorporavel" - o que e' o
#     comportamento correto (nao fingir que funciona), mas significa
#     que nem toda camera "somente_pagina" vira video.
#
#     NAO CONSEGUI TESTAR isso contra skylinewebcams.com,
#     webcamera24.com nem earthcam.com de verdade: a allowlist de rede
#     da minha sandbox bloqueia esses hosts. O regex cobre o padrao mais
#     comum (URL .m3u8 literal na pagina), mas so' quem rodar isso local
#     pode confirmar se bate com a pagina real. /api/diagnostics/ traz
#     um "stream_probe" que testa 1 camera de amostra e mostra achou ou
#     nao - cole o resultado aqui se quiser que eu ajuste o regex pro
#     formato real da pagina.
#
# SEGURANCA: so' busca pagina/stream de hosts numa lista pequena e
# fixa (_ALLOWED_STREAM_HOSTS). Sem isso, o endpoint de stream vira um
# "busque qualquer URL que eu mandar" (SSRF) - o usuario controla
# page_url via query string, entao a validacao de host e' obrigatoria
# antes de qualquer requests.get(). O proxy de segmento/playlist aninhado
# tambem so' aceita URL cujo host bate com o host ja' descoberto pra
# aquela camera (_host_matches), pelo mesmo motivo.

_ALLOWED_STREAM_HOSTS = (
    "skylinewebcams.com",
    "hd-auth.skylinewebcams.com",
    "webcamera24.com",
    "earthcam.com",
    "youtube.com",
    "youtu.be",
)
# URL absoluta clássica de .m3u8
_M3U8_URL_RE = re.compile(r"""https?://[^\s"'<>\\]+?\.m3u8[^\s"'<>\\]*""", re.I)
# SkylineWebcams coloca no JS do player: source:'livee.m3u8?a=TOKEN'
# (relative, com dois "e" de propósito — o endpoint real é live.m3u8).
_SKYLINE_REL_M3U8_RE = re.compile(
    r"""(?:url|source)\s*[:=]\s*['"](livee?\.m3u8\?a=[A-Za-z0-9]+)['"]""",
    re.I,
)
_SKYLINE_HD_AUTH_BASE = "https://hd-auth.skylinewebcams.com/"
# EarthCam embute no HTML: var json_base = { "cam": { "<cam_id>": {
#   "html5_streamingdomain": "https://videos-N.earthcam.com",
#   "html5_streampath": "/fecnetwork/....m3u8?t=...",
#   "liveon": "true", "live_type": "flashvideo", ... } } };
_EARTHCAM_JSON_BASE_RE = re.compile(
    r"""var\s+json_base\s*=\s*(\{.*?\});""",
    re.DOTALL,
)
_STREAM_CACHE_TTL = 600  # depois disso, tenta extrair de novo da pagina

_stream_lock = threading.Lock()
_stream_cache = {}  # camera_id -> {root_url, referer, origin, host, found_at}

# ---------- Bloqueio de canais YouTube privados/indisponiveis ----------
#
# Algumas cameras do SkylineWebcams/Webcamera24 apontam pra um video do
# YouTube que ficou privado, foi removido, ou teve o embed desabilitado
# pelo dono (o app mostra "Este video e privado / Faca login" dentro do
# iframe, sem nenhum jeito de fechar isso so' com CSS - o YouTube nao
# avisa a pagina pai por causa da mesma-origem).
#
# Nao da' pra checar TODAS as ~167 cameras (e futuras) de uma vez contra
# a API do YouTube a cada listagem do mapa - custaria uma chamada de rede
# por camera a cada /api/cameras/, o que e' caro e arriscaria rate limit
# do YouTube. Em vez disso: quando uma camera e' aberta (usuario clica,
# get_camera_stream_info roda), verificamos o video especifico via oEmbed
# (endpoint publico, sem chave). Se vier confirmado privado/removido, o
# video_id entra numa lista persistida em disco - a partir dai' essa
# camera some do mapa em toda listagem futura (get_cameras filtra usando
# essa lista, sem nenhuma chamada de rede extra, so' o video_id extraido
# do page_url por regex).
#
# IMPORTANTE: "privado" pode ser TEMPORARIO (o dono do canal reabre
# depois, foi so' uma instabilidade do lado do YouTube, etc.) - por isso
# o bloqueio NAO e' permanente. Cada entrada guarda quando foi confirmada
# privada pela ultima vez (last_checked); depois de _YT_RECHECK_SECONDS,
# ela e' testada de novo - tanto passivamente (sweep em background, ver
# _yt_recheck_sweep) quanto na hora (se alguem reabrir essa camera antes
# do sweep rodar). Se o oEmbed voltar a responder 200, a camera e'
# desbloqueada e volta a aparecer no mapa automaticamente.
_YT_BLOCKED_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "youtube_blocked.json"
)
_YT_RECHECK_SECONDS = 6 * 60 * 60  # reverifica um video bloqueado a cada 6h
_YT_PUBLIC_RECHECK_SECONDS = 3600  # video ja' confirmado publico: nao recheca por 1h

_yt_blocked_lock = threading.Lock()
_yt_blocked_meta = None  # dict carregado sob demanda: video_id -> {"blocked_at": ts, "last_checked": ts}
_yt_checked_ok = {}  # video_id -> timestamp da ultima confirmacao "publico" (so' em memoria)
_yt_sweep_thread_started = False


def _load_yt_blocked_meta():
    global _yt_blocked_meta
    with _yt_blocked_lock:
        if _yt_blocked_meta is not None:
            return _yt_blocked_meta
        try:
            with open(_YT_BLOCKED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("blocked", {})
            if isinstance(raw, list):
                # Formato antigo (so' lista de ids, sem timestamp) - migra
                # pro formato novo tratando como bloqueado "agora", entao
                # ainda passa pelo ciclo normal de reverificacao.
                now = time.time()
                raw = {vid: {"blocked_at": now, "last_checked": now} for vid in raw}
            _yt_blocked_meta = raw
        except FileNotFoundError:
            _yt_blocked_meta = {}
        except Exception:
            logger.warning("YouTube blocklist: arquivo em disco corrompido/ilegivel, comecando vazio")
            _yt_blocked_meta = {}
        return _yt_blocked_meta


def _atomic_json_write(path, payload):
    """Escrita atomica com tolerância a WinError 32 (arquivo em uso no Windows)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.{int(time.time() * 1000)}.tmp"
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
            os.replace(tmp_path, path)
            return
        except (PermissionError, OSError) as exc:
            last_err = exc
            time.sleep(0.05 * (attempt + 1))
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        logger.warning("Escrita atomica falhou (%s); gravado direto em %s", last_err, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _save_yt_blocked_meta():
    try:
        _atomic_json_write(_YT_BLOCKED_FILE, {"blocked": _yt_blocked_meta})
    except Exception:
        logger.exception("YouTube blocklist: falha ao salvar em disco (%s)", _YT_BLOCKED_FILE)


def _is_youtube_blocked(video_id):
    return video_id in _load_yt_blocked_meta()


def _needs_recheck(video_id):
    meta = _load_yt_blocked_meta()
    entry = meta.get(video_id)
    if not entry:
        return False
    return (time.time() - entry.get("last_checked", 0)) >= _YT_RECHECK_SECONDS


def _recently_confirmed_public(video_id):
    with _yt_blocked_lock:
        ts = _yt_checked_ok.get(video_id)
    return ts is not None and (time.time() - ts) < _YT_PUBLIC_RECHECK_SECONDS


def _remember_public(video_id):
    with _yt_blocked_lock:
        _yt_checked_ok[video_id] = time.time()


def _mark_youtube_blocked(video_id):
    """Marca (ou atualiza) o bloqueio - grava sempre o momento desta
    confirmacao em last_checked, pra' contar a janela ate' a proxima
    reverificacao a partir de AGORA, nao da primeira vez que foi visto."""
    meta = _load_yt_blocked_meta()
    now = time.time()
    with _yt_blocked_lock:
        entry = meta.get(video_id)
        if entry:
            entry["last_checked"] = now
        else:
            meta[video_id] = {"blocked_at": now, "last_checked": now}
        _save_yt_blocked_meta()
    logger.warning("YouTube: video %s confirmado privado/indisponivel - camera removida do mapa (recheck em ~%dh)",
                    video_id, _YT_RECHECK_SECONDS // 3600)


def _unmark_youtube_blocked(video_id):
    meta = _load_yt_blocked_meta()
    with _yt_blocked_lock:
        existed = meta.pop(video_id, None) is not None
        if existed:
            _save_yt_blocked_meta()
    if existed:
        logger.info("YouTube: video %s voltou a ficar publico - camera de volta ao mapa", video_id)


def _check_youtube_embeddable(video_id):
    """
    Consulta o oEmbed publico do YouTube (sem chave) pra' saber se o
    video ainda e' publico/incorporavel.
      True  -> publico, pode tocar normalmente (ou voltar a aparecer,
               se estava bloqueado).
      False -> CONFIRMADO privado/removido/embed desabilitado agora
               (oEmbed responde 401/403/404). Bloqueio TEMPORARIO - sera'
               testado de novo depois de _YT_RECHECK_SECONDS.
      None  -> nao foi possivel confirmar agora (timeout, erro de rede,
               rate limit do YouTube/429, 5xx) - trata como "nao sabemos
               ainda", NUNCA bloqueia nem desbloqueia por causa de uma
               falha temporaria da checagem em si.
    """
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            headers=DEFAULT_HEADERS,
            timeout=5,
        )
    except Exception as exc:
        logger.warning("YouTube oEmbed: falha na verificacao de %s: %s", video_id, exc)
        return None

    if resp.status_code == 200:
        return True
    if resp.status_code in (401, 403, 404):
        return False
    logger.warning("YouTube oEmbed: status inesperado (%d) para %s - nao bloqueando nem desbloqueando", resp.status_code, video_id)
    return None


def _yt_recheck_sweep():
    """Reverifica em segundo plano as cameras bloqueadas ha' mais de
    _YT_RECHECK_SECONDS, uma de cada vez com uma pequena pausa entre
    elas (gentileza com a API do YouTube - nao dispara tudo de uma vez).
    Quem voltou a ficar publica e' desbloqueada automaticamente."""
    meta = _load_yt_blocked_meta()
    with _yt_blocked_lock:
        candidates = [vid for vid, entry in meta.items() if _needs_recheck(vid)]
    if not candidates:
        return
    logger.info("YouTube blocklist: reverificando %d video(s) bloqueado(s)", len(candidates))
    for vid in candidates:
        embeddable = _check_youtube_embeddable(vid)
        if embeddable is True:
            _unmark_youtube_blocked(vid)
            _remember_public(vid)
        elif embeddable is False:
            _mark_youtube_blocked(vid)  # continua bloqueado, so' atualiza last_checked
        # None (falha na checagem): nao mexe - tenta de novo no proximo sweep
        time.sleep(0.3)


def _background_yt_sweep_loop():
    while True:
        time.sleep(_YT_RECHECK_SECONDS)
        try:
            _yt_recheck_sweep()
        except Exception:
            logger.exception("Erro no sweep de reverificacao da blocklist do YouTube")


def _ensure_yt_sweep_thread():
    global _yt_sweep_thread_started
    with _yt_blocked_lock:
        if _yt_sweep_thread_started:
            return
        _yt_sweep_thread_started = True
    threading.Thread(target=_background_yt_sweep_loop, daemon=True, name="yt-recheck-sweep").start()


def _host_allowed(url, allowed=_ALLOWED_STREAM_HOSTS):
    try:
        host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in allowed)


def _host_matches(url, allowed_host):
    """Aceita o host exato ou qualquer subdomínio do mesmo domínio base
    (ex.: hd-auth.skylinewebcams.com bate com skylinewebcams.com;
    videos-3.earthcam.com bate com earthcam.com)."""
    try:
        host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return False
    allowed = allowed_host.lower().split(":")[0]
    if host == allowed:
        return True
    # Famílias de CDN das fontes que usamos
    for root in ("skylinewebcams.com", "earthcam.com"):
        if allowed.endswith(root) and host.endswith(root):
            return True
    if host.endswith("." + allowed):
        return True
    return False


def _normalize_skyline_m3u8(candidate, page_url):
    """
    Converte o trecho que o SkylineWebcams embute no HTML
    (livee.m3u8?a=TOKEN ou live.m3u8?a=TOKEN, relativo) no URL
    absoluto e funcional do CDN de auth.
    """
    if not candidate:
        return None
    candidate = candidate.replace("&amp;", "&").strip()
    # Trap intencional do site: "livee" (dois e) → usar "live"
    if "livee.m3u8" in candidate.lower():
        candidate = re.sub(r"livee\.m3u8", "live.m3u8", candidate, flags=re.I)
    if candidate.startswith("http://") or candidate.startswith("https://"):
        # Já absoluto — só garante o domínio de auth se for skyline
        if "skylinewebcams.com" in candidate and "hd-auth." not in candidate:
            # raro; mantém o que veio
            return candidate
        return candidate
    # Relativo → junta no CDN de auth
    return urljoin(_SKYLINE_HD_AUTH_BASE, candidate.lstrip("/"))


def _extract_earthcam_hls(html, page_url):
    """
    Extrai a URL HLS ao vivo de uma página EarthCam.

    O site embute `var json_base = {...}` com, para cada câmera da página:
      html5_streamingdomain  (ex. https://videos-3.earthcam.com)
      html5_streampath       (ex. /fecnetwork/9974.flv/playlist.m3u8?t=...)
      liveon / live_type / defaulttab

    Preferimos a câmera indicada por ?cam= na URL; senão a primeira
    que estiver liveon=true e com path HLS.
    """
    m = _EARTHCAM_JSON_BASE_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    cams = data.get("cam") if isinstance(data, dict) else None
    if not isinstance(cams, dict) or not cams:
        return None

    # Preferir o ?cam= da URL da página
    cam_name = None
    try:
        qs = parse_qs(urlsplit(page_url).query)
        cam_name = (qs.get("cam") or [None])[0]
    except Exception:
        cam_name = None

    candidates = []
    if cam_name and cam_name in cams:
        candidates.append(cams[cam_name])
    # Depois qualquer outra live
    for name, c in cams.items():
        if name == cam_name:
            continue
        if isinstance(c, dict):
            candidates.append(c)

    for c in candidates:
        if not isinstance(c, dict):
            continue
        # Preferir só streams ao vivo (quando o campo existe)
        liveon = str(c.get("liveon") or "").lower()
        if liveon and liveon not in ("true", "1", "yes"):
            continue
        domain = (c.get("html5_streamingdomain") or "").strip()
        path = (c.get("html5_streampath") or "").strip()
        if not domain or not path:
            continue
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not domain.startswith("http"):
            domain = "https://" + domain.lstrip("/")
        return domain.rstrip("/") + (path if path.startswith("/") else "/" + path)

    return None


def get_camera_stream_info(camera_id, page_url, force=False):
    """
    Descobre como tocar esta camera ao vivo. Devolve um destes formatos:
      {"type": "youtube", "video_id": "..."}
      {"type": "hls", "playlist_url": "/api/cameras/hls/<id>/playlist.m3u8"}
      {"type": "none", "reason": "...", "page_url": "..."}

    force=True pula o cache por tempo e refaz a extracao na pagina AGORA.
    Usado por get_hls_playlist() quando o token ja' descoberto comecou a
    dar 403/404: o token pode morrer bem antes do TTL normal
    (_STREAM_CACHE_TTL so' limita quanto tempo ficamos SEM checar de
    novo quando tudo esta' indo bem - nao e' uma promessa de validade
    do token). Sem o `force`, esta funcao devolveria o mesmo tipo "hls"
    cacheado, mesmo sabendo (pelo 403 que acabou de acontecer) que ele
    esta' morto - um loop de falha sem saida.
    """
    if not page_url or not _host_allowed(page_url):
        return {"type": "none", "reason": "fonte_nao_permitida"}

    yt_id = _youtube_id_from_url(page_url)
    if yt_id:
        if _is_youtube_blocked(yt_id):
            if not _needs_recheck(yt_id):
                return {"type": "none", "reason": "youtube_privado", "page_url": page_url}
            # Prazo de reverificacao vencido: testa de novo agora (o
            # usuario abrindo a camera nao precisa esperar o proximo
            # sweep em background pra' descobrir que ela voltou).
            embeddable = _check_youtube_embeddable(yt_id)
            if embeddable is True:
                _unmark_youtube_blocked(yt_id)
                _remember_public(yt_id)
                return {"type": "youtube", "video_id": yt_id}
            if embeddable is False:
                _mark_youtube_blocked(yt_id)
            return {"type": "none", "reason": "youtube_privado", "page_url": page_url}

        # Video nao bloqueado: so' checa contra o YouTube quando ainda
        # nao foi confirmado publico ha' menos de 1h - poupa uma chamada
        # de rede extra em toda reabertura da mesma camera ja' sabida boa.
        if not _recently_confirmed_public(yt_id):
            embeddable = _check_youtube_embeddable(yt_id)
            if embeddable is False:
                _mark_youtube_blocked(yt_id)
                return {"type": "none", "reason": "youtube_privado", "page_url": page_url}
            if embeddable is True:
                _remember_public(yt_id)
        return {"type": "youtube", "video_id": yt_id}

    with _stream_lock:
        cached = _stream_cache.get(camera_id)
    if not force and cached and (time.time() - cached["found_at"]) < _STREAM_CACHE_TTL:
        return {"type": "hls", "playlist_url": f"/api/cameras/hls/{camera_id}/playlist.m3u8"}

    try:
        # Paginas HTML de webcam (Skyline/EarthCam) precisam de Accept de
        # browser; DEFAULT_HEADERS pede application/json (APIs).
        page_headers = {
            **DEFAULT_HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": page_url,
        }
        resp = requests.get(page_url, headers=page_headers, timeout=12)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        return {"type": "none", "reason": f"pagina_inacessivel: {type(exc).__name__}", "page_url": page_url}

    # BUGFIX: muitas paginas embutem a URL do HLS dentro de um blob
    # JSON/JS no <script>, com as barras escapadas ("https:\/\/...
    # \/stream.m3u8"). A regex exige "://" literal, entao nunca batia
    # nesse caso e a camera caia sempre no fallback de link - mesmo
    # quando a URL do stream estava logo ali no HTML. Desescapar "\/"
    # antes de procurar resolve isso sem arriscar falso positivo (so'
    # afeta a copia usada pra busca, nao o HTML original).
    unescaped_html = html.replace("\\/", "/")


    # Webcamera24: maioria é YouTube embed na página /camera/...
    if "webcamera24.com" in page_url.lower():
        yt = re.search(
            r"youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]{6,})",
            unescaped_html,
            re.I,
        )
        if yt:
            return {"type": "youtube", "video_id": yt.group(1)}
        enrich = _load_webcamera24_enrichment()
        m_cid = re.search(r"[?&]camera=([a-f0-9]{24})", page_url)
        meta = enrich.get(m_cid.group(1)) if m_cid else None
        if not meta:
            for row in enrich.values():
                if row.get("url") and page_url.rstrip("/") in str(row.get("url")).rstrip("/"):
                    meta = row
                    break
                if row.get("url") and str(row.get("url")).rstrip("/") in page_url.rstrip("/"):
                    meta = row
                    break
        if meta and meta.get("youtube_id"):
            return {"type": "youtube", "video_id": meta["youtube_id"]}

    root_url = None

    # 1) SkylineWebcams: padrão relativo source:'livee.m3u8?a=TOKEN'
    if "skylinewebcams.com" in page_url.lower():
        m_sw = _SKYLINE_REL_M3U8_RE.search(unescaped_html) or _SKYLINE_REL_M3U8_RE.search(html)
        if m_sw:
            root_url = _normalize_skyline_m3u8(m_sw.group(1), page_url)

    # 2) EarthCam: var json_base = { cam: { id: { html5_streamingdomain, html5_streampath } } }
    if not root_url and "earthcam.com" in page_url.lower():
        root_url = _extract_earthcam_hls(html, page_url)

    # 3) Qualquer URL .m3u8 absoluta (outras fontes / fallback)
    if not root_url:
        m = _M3U8_URL_RE.search(unescaped_html) or _M3U8_URL_RE.search(html)
        if m:
            candidate = m.group(0).replace("&amp;", "&")
            if "skylinewebcams.com" in page_url.lower():
                root_url = _normalize_skyline_m3u8(candidate, page_url)
            else:
                root_url = candidate

    if not root_url:
        return {"type": "none", "reason": "sem_m3u8_no_html", "page_url": page_url}

    origin = f"{urlsplit(page_url).scheme}://{urlsplit(page_url).netloc}"
    with _stream_lock:
        _stream_cache[camera_id] = {
            "root_url": root_url,
            "referer": page_url,
            "origin": origin,
            "host": urlsplit(root_url).netloc,
            "found_at": time.time(),
        }
    return {"type": "hls", "playlist_url": f"/api/cameras/hls/{camera_id}/playlist.m3u8"}


def _rewrite_m3u8(camera_id, manifest_url, text):
    """Reescreve um manifesto HLS pra que toda URL (playlist aninhada,
    segmento, chave de criptografia) passe pelo nosso proxy - senao o
    navegador tentaria buscar direto no CDN de origem e provavelmente
    levaria 403 (mesma protecao de hotlink que tinhamos pras imagens)."""
    def to_proxy(abs_u):
        if ".m3u8" in abs_u.lower():
            return f"/api/cameras/hls/{camera_id}/playlist.m3u8?u={quote(abs_u, safe='')}"
        return f"/api/cameras/hls/{camera_id}/segment?u={quote(abs_u, safe='')}"

    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        if stripped.startswith("#"):
            def _sub_uri(mm):
                return f'URI="{to_proxy(urljoin(manifest_url, mm.group(1)))}"'
            out_lines.append(re.sub(r'URI="([^"]+)"', _sub_uri, line))
            continue
        out_lines.append(to_proxy(urljoin(manifest_url, stripped)))
    return "\n".join(out_lines) + "\n"


def get_hls_playlist(camera_id, override_url=None):
    with _stream_lock:
        cached = _stream_cache.get(camera_id)
    if not cached:
        return None, None
    if override_url:
        if not _host_matches(override_url, cached["host"]):
            return None, None
        manifest_url = override_url
    else:
        manifest_url = cached["root_url"]

    headers = {**DEFAULT_HEADERS, "Referer": cached["referer"], "Origin": cached["origin"]}
    try:
        resp = requests.get(manifest_url, headers=headers, timeout=10)
        resp.raise_for_status()
        text = resp.text
    except Exception:
        if override_url:
            return None, None
        # Token provavelmente expirou - forca reextracao ignorando cache.
        info = get_camera_stream_info(camera_id, cached["referer"], force=True)
        if info.get("type") != "hls":
            return None, None
        with _stream_lock:
            cached = _stream_cache.get(camera_id)
        if not cached:
            return None, None
        manifest_url = cached["root_url"]
        try:
            resp = requests.get(
                manifest_url,
                headers={**DEFAULT_HEADERS, "Referer": cached["referer"], "Origin": cached["origin"]},
                timeout=10,
            )
            resp.raise_for_status()
            text = resp.text
        except Exception:
            return None, None

    return _rewrite_m3u8(camera_id, manifest_url, text), "application/vnd.apple.mpegurl"


def get_hls_segment(camera_id, segment_url):
    with _stream_lock:
        cached = _stream_cache.get(camera_id)
    if not cached or not segment_url or not _host_matches(segment_url, cached["host"]):
        return None, None
    headers = {**DEFAULT_HEADERS, "Referer": cached["referer"], "Origin": cached["origin"]}
    try:
        resp = requests.get(segment_url, headers=headers, timeout=12)
        resp.raise_for_status()
    except Exception:
        return None, None
    return resp.content, resp.headers.get("Content-Type") or "video/mp2t"


def get_stream_probe_sample(cameras):
    """Roda a extracao de HLS contra 1 camera de amostra (a 1a
    'somente_pagina' sem youtube que aparecer) pra' o /api/diagnostics/.
    E' como eu vejo, sem rede pra esses hosts, se o regex bate com a
    pagina real de quem estiver rodando isso local."""
    for cam in cameras:
        page_url = cam.get("page_url")
        if not page_url or cam.get("image_url") or _youtube_id_from_url(page_url):
            continue
        if not _host_allowed(page_url):
            continue
        info = get_camera_stream_info(f"diag-{cam.get('id')}", page_url)
        return {"camera_id": cam.get("id"), "source": cam.get("source"), "page_url": page_url, "result": info}
    return {"note": "nenhuma camera 'somente pagina' elegivel na amostra atual"}


def get_camera_index_report():
    """
    Quantas cameras de CADA fonte tem imagem indexada, e por que as
    outras estao cinza. E' o relatorio que responde "esta fonte nao foi
    indexada direito" sem chutar: se uma fonte aparece com
    sem_imagem_indexada alto, o problema e' o parser dela aqui, nao as
    cameras estarem fora do ar.
    """
    with _cache_lock:
        base = list(_cache.get("data") or [])

    report = {}
    for cam in base:
        src = cam.get("source") or "?"
        row = report.setdefault(src, {
            "total": 0,
            "com_imagem": 0,
            "somente_pagina": 0,
            "fonte_offline": 0,
            "sem_imagem_indexada": 0,
            "proxy_falhou": 0,
        })
        _apply_status(cam)
        row["total"] += 1
        reason = cam.get("status_reason")
        if reason is None:
            row["com_imagem"] += 1
        else:
            row[reason] = row.get(reason, 0) + 1

    for row in report.values():
        row["pct_com_imagem"] = (
            round(100.0 * row["com_imagem"] / row["total"], 1) if row["total"] else 0.0
        )
    return dict(sorted(report.items(), key=lambda kv: -kv[1]["total"]))


_IMAGE_CACHE_TTL_SECONDS = 5  # imagens atualizam a cada poucos segundos nas fontes
_image_cache_lock = threading.Lock()
_image_cache = {}  # camera_id -> {"timestamp": ..., "content": ..., "content_type": ...}

_cache = {
    "timestamp": 0,
    "data": None,
    "source": "demo",
}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 10 * 60
_cache_refreshing = False  # evita disparar N refreshes em paralelo

# Persistencia em disco do cache base: sem isso, TODO restart do processo
# (deploy, crash, reinicio do host) comeca com cache 100% frio e o
# PRIMEIRO carregamento do app fica preso esperando o fetch completo das
# ~30 fontes (ate 18s) + OpenCCTV. Salvando o ultimo resultado bom em
# disco, o boot consegue servir esses dados (mesmo que velhos) na hora,
# e so' depois atualiza em segundo plano - ninguem espera o fetch frio.
_BASE_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "camera_base_cache.json"
)


def _load_base_cache_from_disk():
    try:
        with open(_BASE_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        data = payload.get("data")
        source = payload.get("source") or ""
        if isinstance(data, list) and data:
            with _cache_lock:
                _cache["data"] = data
                _cache["source"] = source
                # timestamp 0 => considerado vencido, dispara refresh em
                # background na primeira chamada real, mas ja' serve algo.
                _cache["timestamp"] = 0
                _cache["base_only"] = True
            logger.info("camera_base_cache: %d cameras carregadas do disco (boot a quente)", len(data))
            return True
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("camera_base_cache: falha ao carregar cache do disco")
    return False


def _save_base_cache_to_disk(data, source):
    try:
        _atomic_json_write(_BASE_CACHE_FILE, {"data": data, "source": source, "saved_at": time.time()})
    except Exception:
        logger.exception("camera_base_cache: falha ao salvar cache em disco")



def _cap_source_list(cameras):
    """Amostra uniforme se a fonte passar de MAX_CAMERAS_PER_SOURCE."""
    if not MAX_CAMERAS_PER_SOURCE or len(cameras) <= MAX_CAMERAS_PER_SOURCE:
        return cameras
    step = len(cameras) / MAX_CAMERAS_PER_SOURCE
    return [cameras[int(i * step)] for i in range(MAX_CAMERAS_PER_SOURCE)]



def _arcgis_query_all(url, out_fields="*", page_size=1000, timeout=30, extra_params=None):
    """
    Pagina FeatureServer/MapServer em lotes (resultOffset).

    1) tenta returnCountOnly para saber o total
    2) se total conhecido, busca offsets em paralelo
    3) senao, pagina sequencial ate exceededTransferLimit=false
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(offset):
        params = {
            "where": "1=1",
            "outFields": out_fields,
            "returnGeometry": "true",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        if extra_params:
            params.update(extra_params)
        resp = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json()
        if raw.get("error"):
            raise ValueError(f"ArcGIS erro: {raw['error']}")
        return raw.get("features") or [], bool(raw.get("exceededTransferLimit"))

    total = None
    try:
        cparams = {"where": "1=1", "returnCountOnly": "true", "f": "json"}
        if extra_params:
            cparams.update({k: v for k, v in extra_params.items() if k not in cparams})
        cr = requests.get(url, params=cparams, headers=DEFAULT_HEADERS, timeout=min(15, timeout))
        if cr.ok:
            total = (cr.json() or {}).get("count")
            total = int(total) if total is not None else None
    except Exception:
        total = None

    features = []
    if total and total > 0:
        offsets = list(range(0, total, page_size))
        workers = min(6, len(offsets))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(_one, off): off for off in offsets}
            by_off = {}
            for fut in as_completed(futs):
                off = futs[fut]
                try:
                    batch, _ = fut.result()
                    by_off[off] = batch
                except Exception as exc:
                    logger.warning("ArcGIS lote offset=%s falhou: %s", off, exc)
            for off in sorted(by_off):
                features.extend(by_off[off])
        return features

    # fallback sequencial
    offset = 0
    while offset <= 100000:
        try:
            batch, exceeded = _one(offset)
        except Exception as exc:
            if offset == 0:
                raise
            logger.warning("ArcGIS sequencial offset=%s: %s", offset, exc)
            break
        if not batch:
            break
        features.extend(batch)
        if not exceeded and len(batch) < page_size:
            break
        offset += len(batch)
    return features


def _parse_latlng_geography(lat_lng):
    """Extrai lat/lon de latLng.geography.wellKnownText 'POINT (lon lat)'."""
    if not isinstance(lat_lng, dict):
        return None, None
    geo = lat_lng.get("geography") or lat_lng
    wkt = geo.get("wellKnownText") if isinstance(geo, dict) else None
    if isinstance(wkt, str) and "POINT" in wkt.upper():
        m = re.search(r"POINT\s*\(\s*([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)\s*\)", wkt, re.I)
        if m:
            try:
                lon, lat = float(m.group(1)), float(m.group(2))
                return lat, lon
            except ValueError:
                pass
    # alguns sites mandam latitude/longitude direto
    try:
        if "latitude" in lat_lng and "longitude" in lat_lng:
            return float(lat_lng["latitude"]), float(lat_lng["longitude"])
    except (TypeError, ValueError, KeyError):
        pass
    return None, None


def _fetch_cars_listdata_cameras(site_root, id_prefix, source_label):
    """
    Castle Rock 511 — lista completa via POST /List/GetData/Cameras.

    O servidor ignora length>100 e devolve no maximo 100 linhas por
    request. Estrategia em lotes:
      1) 1a pagina: descobre recordsTotal
      2) demais paginas (start=100,200,...) em paralelo (ThreadPool)
      3) merge + dedupe por id
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    site_root = site_root.rstrip("/")
    list_url = f"{site_root}/List/GetData/Cameras"
    headers = {
        **DEFAULT_HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{site_root}/List/Cameras",
        "Origin": site_root,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    page = 100  # hard cap do Castle Rock — length maior nao aumenta o lote
    img_headers = {**DEFAULT_HEADERS, "Referer": site_root + "/", "Accept": "image/*,*/*"}

    def _fetch_page(start):
        form = {
            "draw": "1",
            "start": str(start),
            "length": str(page),
            "search[value]": "",
            "search[regex]": "false",
        }
        resp = requests.post(list_url, data=form, headers=headers, timeout=35)
        resp.raise_for_status()
        if not (resp.text or "").strip():
            return start, [], 0
        data = resp.json()
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            rows = []
        try:
            total = int(data.get("recordsTotal") or 0)
        except (TypeError, ValueError):
            total = 0
        return start, rows, total

    def _rows_to_cameras(rows, seen):
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_id = row.get("id") or row.get("DT_RowId") or row.get("sourceId")
            if raw_id is None:
                continue
            cam_id = f"{id_prefix}-{raw_id}"
            if cam_id in seen:
                continue
            seen.add(cam_id)

            lat, lon = _parse_latlng_geography(row.get("latLng") or {})
            if lat is None or lon is None:
                continue

            roadway = (row.get("roadway") or "").strip()
            direction = (row.get("direction") or "").strip()
            location = (row.get("location") or row.get("nickname") or "").strip()
            bits = [b for b in (roadway, direction, location) if b]
            title = " · ".join(bits) if bits else f"{source_label} #{raw_id}"

            raw_image = None
            images = row.get("images") or []
            if isinstance(images, list):
                for im in images:
                    if not isinstance(im, dict) or im.get("disabled") or im.get("blocked"):
                        continue
                    u = im.get("imageUrl") or im.get("url")
                    if u:
                        if u.startswith("/"):
                            u = site_root + u
                        elif not u.startswith("http"):
                            u = urljoin(site_root + "/", u)
                        raw_image = u
                        break

            if raw_image:
                _index_image(cam_id, raw_image, headers=img_headers)

            out.append({
                "id": cam_id,
                "location": title,
                "lat": lat,
                "lon": lon,
                "status": "ONLINE" if row.get("visible", True) else "OFFLINE",
                "source": source_label,
                "image_url": f"/api/cameras/image/{cam_id}" if raw_image else None,
            })
        return out

    # --- lote 0: descobre total ---
    try:
        _, first_rows, total = _fetch_page(0)
    except Exception as exc:
        raise ValueError(f"{id_prefix}: List/GetData falhou na 1a pagina: {exc}") from exc

    seen = set()
    cameras = _rows_to_cameras(first_rows, seen)

    if total and total > page:
        starts = list(range(page, total, page))
        # paraleliza lotes restantes (ate 8 workers — evita martelar o 511)
        workers = min(8, len(starts))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fetch_page, s): s for s in starts}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    _, rows, _ = fut.result()
                except Exception as exc:
                    logger.warning("%s: falha no lote start=%s: %s", id_prefix, s, exc)
                    continue
                cameras.extend(_rows_to_cameras(rows, seen))
    elif not total and first_rows:
        # total desconhecido: continua sequencial a partir de 100
        start = page
        while start <= 20000:
            try:
                _, rows, _ = _fetch_page(start)
            except Exception as exc:
                logger.warning("%s: falha no lote start=%s: %s", id_prefix, start, exc)
                break
            if not rows:
                break
            cameras.extend(_rows_to_cameras(rows, seen))
            if len(rows) < page:
                break
            start += page

    if not cameras:
        raise ValueError(f"{id_prefix}: List/GetData vazio (total anunciado={total})")
    logger.info("%s: List/GetData catalogou %d cameras (anunciado=%s)", id_prefix, len(cameras), total)
    return _cap_source_list(cameras)


def _fetch_nyc_cameras():
    resp = requests.get(NYC_CAMERAS_URL, headers=DEFAULT_HEADERS, timeout=8)
    resp.raise_for_status()
    raw = resp.json()

    cameras = []
    for cam in raw:
        lat, lon = cam.get("latitude"), cam.get("longitude")
        if lat is None or lon is None:
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue

        cam_id = f"nyc-{cam.get('id')}"
        # Fallback: se o campo dedicado sumir/renomear, procura a imagem
        # em qualquer lugar do registro em vez de marcar a camera como
        # sem imagem (que o semaforo pintaria de cinza).
        raw_image_url = cam.get("imageUrl") or _extract_image_url(cam, base_url=NYC_CAMERAS_URL)
        cameras.append({
            "id": cam_id,
            "location": cam.get("name") or "NYC DOT Camera",
            "lat": lat,
            "lon": lon,
            # A API ja devolveu isOnline como bool, como "true" e como
            # "Y" em momentos diferentes. Comparar so' com "true"
            # marcava camera boa como OFFLINE.
            "status": "OFFLINE" if _is_falsy_flag(cam.get("isOnline")) else "ONLINE",
            "source": "NYC DOT (nyctmc.org)",
            # Nao expor a URL crua da NYC pro navegador (leva a 403 por
            # hotlink-protection). O front sempre usa nossa rota de proxy.
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
        })
        if raw_image_url:
            with _image_cache_lock:
                _image_cache.setdefault(cam_id, {})
                _image_cache[cam_id]["source_url"] = raw_image_url
                _image_cache[cam_id]["headers"] = _NYC_IMAGE_HEADERS

    if not cameras:
        raise ValueError("Lista de cameras da NYC vazia")


    return cameras


def _fetch_caltrans_cameras():
    """
    Caltrans CCTV via ArcGIS FeatureServer aberto (sem chave), 12
    distritos rodoviarios da California cobertos numa unica consulta.
    Schema confirmado (ver docstring do modulo): latitude/longitude,
    locationName, district, route, inService, currentImageURL.
    """
    # ArcGIS limita ~2000 por request — paginar ate o total (~2936).
    features = _arcgis_query_all(CALTRANS_CCTV_URL, out_fields="*", page_size=1000, timeout=25)
    cameras = []
    for feat in features:
        attrs = feat.get("attributes") or {}
        geom = feat.get("geometry") or {}
        lat = attrs.get("latitude")
        lon = attrs.get("longitude")
        if lat in (None, "", 0) or lon in (None, "", 0):
            lat, lon = geom.get("y"), geom.get("x")
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue

        raw_id = _first_id(attrs.get("index_"), attrs.get("OBJECTID"), attrs.get("objectid"))
        cam_id = f"caltrans-{raw_id if raw_id is not None else f'{lat:.5f}_{lon:.5f}'}"
        name = attrs.get("locationName") or attrs.get("nearbyPlace") or "Caltrans CCTV"
        district = attrs.get("district")
        route = attrs.get("route")
        label_bits = [str(name)]
        if district:
            label_bits.append(f"D{district}")
        if route:
            label_bits.append(f"Rota {route}")
        in_service = str(attrs.get("inService") or "").strip().upper()
        raw_image_url = attrs.get("currentImageURL") or _extract_image_url(attrs)

        cameras.append({
            "id": cam_id,
            "location": " · ".join(label_bits),
            "lat": lat,
            "lon": lon,
            "status": "OFFLINE" if in_service in ("N", "NO", "FALSE", "0") else "ONLINE",
            "source": "Caltrans CCTV (dot.ca.gov)",
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
        })
        if raw_image_url:
            with _image_cache_lock:
                _image_cache.setdefault(cam_id, {})
                _image_cache[cam_id]["source_url"] = raw_image_url
                _image_cache[cam_id]["headers"] = _CALTRANS_IMAGE_HEADERS

    if not cameras:
        raise ValueError("Caltrans CCTV nao retornou nenhuma camera")


    return cameras


def _fetch_cars511_cameras(base_url, source_label, id_prefix):
    """
    Fetcher generico para qualquer implantacao da plataforma publica
    "CARS/511" (Castle Rock ATIS) - o mesmo backend por tras de
    dezenas de sites "511" de estados dos EUA e provincias do Canada,
    todos com o endpoint /api/v2/get/cameras e schema JSON identico.
    Usado aqui para Utah (EUA), Ontario e Alberta (Canada) - so muda a
    URL base e o rotulo da fonte.
    """
    resp = requests.get(base_url, headers=DEFAULT_HEADERS, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    cameras = []
    for cam in raw:
        lat, lon = cam.get("Latitude"), cam.get("Longitude")
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            continue

        views = cam.get("Views") or []
        view = next((v for v in views if str(v.get("Status")).lower() == "enabled"), None)
        if not view:
            continue
        raw_image_url = view.get("Url")
        if not raw_image_url:
            continue

        cam_id = f"{id_prefix}-{cam.get('Id')}"
        location = cam.get("Location") or cam.get("Roadway") or source_label
        cameras.append({
            "id": cam_id,
            "location": str(location),
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": source_label,
            "image_url": f"/api/cameras/image/{cam_id}",
        })
        with _image_cache_lock:
            _image_cache.setdefault(cam_id, {})
            _image_cache[cam_id]["source_url"] = raw_image_url
            _image_cache[cam_id]["headers"] = _CALTRANS_IMAGE_HEADERS

    if not cameras:
        raise ValueError(f"{source_label} nao retornou nenhuma camera")


    return cameras


def _fetch_udot_cameras():
    return _fetch_cars511_cameras(UDOT_CAMERAS_URL, "UDOT Traffic (Utah, EUA)", "udot")


def _fetch_ontario511_cameras():
    return _fetch_cars511_cameras(ONTARIO511_CAMERAS_URL, "Ontario 511 (Canadá)", "on511")


def _fetch_alberta511_cameras():
    return _fetch_cars511_cameras(ALBERTA511_CAMERAS_URL, "Alberta 511 (Canadá)", "ab511")


def _fetch_tfl_jamcam_cameras():
    """
    TfL JamCams (Londres, Reino Unido) - API publica da Transport for
    London. Cada "Place" tem lat/lon no nivel raiz e a URL da imagem
    dentro de additionalProperties (key == "imageUrl"), apontando
    direto pro S3 da TfL - sem protecao de hotlink conhecida, mas
    passa pelo mesmo proxy por consistencia/CORS.
    """
    resp = requests.get(TFL_JAMCAM_URL, headers=DEFAULT_HEADERS, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    cameras = []
    for place in raw:
        lat, lon = place.get("lat"), place.get("lon")
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue

        props = place.get("additionalProperties") or []
        raw_image_url = next(
            (p.get("value") for p in props if p.get("key") == "imageUrl" and p.get("value")),
            None,
        )
        if not raw_image_url:
            continue

        place_id = place.get("id") or f"{lat:.5f}_{lon:.5f}"
        cam_id = f"tfljam-{place_id}"
        cameras.append({
            "id": cam_id,
            "location": place.get("commonName") or "TfL JamCam (Londres)",
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "TfL JamCams (Londres, Reino Unido)",
            "image_url": f"/api/cameras/image/{cam_id}",
        })
        with _image_cache_lock:
            _image_cache.setdefault(cam_id, {})
            _image_cache[cam_id]["source_url"] = raw_image_url
            _image_cache[cam_id]["headers"] = _CALTRANS_IMAGE_HEADERS

    if not cameras:
        raise ValueError("TfL JamCams nao retornou nenhuma camera")


    return cameras


def _resolve_climaaovivo_snapshot(page_url):
    """
    Abre a pagina da camera no climaaovivo.com.br e extrai a URL assinada
    do snapshot atual (campo desarquivo em __NEXT_DATA__ / HTML).
    Retorna a URL absoluta ou None.
    """
    try:
        resp = requests.get(page_url, headers=_CLIMAAOVIVO_HEADERS, timeout=12)
        resp.raise_for_status()
        text = resp.text
    except Exception:
        return None

    # Preferir JSON embutido do Next.js
    m = re.search(
        r'"desarquivo"\s*:\s*"(https:[^"]*cavsnapshots[^"]+)"',
        text,
    )
    if not m:
        m = re.search(
            r'(https://cavsnapshots\.s3\.amazonaws\.com/[^"\\]+\.jpg[^"\\]*)',
            text,
        )
    if not m:
        return None
    url = m.group(1)
    # unescape JSON (\u0026 -> &)
    try:
        url = url.encode("utf-8").decode("unicode_escape")
    except Exception:
        url = url.replace("\\u0026", "&").replace("\\/", "/")
    return url


def _fetch_climaaovivo_cameras():
    """
    Clima ao Vivo (Brasil) — API publica cmsv2.climaaovivo.com.br/api/cameras.
    ~194 cameras de monitoramento climatico com lat/lon. O snapshot ao vivo
    fica em cavsnapshots S3 com URL assinada; resolvemos sob demanda no
    proxy de imagem a partir da pagina da camera.
    """
    resp = requests.get(
        CLIMAAOVIVO_CAMERAS_URL,
        headers=_CLIMAAOVIVO_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    raw = resp.json()
    items = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError("Clima ao Vivo nao retornou nenhuma camera")

    cameras = []
    for cam in items:
        if str(cam.get("inprivada", "0")).strip() not in ("0", "false", "False", ""):
            continue
        try:
            lat = float(cam.get("deslatitude"))
            lon = float(cam.get("deslongitude"))
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue

        raw_id = _first_id(cam.get("idcamera"), cam.get("descodigo"), fallback=f"{lat:.4f}_{lon:.4f}")
        cam_id = f"cav-{raw_id}"
        titulo = (cam.get("destitulo") or cam.get("desdescricao") or "Clima ao Vivo").strip()
        cidade = (cam.get("descidade") or "").strip()
        uf = (cam.get("dessigla") or "").strip().upper()
        location_bits = [titulo]
        if cidade:
            location_bits.append(f"{cidade}/{uf}" if uf else cidade)
        elif uf:
            location_bits.append(uf)

        desurl = (cam.get("desurl") or "").strip()
        sigla = uf.lower() if uf else ""
        # Padrao confirmado no sitemap e nas paginas: /{uf}/{desurl}
        page_url = f"{CLIMAAOVIVO_SITE}/{sigla}/{desurl}" if sigla and desurl else None

        cameras.append({
            "id": cam_id,
            "location": " · ".join(location_bits),
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "Clima ao Vivo (climaaovivo.com.br)",
            "image_url": f"/api/cameras/image/{cam_id}" if page_url else None,
        })
        if page_url:
            with _image_cache_lock:
                entry = _image_cache.setdefault(cam_id, {})
                entry["headers"] = _CLIMAAOVIVO_IMAGE_HEADERS
                entry["cav_page_url"] = page_url
                entry["cav_descodigo"] = cam.get("descodigo")

    if not cameras:
        raise ValueError("Clima ao Vivo: nenhuma camera valida apos filtro")


    return cameras


def _fetch_digitraffic_cameras():
    """Weathercams da Fintraffic / Digitraffic (Finlândia) — sem chave."""
    resp = requests.get(
        DIGITRAFFIC_CAM_URL,
        headers={**DEFAULT_HEADERS, "Accept": "application/json", "Accept-Encoding": "gzip"},
        timeout=25,
    )
    resp.raise_for_status()
    features = resp.json()
    if not isinstance(features, list):
        features = (features or {}).get("features") or []
    cameras = []
    for f in features:
        props = f.get("properties") or f
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        presets = props.get("presets") or []
        preset_id = None
        for p in presets:
            if p.get("inCollection", True):
                preset_id = p.get("id")
                break
        if not preset_id and presets:
            preset_id = presets[0].get("id")
        station_id = _first_id(props.get("id"), f.get("id"), fallback="unk")
        cam_id = f"digitraffic-{station_id}"
        name = (props.get("name") or station_id or "").strip()
        img = DIGITRAFFIC_CAM_IMG.format(preset_id=preset_id) if preset_id else None
        cameras.append({
            "id": cam_id,
            "location": f"{name} (Finlândia)",
            "lat": lat,
            "lon": lon,
            "status": "ONLINE" if props.get("collectionStatus") == "GATHERING" else "UNKNOWN",
            "source": "Digitraffic / Fintraffic (FI)",
            "image_url": f"/api/cameras/image/{cam_id}" if img else None,
        })
        if img:
            with _image_cache_lock:
                entry = _image_cache.setdefault(cam_id, {})
                entry["source_url"] = img
                entry["headers"] = DEFAULT_HEADERS
    if not cameras:
        raise ValueError("Digitraffic cameras vazio")
    cameras = _cap_source_list(cameras)
    return cameras


def _fetch_mapicons_cameras(url, id_prefix, source_label):
    """Portais 511 no formato mapIcons (item2[].location = [lat,lon])."""
    site_root = url.rsplit("/", 3)[0] + "/"
    resp = requests.get(
        url,
        headers={**DEFAULT_HEADERS, "Accept": "application/json", "Referer": site_root},
        timeout=25,
    )
    resp.raise_for_status()
    data = resp.json()
    # Cabecalhos usados depois pelo proxy pra buscar a imagem: estes
    # portais devolvem 403 sem Referer do proprio site.
    img_headers = {**DEFAULT_HEADERS, "Referer": site_root, "Accept": "image/*,*/*"}
    items = data.get("item2") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError(f"{id_prefix}: formato inesperado")
    cameras = []
    for it in items:
        loc = it.get("location") or []
        if len(loc) < 2:
            continue
        try:
            lat, lon = float(loc[0]), float(loc[1])
        except (TypeError, ValueError):
            continue
        item_id = _first_id(it.get("itemId"), it.get("id"))
        if item_id is None:
            continue
        cam_id = f"{id_prefix}-{item_id}"
        title = (it.get("title") or f"{id_prefix} #{item_id}").strip()
        # Os portais 511 entregam a imagem dentro do HTML do popup
        # ("description"/"tooltip"), nao num campo dedicado - por isso a
        # versao anterior gravava None e a camera virava cinza.
        raw_image_url = _extract_image_url(it, base_url=site_root)
        _index_image(cam_id, raw_image_url, headers=img_headers)
        cameras.append({
            "id": cam_id,
            "location": title,
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": source_label,
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
        })
    if not cameras:
        raise ValueError(f"{id_prefix}: vazio")
    cameras = _cap_source_list(cameras)
    return cameras


def _make_mapicons_fetcher(url, id_prefix, source_label):
    """Prefere List/GetData (imagem+coords); cai no mapIcons se a lista vier vazia."""
    site_root = url.split("/map/")[0] if "/map/" in url else url.rsplit("/", 3)[0]

    def _fn():
        try:
            return _fetch_cars_listdata_cameras(site_root, id_prefix, source_label)
        except Exception as exc:
            logger.warning(
                "%s: List/GetData falhou (%s) — fallback mapIcons (so coords)",
                id_prefix, exc,
            )
            return _fetch_mapicons_cameras(url, id_prefix, source_label)

    _fn.__name__ = f"_fetch_{id_prefix}_cameras"
    return _fn


# Uma função por portal mapIcons (compat + registro dinâmico em get_cameras).
_MAPICONS_FETCHERS = tuple(
    (f"{prefix}_live", _make_mapicons_fetcher(url, prefix, label))
    for url, prefix, label in _MAPICONS_SOURCES
)


def _fetch_fl511_cameras():
    return _fetch_mapicons_cameras(*_MAPICONS_SOURCES[0])


def _fetch_illinois_cameras():
    """Illinois / Travel Midwest CCTV via ArcGIS FeatureServer (~3.6k)."""
    # ArcGIS limita ~1000 por request — paginar ate ~3692.
    feats = _arcgis_query_all(
        IL_ARCGIS_CCTV_URL,
        out_fields="OBJECTID,CameraLocation,CameraDirection,x,y,SnapShot,ImgPath,TooOld",
        page_size=1000,
        timeout=30,
    )
    if not feats:
        raise ValueError("Illinois ArcGIS: sem features")
    cameras = []
    for feat in feats:
        attrs = feat.get("attributes") or {}
        geom = feat.get("geometry") or {}
        try:
            lat = float(attrs.get("y") if attrs.get("y") is not None else geom.get("y"))
            lon = float(attrs.get("x") if attrs.get("x") is not None else geom.get("x"))
        except (TypeError, ValueError):
            continue
        oid = attrs.get("OBJECTID")
        if oid is None:
            continue
        cam_id = f"il-{oid}"
        name = (attrs.get("CameraLocation") or f"Illinois CCTV #{oid}").strip()
        direction = (attrs.get("CameraDirection") or "").strip()
        if direction and direction.upper() != "NONE":
            name = f"{name} ({direction})"
        raw_image = attrs.get("SnapShot") or attrs.get("ImgPath")
        if raw_image and not str(raw_image).startswith("http"):
            raw_image = None
        too_old = str(attrs.get("TooOld") or "").lower() in ("true", "1", "yes")
        status = "OFFLINE" if too_old else "ONLINE"
        if raw_image:
            with _image_cache_lock:
                entry = _image_cache.setdefault(cam_id, {})
                entry["source_url"] = raw_image
                entry["headers"] = {
                    **DEFAULT_HEADERS,
                    "Referer": "https://www.travelmidwest.com/",
                    "Accept": "image/*,*/*",
                }
        cameras.append({
            "id": cam_id,
            "location": name,
            "lat": lat,
            "lon": lon,
            "status": status,
            "source": "Illinois / Travel Midwest (US)",
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image else None,
        })
    if not cameras:
        raise ValueError("Illinois ArcGIS: vazio apos parse")
    return _cap_source_list(cameras)


def _fetch_singapore_cameras():
    """Singapura LTA via data.gov.sg traffic-images (sem chave)."""
    resp = requests.get(
        SG_TRAFFIC_IMAGES_URL,
        headers={**DEFAULT_HEADERS, "Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("Singapore traffic-images: sem items")
    cams = items[0].get("cameras") if isinstance(items[0], dict) else None
    if not isinstance(cams, list) or not cams:
        raise ValueError("Singapore traffic-images: sem cameras")
    cameras = []
    for it in cams:
        loc = it.get("location") or {}
        try:
            lat = float(loc.get("latitude"))
            lon = float(loc.get("longitude"))
        except (TypeError, ValueError):
            continue
        cid = it.get("camera_id")
        if cid is None:
            continue
        cam_id = f"sg-{cid}"
        raw_image = it.get("image")
        if raw_image:
            with _image_cache_lock:
                entry = _image_cache.setdefault(cam_id, {})
                entry["source_url"] = raw_image
                entry["headers"] = {
                    **DEFAULT_HEADERS,
                    "Referer": "https://data.gov.sg/",
                    "Accept": "image/*,*/*",
                }
        cameras.append({
            "id": cam_id,
            "location": f"Singapore camera {cid}",
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "Singapore LTA / data.gov.sg",
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image else None,
        })
    if not cameras:
        raise ValueError("Singapore: vazio")
    return cameras


def _fetch_deldot_cameras():
    """Delaware DOT videocameras JSON — sem chave."""
    resp = requests.get(
        DELDOT_CAMERAS_URL,
        headers={**DEFAULT_HEADERS, "Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    # Schema DelDOT: { videoCameras: [ { id, title, lat, lon, ... } ] }
    items = None
    if isinstance(data, dict):
        items = data.get("videoCameras") or data.get("cameras")
        if items is None:
            for k, v in data.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    items = v
                    break
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        raise ValueError("DelDOT formato inesperado")
    cameras = []
    for it in items:
        lat = it.get("lat") or it.get("latitude")
        lon = it.get("lon") or it.get("longitude")
        if lat is None or lon is None:
            continue
        cid = _first_id(it.get("id"), it.get("cameraId"), it.get("title"), fallback=len(cameras))
        cam_id = f"deldot-{cid}"
        title = (it.get("title") or it.get("name") or f"DE #{cid}").strip()
        raw_image_url = _extract_image_url(it, base_url=DELDOT_CAMERAS_URL)
        _index_image(
            cam_id,
            raw_image_url,
            headers={**DEFAULT_HEADERS, "Referer": "https://tmc.deldot.gov/", "Accept": "image/*,*/*"},
        )
        cameras.append({
            "id": cam_id,
            "location": f"{title} (Delaware)",
            "lat": float(lat),
            "lon": float(lon),
            "status": "ONLINE",
            "source": "DelDOT (US)",
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
        })
    if not cameras:
        raise ValueError("DelDOT vazio")
    cameras = _cap_source_list(cameras)
    return cameras


def _fetch_earthcam_cameras():
    """
    EarthCam Network — lista publica de webcams do mundo (skyline, praias,
    landmarks, resorts, etc.). Endpoint:
      GET https://www.earthcam.com/api/mapsearch/get_locations_network.php?r=ecn&a=fetch
    Schema: { status, msg, data: [ { places: [ { id, name, posn:[lat,lon],
    location, city, country, image, thumbnail, url, ... } ] } ] }
    ~300 cameras, sem chave. Snapshots em earthcam.com/cams/includes/image.php
    (proxy com Referer do site oficial).
    """
    resp = requests.get(
        EARTHCAM_NETWORK_URL,
        headers=_EARTHCAM_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    places = []
    if isinstance(data, dict):
        raw = data.get("data")
        if isinstance(raw, list) and raw:
            first = raw[0]
            if isinstance(first, dict):
                places = first.get("places") or []
        elif isinstance(raw, dict):
            places = raw.get("places") or []
    if not isinstance(places, list) or not places:
        raise ValueError("EarthCam Network nao retornou places")

    cameras = []
    seen = set()
    for p in places:
        if not isinstance(p, dict):
            continue
        posn = p.get("posn") or []
        if not (isinstance(posn, (list, tuple)) and len(posn) >= 2):
            continue
        try:
            lat = float(posn[0])
            lon = float(posn[1])
        except (TypeError, ValueError):
            continue
        cid = _first_id(p.get("id"), p.get("name"), fallback=len(cameras))
        cam_id = f"earthcam-{cid}"
        if cam_id in seen:
            continue
        seen.add(cam_id)

        name = (p.get("name") or "EarthCam").strip()
        location = (p.get("location") or "").strip()
        country = (p.get("country") or "").strip()
        if location and country and country not in location:
            label = f"{name} · {location}"
        elif location:
            label = f"{name} · {location}"
        elif country:
            label = f"{name} · {country}"
        else:
            label = name

        # Prefer dynamic snapshot (image.php); thumbnail as fallback.
        raw_image_url = p.get("image") or p.get("thumbnail")
        if not raw_image_url and isinstance(p.get("icon"), dict):
            raw_image_url = p["icon"].get("icon")

        cameras.append({
            "id": cam_id,
            "location": label,
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "EarthCam Network",
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
            "page_url": p.get("url"),
        })
        if raw_image_url:
            with _image_cache_lock:
                _image_cache.setdefault(cam_id, {})
                _image_cache[cam_id]["source_url"] = raw_image_url
                _image_cache[cam_id]["headers"] = _EARTHCAM_IMAGE_HEADERS

    if not cameras:
        raise ValueError("EarthCam Network vazio apos parse")
    cameras = _cap_source_list(cameras)
    return cameras


_WEBCAMERA24_DATA_PATH = Path(__file__).resolve().parents[1] / "static" / "data" / "webcamera24.json"
_wc24_enrich_cache = None


def _load_webcamera24_enrichment():
    """
    Índice id → {url, name, youtube_id, iframe, lat, lon} gerado a partir
    das páginas /camera/{país}/{slug}/ (o mapa público só devolve id+lat+lng
    e o link genérico /map/ não abre a câmera certa).
    """
    global _wc24_enrich_cache
    if _wc24_enrich_cache is not None:
        return _wc24_enrich_cache
    path = _WEBCAMERA24_DATA_PATH
    by_id = {}
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("id"):
                        by_id[str(it["id"])] = it
        except Exception:
            by_id = {}
    _wc24_enrich_cache = by_id
    return by_id


def _fetch_webcamera24_cameras():
    """
    Webcamera24 — mapa global + enriquecimento local.

      GET https://webcamera24.com/api/v1/cameras/map
        → ~4104 pontos { id, lat, lng } (sem nome, sem URL da câmera)

    O site real usa /camera/{country}/{slug}/. O JSON embutido em
    static/data/webcamera24.json liga o id do mapa à página correta,
    nome e pista de stream (YouTube / iframe). Sem match, page_url fica
    genérico (mapa com ?camera=id).
    """
    resp = requests.get(
        WEBCAMERA24_MAP_URL,
        headers={
            **DEFAULT_HEADERS,
            "Accept": "application/json",
            "Referer": "https://webcamera24.com/map/",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("cameras") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise ValueError("Webcamera24 map vazio")

    enrich = _load_webcamera24_enrichment()
    cameras = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        lat = it.get("lat")
        lon = it.get("lng") if it.get("lng") is not None else it.get("lon")
        if lat is None or lon is None:
            continue
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            continue
        cid = str(it.get("id") or f"{lat:.5f}_{lon:.5f}")
        cam_id = f"wc24-{cid}"
        if cam_id in seen:
            continue
        seen.add(cam_id)

        meta = enrich.get(cid) or {}
        label = (meta.get("name") or it.get("title") or it.get("name")
                 or f"Webcamera24 · {cid[:10]}")
        raw_page_url = meta.get("url")
        page_url_generic = False
        if not raw_page_url:
            raw_page_url = f"https://webcamera24.com/map/?camera={cid}"
            page_url_generic = True

        raw_image_url = None
        yt_id = meta.get("youtube_id")
        if yt_id:
            raw_image_url = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
            with _image_cache_lock:
                _image_cache.setdefault(cam_id, {})
                _image_cache[cam_id]["source_url"] = raw_image_url
                _image_cache[cam_id]["headers"] = {
                    **DEFAULT_HEADERS,
                    "Accept": "image/*,*/*;q=0.8",
                }

        cameras.append({
            "id": cam_id,
            "location": str(label)[:80],
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "Webcamera24",
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
            "page_url": raw_page_url,
            "page_url_generic": page_url_generic,
        })

    for cid, meta in enrich.items():
        cam_id = f"wc24-{cid}"
        if cam_id in seen:
            continue
        lat, lon = meta.get("latitude"), meta.get("longitude")
        if lat is None or lon is None:
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        seen.add(cam_id)
        yt_id = meta.get("youtube_id")
        raw_image_url = (
            f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg" if yt_id else None
        )
        if raw_image_url:
            with _image_cache_lock:
                _image_cache.setdefault(cam_id, {})
                _image_cache[cam_id]["source_url"] = raw_image_url
                _image_cache[cam_id]["headers"] = {
                    **DEFAULT_HEADERS,
                    "Accept": "image/*,*/*;q=0.8",
                }
        cameras.append({
            "id": cam_id,
            "location": str(meta.get("name") or cid)[:80],
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "Webcamera24",
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
            "page_url": meta.get("url"),
            "page_url_generic": False,
        })

    if not cameras:
        raise ValueError("Webcamera24 sem coordenadas validas")
    cameras = _cap_source_list(cameras)
    return cameras



def _hk_td_extract_items(data):
    """
    O dataset publico do HK Transport Department ja apareceu embrulhado
    de formas diferentes em versoes passadas (lista solta, ou dict com
    a lista dentro de uma chave tipo "en"/"features"/"result"). Tenta
    achar a lista de cameras em qualquer um desses formatos em vez de
    assumir um so'.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("en", "features", "result", "data", "records"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        # GeoJSON FeatureCollection
        if data.get("type") == "FeatureCollection" and isinstance(data.get("features"), list):
            return data["features"]
    return []


def _hk_td_image_url(item):
    """Varios nomes possiveis pro campo da imagem, dependendo da versao do dataset."""
    for key in ("imageUrls", "image_urls", "images"):
        v = item.get(key)
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                for k in ("url", "imageUrl", "src"):
                    if first.get(k):
                        return first[k]
    for key in ("url", "imageUrl", "image_url", "snapshotUrl"):
        if item.get(key):
            return item[key]
    return None


def _fetch_hk_traffic_cameras():
    """
    Hong Kong Transport Department — CCTV de transito em tempo real
    (data.gov.hk, sem chave). Unica fonte de China (RAE de Hong Kong)
    com API publica encontrada; mainland China nao tem equivalente
    aberto conhecido (ver comentario no topo do arquivo, perto de
    HK_TD_CCTV_URL, e a nota no SOURCES.md).
    """
    resp = requests.get(HK_TD_CCTV_URL, headers=DEFAULT_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    items = _hk_td_extract_items(data)
    if not items:
        raise ValueError("HK Transport Department: formato inesperado")

    cameras = []
    for it in items:
        if not isinstance(it, dict):
            continue
        props = it.get("properties") if isinstance(it.get("properties"), dict) else it
        geom = it.get("geometry") if isinstance(it.get("geometry"), dict) else None

        lat = lon = None
        if geom and isinstance(geom.get("coordinates"), (list, tuple)) and len(geom["coordinates"]) >= 2:
            lon, lat = geom["coordinates"][0], geom["coordinates"][1]
        else:
            lat = props.get("latitude") or props.get("lat")
            lon = props.get("longitude") or props.get("lng") or props.get("lon")
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            continue

        cam_key = _first_id(
            props.get("key"), props.get("id"), props.get("code"),
            fallback=f"{lat:.5f}_{lon:.5f}",
        )
        cam_id = f"hktd-{cam_key}"
        desc = (
            props.get("description") or props.get("name") or props.get("location")
            or f"HK Traffic Cam {cam_key}"
        )
        district = props.get("district") or props.get("region") or ""
        location = f"{desc} ({district})" if district and district not in str(desc) else str(desc)

        raw_image_url = _hk_td_image_url(props)
        cameras.append({
            "id": cam_id,
            "location": location,
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "HK Transport Dept (data.gov.hk)",
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
        })
        if raw_image_url:
            with _image_cache_lock:
                _image_cache.setdefault(cam_id, {})
                _image_cache[cam_id]["source_url"] = raw_image_url
                _image_cache[cam_id]["headers"] = DEFAULT_HEADERS

    if not cameras:
        raise ValueError("HK Transport Department nao retornou nenhuma camera")
    cameras = _cap_source_list(cameras)
    return cameras


def _its_korea_extract_items(data):
    """
    Extrai a lista de cameras da resposta do ITS Korea, tentando os
    formatos plausiveis em ordem:
      1) lista JSON pura na raiz
      2) response.data (formato CONFIRMADO contra um exemplo de
         terceiro testado na API real - ver docstring de
         _fetch_its_korea_cameras)
      3) response.body.data (formato alternativo que outras APIs
         publicas coreanas no padrao OpenAPI costumam usar - mantido
         como fallback defensivo, caso o deploy real varie)
      4) data / items / cctv / list soltos na raiz, com ou sem
         aninhamento extra em "item"
    Devolve [] (nunca levanta excecao) se nada bater - quem chama trata
    lista vazia normalmente (proximo tipo de rodovia, ou fonte inteira
    cai fora se as duas vierem vazias).
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    candidates = [data]
    resp = data.get("response")
    if isinstance(resp, dict):
        candidates.append(resp)
        body = resp.get("body")
        if isinstance(body, dict):
            candidates.append(body)

    for cand in candidates:
        for key in ("data", "items", "cctv", "list"):
            v = cand.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = v.get("item")
                if isinstance(inner, list):
                    return inner
                if isinstance(inner, dict):
                    return [inner]
    return []


def _fetch_its_korea_cameras():
    """
    ITS Korea (openapi.its.go.kr) — CCTV de rodovia na COREIA DO SUL.

    NAO FOI POSSIVEL TESTAR CONTRA A API AO VIVO NESTE AMBIENTE (o
    dominio its.go.kr bloqueia acesso automatizado / nao esta na
    allowlist de rede do container de dev, e o web_fetch de pesquisa
    tambem recusou por robots.txt). Mas a pesquisa desta sessao
    confirmou dois pontos que a versao anterior deste fetcher errava:

    1) "type" NAO aceita "all" - os unicos valores documentados sao
       "ex" (rodovias/expressway, 고속도로) e "its" (rodovias nacionais,
       국도). Por isso agora fazemos DUAS chamadas (uma por tipo) e
       juntamos o resultado, em vez de uma so' com "all" (que
       provavelmente so' devolvia vazio ou erro).
    2) Os nomes de campo abaixo (coordx/coordy/cctvname/cctvurl, dentro
       de response.data) batem com um exemplo de codigo de terceiro
       testado contra a API real (blog devlog publico, 2022) - aumenta
       bastante a confianca no parser mesmo sem eu ter conseguido bater
       na API diretamente agora.

    Com key=test (modo demo, sem cadastro) a cota e'/pode ser bem
    limitada; pra' cobertura real do pais inteiro, cadastre uma chave
    gratuita em https://www.its.go.kr e defina ITS_KOREA_API_KEY.
    """
    min_lon, min_lat, max_lon, max_lat = ITS_KOREA_BBOX
    cameras = []
    for road_type in ("ex", "its"):
        params = {
            "key": ITS_KOREA_API_KEY,
            "ReqType": 2,
            "MinX": min_lon,
            "MaxX": max_lon,
            "MinY": min_lat,
            "MaxY": max_lat,
            "type": road_type,
            "getType": "json",
        }
        try:
            resp = requests.get(ITS_KOREA_CCTV_URL, params=params, headers=DEFAULT_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            # Um dos dois tipos pode falhar (key/cota) sem invalidar o outro.
            continue

        items = _its_korea_extract_items(data)

        for it in items:
            if not isinstance(it, dict):
                continue
            lat = it.get("coordy") or it.get("lat") or it.get("latitude")
            lon = it.get("coordx") or it.get("lon") or it.get("longitude")
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                continue

            cam_key = _first_id(it.get("cctvid"), it.get("id"), fallback=f"{lat:.5f}_{lon:.5f}")
            cam_id = f"itskr-{road_type}-{cam_key}"
            name = it.get("cctvname") or it.get("name") or f"Korea CCTV {cam_key}"
            raw_image_url = it.get("cctvurl") or it.get("imageurl") or it.get("url")

            cameras.append({
                "id": cam_id,
                "location": str(name),
                "lat": lat,
                "lon": lon,
                "status": "ONLINE",
                "source": "ITS Korea (openapi.its.go.kr)",
                "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
            })
            if raw_image_url:
                with _image_cache_lock:
                    _image_cache.setdefault(cam_id, {})
                    _image_cache[cam_id]["source_url"] = raw_image_url
                    _image_cache[cam_id]["headers"] = DEFAULT_HEADERS

    if not cameras:
        raise ValueError("ITS Korea nao retornou nenhuma camera (verificar key/schema)")
    cameras = _cap_source_list(cameras)
    return cameras


def _fetch_taiwan_thb_generic(url, id_prefix, source_label, allow_empty=False):
    """
    Parser compartilhado pelos tres endpoints THB de Taiwan (freeway,
    provincial, county) - schema identico nos tres.
    """
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
    resp.raise_for_status()
    items = resp.json()
    if not isinstance(items, list):
        raise ValueError(f"{source_label}: formato inesperado (esperava lista JSON)")

    cameras = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            lon, lat = float(it.get("gisx")), float(it.get("gisy"))
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            continue

        cam_key = _first_id(it.get("id"), fallback=f"{lat:.5f}_{lon:.5f}")
        cam_id = f"{id_prefix}-{cam_key}"
        name = it.get("stakenumber") or f"Taiwan CCTV {cam_key}"
        # "html" e' o nome do campo na API deles, mas o valor e' a URL
        # direta da camera (imagem ou stream), nunca uma pagina HTML de
        # verdade - ver comentario grande perto de TW_THB_FREEWAY_CCTV_URL.
        raw_image_url = it.get("html")

        cameras.append({
            "id": cam_id,
            "location": str(name),
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": source_label,
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
        })
        if raw_image_url:
            with _image_cache_lock:
                _image_cache.setdefault(cam_id, {})
                _image_cache[cam_id]["source_url"] = raw_image_url
                _image_cache[cam_id]["headers"] = DEFAULT_HEADERS

    if not cameras and not allow_empty:
        raise ValueError(f"{source_label} nao retornou nenhuma camera")
    return _cap_source_list(cameras)


def _fetch_taiwan_freeway_cameras():
    """
    Taiwan (國道 / rodovias nacionais, "freeways") — Directorate General
    of Highways, thbapp.thb.gov.tw. TESTADA AO VIVO com sucesso nesta
    sessao (pesquisa web, nao no sandbox de bash): devolveu milhares de
    cameras reais em JSON, sem chave.
    """
    return _fetch_taiwan_thb_generic(
        TW_THB_FREEWAY_CCTV_URL, "twfwy", "Taiwan THB Freeway (thbapp.thb.gov.tw)"
    )


def _fetch_taiwan_provincial_cameras():
    """
    Taiwan (省道 / estradas provinciais) — mesma fonte/formato do
    Freeway acima, endpoint /thb. Tambem testada ao vivo com sucesso
    nesta sessao.
    """
    return _fetch_taiwan_thb_generic(
        TW_THB_PROVINCIAL_CCTV_URL, "twthb", "Taiwan THB Provincial (thbapp.thb.gov.tw)"
    )


def _fetch_taiwan_county_cameras():
    """
    Taiwan (縣市 / vias municipais) — mesmo formato, endpoint /county.
    No teste desta sessao devolveu lista VAZIA (pode exigir um
    parametro de cidade que nao mandamos, ou os dados so' aparecem em
    certos horarios) - por isso, ao contrario das outras duas funcoes
    acima, aqui uma lista vazia NAO e' tratada como erro: so' contribui
    zero cameras quando isso acontece, sem derrubar a fonte inteira.
    """
    return _fetch_taiwan_thb_generic(
        TW_THB_COUNTY_CCTV_URL, "twcounty", "Taiwan THB County (thbapp.thb.gov.tw)", allow_empty=True
    )


def _fetch_haifa_cameras():
    """
    Haifa (ISRAEL) — dataset municipal aberto de cameras de transito
    (opendata.haifa.muni.il), formato GeoJSON.

    NAO FOI POSSIVEL TESTAR CONTRA A URL AO VIVO NESTE AMBIENTE (o
    dominio bloqueia acesso automatizado no sandbox de dev). Parser
    defensivo baseado no formato GeoJSON padrao do portal (mesma
    familia de portais CKAN/datacity usada por varias cidades de
    Israel) - precisa validacao com rede aberta antes de confiar.

    So cobre a cidade de Haifa, nao Israel inteiro: nao ha' API
    publica aberta conhecida para as cameras de rodovia nacionais
    (Netivei Israel / Ayalon Highways) sem cadastro/chave.
    """
    resp = requests.get(HAIFA_TRAFFIC_CAMERAS_URL, headers=DEFAULT_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise ValueError("Haifa traffic cameras: formato GeoJSON inesperado")

    cameras = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates")
        if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            continue

        cam_key = _first_id(props.get("id"), props.get("OBJECTID"), fallback=f"{lat:.5f}_{lon:.5f}")
        cam_id = f"haifa-{cam_key}"
        name = props.get("name") or props.get("Name") or props.get("street") or f"Haifa Cam {cam_key}"

        cameras.append({
            "id": cam_id,
            "location": str(name),
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "Haifa Municipality (opendata.haifa.muni.il)",
            "image_url": None,
        })

    if not cameras:
        raise ValueError("Haifa traffic cameras nao retornou nenhuma camera")
    cameras = _cap_source_list(cameras)
    return cameras


# SkylineWebcams — base embutida (coordenadas + URLs de página/YouTube).
# Arquivo gerado a partir de dataset público curado; atualizar
# app/static/data/skylinewebcams.json para refrescar a lista.
_SKYLINE_DATA_PATH = Path(__file__).resolve().parents[1] / "static" / "data" / "skylinewebcams.json"
_YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([A-Za-z0-9_-]{6,})",
    re.I,
)


def _youtube_id_from_url(url):
    if not url:
        return None
    m = _YOUTUBE_ID_RE.search(str(url))
    return m.group(1) if m else None


def _fetch_skylinewebcams_cameras():
    """
    SkylineWebcams — ~1851 webcams cênicas (praias, cidades, castelos,
    resorts) em 65+ países, com lat/lon.

    Dados embutidos em static/data/skylinewebcams.json (fonte:
    skylinewebcams.com). A maioria das páginas usa HLS com token
    expirável (url_type=html_page); ~167 são canais YouTube.

    - YouTube: thumbnail hqdefault como image_url (proxyável).
    - html_page: sem snapshot estável em lote; marker no mapa + page_url
      para abrir a transmissão no site oficial.
    """
    path = _SKYLINE_DATA_PATH
    if not path.is_file():
        raise ValueError(f"SkylineWebcams data ausente: {path}")

    with open(path, "r", encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list) or not items:
        raise ValueError("SkylineWebcams JSON vazio ou inválido")

    cameras = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        lat = it.get("latitude")
        lon = it.get("longitude")
        if lat is None or lon is None:
            continue
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue

        name = (it.get("display_name") or it.get("name") or "SkylineWebcams").strip()
        country = (it.get("country_code") or "").strip().upper()
        scene = (it.get("scene_type") or it.get("environment") or "").strip()
        label = name
        if country and country not in name:
            label = f"{name} · {country}"
        if scene and scene.lower() not in label.lower():
            label = f"{label} · {scene}"

        page_url = (it.get("url") or "").strip() or None
        # id estável: último segmento da URL do Skyline (ex. durres-beach)
        # ou id do YouTube; fallback no nome.
        slug = None
        if page_url:
            m_yt = _YOUTUBE_ID_RE.search(page_url)
            if m_yt:
                slug = f"yt-{m_yt.group(1)}"
            else:
                path_part = page_url.rstrip("/").rsplit("/", 1)[-1]
                path_part = path_part.replace(".html", "").replace(".htm", "")
                slug = path_part or None
        if not slug:
            slug = name
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(slug))[:80].strip("-").lower() or "cam"
        cam_id = f"swc-{slug}"
        if cam_id in seen:
            continue
        seen.add(cam_id)

        raw_image_url = None
        yt_id = _youtube_id_from_url(page_url) if it.get("url_type") == "youtube" or (
            page_url and "youtu" in page_url
        ) else None
        if yt_id:
            raw_image_url = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"

        cameras.append({
            "id": cam_id,
            "location": label,
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "SkylineWebcams",
            "image_url": f"/api/cameras/image/{cam_id}" if raw_image_url else None,
            "page_url": page_url,
            "country_code": country or None,
        })
        if raw_image_url:
            with _image_cache_lock:
                _image_cache.setdefault(cam_id, {})
                _image_cache[cam_id]["source_url"] = raw_image_url
                # YouTube thumbnails não exigem Referer especial
                _image_cache[cam_id]["headers"] = {
                    **DEFAULT_HEADERS,
                    "Accept": "image/*,*/*;q=0.8",
                }

    if not cameras:
        raise ValueError("SkylineWebcams sem coordenadas válidas")
    # Base já curada (~1.8k); não amostrar agressivamente — cabem no mapa
    return cameras


# Cache do indice completo OpenCCTV (~144k markers). Separado do cache
# de listagem mesclada porque e grande e muda pouco.
_opencctv_markers_cache = {
    "timestamp": 0,
    "ids": None,
    "lats": None,
    "lngs": None,
    "count": 0,
}
_opencctv_markers_lock = threading.Lock()
_OPENCCTV_MARKERS_TTL = 30 * 60  # 30 min
# Limites de plotagem (evita travar o Cesium). O CONTADOR usa o total
# global real (opencctv_total + fontes base), não a quantidade plotada.
# Plotagem progressiva: mais markers ao aproximar (bbox), amostra leve na vista global.
# O contador da UI usa sempre o índice completo (global_total), não estes tetos.
MAX_OPENCCTV_VIEWPORT = 3500
MAX_OPENCCTV_GLOBAL_SAMPLE = 2000


def _load_opencctv_markers(force=False):
    """Carrega/caches o indice completo de markers do OpenCCTV."""
    now = time.time()
    with _opencctv_markers_lock:
        if (
            not force
            and _opencctv_markers_cache["ids"] is not None
            and (now - _opencctv_markers_cache["timestamp"]) < _OPENCCTV_MARKERS_TTL
        ):
            return (
                _opencctv_markers_cache["ids"],
                _opencctv_markers_cache["lats"],
                _opencctv_markers_cache["lngs"],
                _opencctv_markers_cache["count"],
            )

    resp = requests.get(OPENCCTV_MARKERS_URL, headers=_OPENCCTV_HEADERS, timeout=25)
    resp.raise_for_status()
    markers = resp.json()
    ids = markers.get("ids") or []
    lats = markers.get("lats") or []
    lngs = markers.get("lngs") or []
    if not ids or len(ids) != len(lats) or len(ids) != len(lngs):
        raise ValueError("OpenCCTV markers invalidos ou vazios")

    with _opencctv_markers_lock:
        _opencctv_markers_cache["ids"] = ids
        _opencctv_markers_cache["lats"] = lats
        _opencctv_markers_cache["lngs"] = lngs
        _opencctv_markers_cache["count"] = len(ids)
        _opencctv_markers_cache["timestamp"] = now

    return ids, lats, lngs, len(ids)


def _ocv_cam_id(raw_id):
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", str(raw_id))[:96]
    return f"ocv-{safe}"


# ---------- Bloqueio de cameras (OpenCCTV) sem feed tocavel ----------
#
# Uma fatia do indice "leve" do OpenCCTV (so' id/lat/lon, sem saber ainda
# o feed real) aponta pra cameras cujo feed (resolvido no clique via
# batch API) e' vazio ou de tipo que nao conseguimos tocar. Tipos
# suportados agora: image, mjpeg, m3u8 (HLS), iframe/YouTube, mp4/HLS.
# O front remove o marker se _unavailable; o backend nao reenvia ids
# bloqueados nas listagens seguintes.
#
# O bloqueio e' TEMPORARIO: o dono pode trocar o feed. Cada entrada
# guarda last_checked; um sweep em background reverifica em LOTE
# (batch API) depois de _CAM_RECHECK_SECONDS. Quem ganhar feed tocavel
# volta ao mapa automaticamente.
_CAM_UNAVAILABLE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "camera_unavailable.json"
)
_CAM_RECHECK_SECONDS = 7 * 24 * 60 * 60  # OpenCCTV muda de feed bem mais devagar que status de YouTube - 7 dias

_cam_unavailable_lock = threading.Lock()
_cam_unavailable_meta = None  # dict carregado sob demanda: cam_id -> {"raw_id":, "blocked_at":, "last_checked":}
_cam_sweep_thread_started = False


def _load_cam_unavailable_meta():
    global _cam_unavailable_meta
    with _cam_unavailable_lock:
        if _cam_unavailable_meta is not None:
            return _cam_unavailable_meta
        try:
            with open(_CAM_UNAVAILABLE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _cam_unavailable_meta = data.get("unavailable", {})
        except FileNotFoundError:
            _cam_unavailable_meta = {}
        except Exception:
            logger.warning("Cameras indisponiveis: arquivo em disco corrompido/ilegivel, comecando vazio")
            _cam_unavailable_meta = {}
        return _cam_unavailable_meta


def _save_cam_unavailable_meta():
    try:
        _atomic_json_write(_CAM_UNAVAILABLE_FILE, {"unavailable": _cam_unavailable_meta})
    except Exception:
        logger.exception("Cameras indisponiveis: falha ao salvar em disco (%s)", _CAM_UNAVAILABLE_FILE)


def _is_camera_unavailable(cam_id):
    return cam_id in _load_cam_unavailable_meta()


def _mark_camera_unavailable(cam_id, raw_id=None):
    meta = _load_cam_unavailable_meta()
    now = time.time()
    with _cam_unavailable_lock:
        entry = meta.get(cam_id)
        if entry:
            entry["last_checked"] = now
            if raw_id is not None:
                entry["raw_id"] = raw_id
        else:
            meta[cam_id] = {"raw_id": raw_id, "blocked_at": now, "last_checked": now}
        _save_cam_unavailable_meta()
    logger.info("Cameras indisponiveis: %s sem imagem publica - removida do mapa (recheck em ~%dd)",
                cam_id, _CAM_RECHECK_SECONDS // 86400)


def _unmark_camera_unavailable(cam_id):
    meta = _load_cam_unavailable_meta()
    with _cam_unavailable_lock:
        existed = meta.pop(cam_id, None) is not None
        if existed:
            _save_cam_unavailable_meta()
    if existed:
        logger.info("Cameras indisponiveis: %s ganhou imagem proxyavel - de volta ao mapa", cam_id)


def _camera_recheck_sweep():
    """Reverifica em LOTE (1 unica chamada na batch API) as cameras
    marcadas sem imagem ha' mais de _CAM_RECHECK_SECONDS. Quem ganhou
    feed proxyavel volta a ser plotada automaticamente."""
    meta = _load_cam_unavailable_meta()
    now = time.time()
    with _cam_unavailable_lock:
        due = {
            cam_id: entry.get("raw_id")
            for cam_id, entry in meta.items()
            if (now - entry.get("last_checked", 0)) >= _CAM_RECHECK_SECONDS and entry.get("raw_id")
        }
    if not due:
        return

    raw_ids = list(due.values())
    logger.info("Cameras indisponiveis: reverificando %d camera(s) em lote", len(raw_ids))
    try:
        resp = requests.post(
            OPENCCTV_BATCH_URL,
            headers={**_OPENCCTV_HEADERS, "Content-Type": "application/json"},
            json={"ids": raw_ids},
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        logger.warning("Cameras indisponiveis: falha na reverificacao em lote: %s", exc)
        return  # falha na checagem em si - nao desbloqueia nem re-bloqueia nada

    rows_by_raw_id = {}
    if isinstance(rows, list):
        for row in rows:
            if row and row.get("id") is not None:
                rows_by_raw_id[str(row["id"])] = row

    for cam_id, raw_id in due.items():
        row = rows_by_raw_id.get(str(raw_id))
        if row is None:
            # nao veio na resposta - mantem bloqueado, so' atualiza last_checked
            _mark_camera_unavailable(cam_id, raw_id)
            continue
        feed_type = str(row.get("feed_type") or "").lower()
        feed_url = row.get("feed_url")
        # Aceita qualquer feed tocavel (image/mjpeg/m3u8/iframe-YT/mp4), nao so' image.
        if _opencctv_feed_usable(feed_type, feed_url):
            _unmark_camera_unavailable(cam_id)
        else:
            _mark_camera_unavailable(cam_id, raw_id)


def _background_cam_sweep_loop():
    while True:
        time.sleep(_CAM_RECHECK_SECONDS)
        try:
            _camera_recheck_sweep()
        except Exception:
            logger.exception("Erro no sweep de reverificacao de cameras indisponiveis")


def _ensure_cam_sweep_thread():
    global _cam_sweep_thread_started
    with _cam_unavailable_lock:
        if _cam_sweep_thread_started:
            return
        _cam_sweep_thread_started = True
    threading.Thread(target=_background_cam_sweep_loop, daemon=True, name="cam-recheck-sweep").start()


def _ocv_raw_id(cam_id):
    """
    ocv-<id> -> id original do OpenCCTV.
    BUGFIX: antes fazia so str(cam_id)[4:] (slice do prefixo "ocv-"),
    o que devolve o id SANITIZADO (_ocv_cam_id troca qualquer char fora
    de [a-zA-Z0-9._-] por "-", de forma NAO reversivel). Se o id real do
    OpenCCTV tiver algum desses chars, a busca de detalhe mandava pro
    batch API um id diferente do original -> "camera nao encontrada"
    silencioso. Agora prioriza o id original guardado no cache (setado
    em _fetch_opencctv_cameras) e so cai pro slice como ultimo recurso.
    """
    if not cam_id:
        return None
    with _image_cache_lock:
        entry = _image_cache.get(cam_id)
        if entry and entry.get("ocv_raw_id") is not None:
            return entry["ocv_raw_id"]
    if str(cam_id).startswith("ocv-"):
        return str(cam_id)[4:]
    return None


def _opencctv_spatial_sample_indices(lats, lngs, indices, target):
    """
    Amostra indices com grade espacial fina + prioridade BR + boost para
    celulas esparsas (ilhas, costas, oceanos) que a grade grossa antiga
    tendia a esvaziar quando continentes densos competiam pelo mesmo quota.
    """
    if len(indices) <= target:
        return list(indices)

    min_lat, min_lon, max_lat, max_lon = _BR_BBOX
    br_idx, other_idx = [], []
    for i in indices:
        try:
            la, lo = float(lats[i]), float(lngs[i])
        except (TypeError, ValueError):
            continue
        if min_lat <= la <= max_lat and min_lon <= lo <= max_lon:
            br_idx.append(i)
        else:
            other_idx.append(i)

    # BR: ate 20% da amostra (antes ~25%), para sobrar mais para o resto do mundo
    br_quota = min(len(br_idx), max(40, target // 5))
    selected = []
    if br_idx and br_quota:
        step = max(1.0, len(br_idx) / br_quota)
        selected.extend(br_idx[int(j * step)] for j in range(br_quota))

    remaining = target - len(selected)
    if remaining > 0 and other_idx:
        # Grade mais fina (24x48 ≈ 7.5° x 7.5°) para ilhas/arquipelagos
        # nao sumirem dentro de uma unica celula continental.
        cells = {}
        for i in other_idx:
            la, lo = float(lats[i]), float(lngs[i])
            cy = int((la + 90) / 180 * 24)
            cx = int((lo + 180) / 360 * 48)
            cells.setdefault((cy, cx), []).append(i)

        # Ordena celulas: primeiro as mais esparsas (ilhas/mar costumam
        # ter poucos pontos por celula), depois as densas. Assim cada
        # celula esparsa recebe pelo menos 1 marker antes das megacidades
        # consumirem todo o restante.
        cell_keys = sorted(cells.keys(), key=lambda k: (len(cells[k]), k[0], k[1]))
        n_cells = max(1, len(cell_keys))
        # Garante 1 por celula enquanto couber; o resto e redistribuido
        base_per = 1 if remaining >= n_cells else 0
        leftover = remaining - base_per * n_cells if base_per else remaining

        for key in cell_keys:
            bucket = cells[key]
            extra = 0
            if leftover > 0 and len(bucket) > 1:
                # Celulas densas pegam o sobra proporcional, mas limitado
                extra = min(leftover, max(0, len(bucket) // 8), 8)
                leftover -= extra
            take = min(base_per + extra, len(bucket), remaining)
            if take <= 0:
                continue
            step = max(1.0, len(bucket) / take)
            for j in range(take):
                selected.append(bucket[int(j * step)])
                remaining -= 1
            if remaining <= 0:
                break

        if remaining > 0:
            chosen = set(selected)
            for i in other_idx:
                if i not in chosen:
                    selected.append(i)
                    remaining -= 1
                    if remaining <= 0:
                        break

    return selected[:target]


def _fetch_opencctv_cameras(bbox=None, limit=None):
    """
    OpenCCTV — markers leves (id/lat/lon) com plotagem progressiva.

    - Com bbox (zoom): até MAX_OPENCCTV_VIEWPORT na área.
    - Sem bbox (vista global): amostra espacial MAX_OPENCCTV_GLOBAL_SAMPLE.
    O CONTADOR usa o índice completo (global_total), não a quantidade plotada.
    Detalhe/imagem sob demanda no clique (batch API).
    """
    ids, lats, lngs, total = _load_opencctv_markers()
    if limit is not None:
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = None
    if limit is None:
        limit = MAX_OPENCCTV_VIEWPORT if bbox else MAX_OPENCCTV_GLOBAL_SAMPLE
    # Nunca plotar o índice inteiro de uma vez
    cap = MAX_OPENCCTV_VIEWPORT if bbox else MAX_OPENCCTV_GLOBAL_SAMPLE
    limit = max(1, min(int(limit), int(cap)))

    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        candidates = []
        for i in range(total):
            try:
                la, lo = float(lats[i]), float(lngs[i])
            except (TypeError, ValueError):
                continue
            if min_lat <= la <= max_lat and min_lon <= lo <= max_lon:
                candidates.append(i)
        if len(candidates) > limit:
            indices = _opencctv_spatial_sample_indices(lats, lngs, candidates, limit)
        else:
            indices = candidates
    else:
        indices = _opencctv_spatial_sample_indices(
            lats, lngs, range(total), limit
        )

    cameras = []
    for i in indices:
        raw_id = ids[i]
        cam_id = _ocv_cam_id(raw_id)
        if _is_camera_unavailable(cam_id):
            # Ja' confirmado (no clique de algum usuario) que essa camera
            # nao tem imagem publica proxyavel - nao planta o marker de
            # novo. Ver bloco "Bloqueio de cameras (OpenCCTV)" acima.
            continue
        try:
            lat, lon = float(lats[i]), float(lngs[i])
        except (TypeError, ValueError):
            continue
        cameras.append({
            "id": cam_id,
            "location": "OpenCCTV",
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "OpenCCTV (opencctv.org)",
            "image_url": None,
            "lite": True,
            "ocv_id": raw_id,
            "ocv_total": total,
        })
        with _image_cache_lock:
            entry = _image_cache.setdefault(cam_id, {})
            entry["headers"] = _OPENCCTV_IMAGE_HEADERS
            entry["ocv_raw_id"] = raw_id

    if not cameras:
        raise ValueError("OpenCCTV: nenhum marker na area solicitada")
    return cameras


def _opencctv_feed_usable(feed_type, feed_url):
    """
    Decide se o feed do OpenCCTV e' tocavel no app (nao so' imagem).
    Tipos conhecidos da batch API: image, m3u8, mjpeg, iframe, mp4.
    """
    if not feed_url or not str(feed_url).startswith("http"):
        return False
    ft = (feed_type or "").lower()
    if ft in ("image", "mjpeg", "m3u8", "iframe", "mp4"):
        return True
    # fallback: URL parece HLS mesmo com tipo estranho
    u = str(feed_url).lower()
    if ".m3u8" in u or "playlist" in u:
        return True
    return False


def _opencctv_prepare_feed(cam_id, feed_type, feed_url, raw_id):
    """
    Configura caches de imagem/stream e devolve (image_url, stream_hint, page_url).
    stream_hint e' consumido pelo front para YouTube/HLS sem precisar
    de page_url de sites whitelisted.
    """
    ft = (feed_type or "").lower()
    url = str(feed_url or "").strip()
    image_url = None
    stream_hint = None
    page_url = None

    if not url.startswith("http"):
        return None, None, None

    # --- imagem estatica ---
    if ft == "image":
        with _image_cache_lock:
            entry = _image_cache.setdefault(cam_id, {})
            entry["source_url"] = url
            entry["headers"] = _OPENCCTV_IMAGE_HEADERS
            entry["ocv_raw_id"] = raw_id
        image_url = f"/api/cameras/image/{cam_id}"
        return image_url, None, None

    # --- MJPEG: proxy como imagem (primeiro frame / stream continuo) ---
    if ft == "mjpeg" or url.lower().endswith(".mjpg") or ".mjpg" in url.lower() or "mjpeg" in url.lower():
        with _image_cache_lock:
            entry = _image_cache.setdefault(cam_id, {})
            entry["source_url"] = url
            entry["headers"] = _OPENCCTV_IMAGE_HEADERS
            entry["ocv_raw_id"] = raw_id
        image_url = f"/api/cameras/image/{cam_id}"
        return image_url, None, None

    # --- HLS (m3u8 / mp4 que na verdade e playlist) ---
    is_hls = (
        ft in ("m3u8", "mp4")
        or ".m3u8" in url.lower()
        or "playlist" in url.lower()
        or "chunklist" in url.lower()
    )
    if is_hls and (".m3u8" in url.lower() or "playlist" in url.lower() or "chunklist" in url.lower() or ft == "m3u8"):
        try:
            parts = urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}"
            with _stream_lock:
                _stream_cache[cam_id] = {
                    "root_url": url,
                    "referer": "https://opencctv.org/",
                    "origin": origin,
                    "host": parts.netloc,
                    "found_at": time.time(),
                }
            stream_hint = {
                "type": "hls",
                "playlist_url": f"/api/cameras/hls/{cam_id}/playlist.m3u8",
            }
            return None, stream_hint, url
        except Exception:
            pass

    # --- iframe (quase sempre YouTube embed) ---
    if ft == "iframe":
        yt_id = _youtube_id_from_url(url)
        if yt_id:
            page_url = f"https://www.youtube.com/watch?v={yt_id}"
            stream_hint = {"type": "youtube", "video_id": yt_id}
            return None, stream_hint, page_url
        # iframe generico: so link externo
        page_url = url
        return None, None, page_url

    # --- mp4 direto (nao playlist) ---
    if ft == "mp4" and url.lower().endswith(".mp4"):
        page_url = url
        return None, None, page_url

    # desconhecido mas com URL http: so link
    return None, None, url


def get_opencctv_detail(camera_id):
    """
    Resolve nome/cidade/feed de uma camera OpenCCTV via batch API.
    Usado no clique do usuario e internamente pelo proxy de imagem.

    Aceita TODOS os feed_type conhecidos da API (image, mjpeg, m3u8,
    iframe/YouTube, mp4) — nao so' imagem. HLS e YouTube reaproveitam
    a infra ja usada por SkylineWebcams/EarthCam.
    """
    raw_id = _ocv_raw_id(camera_id)
    if not raw_id:
        with _image_cache_lock:
            entry = _image_cache.get(camera_id) or {}
            raw_id = entry.get("ocv_raw_id")
    if not raw_id:
        return None

    try:
        br = requests.post(
            OPENCCTV_BATCH_URL,
            headers={**_OPENCCTV_HEADERS, "Content-Type": "application/json"},
            json={"ids": [raw_id]},
            timeout=20,
        )
        br.raise_for_status()
        rows = br.json()
    except Exception:
        return None

    if not isinstance(rows, list) or not rows:
        return None
    cam = rows[0]
    if not cam:
        return None

    feed_url = cam.get("feed_url")
    feed_type = str(cam.get("feed_type") or "").lower()
    name = (cam.get("name") or "OpenCCTV").strip()
    city = (cam.get("city") or "").strip()
    state = (cam.get("state") or "").strip()
    country = (cam.get("country") or "").strip()
    place_bits = [p for p in (city, state, country) if p]
    location = f"{name} · {', '.join(place_bits)}" if place_bits else name

    cam_id = camera_id if str(camera_id).startswith("ocv-") else _ocv_cam_id(raw_id)

    usable = _opencctv_feed_usable(feed_type, feed_url)
    image_url = None
    stream_hint = None
    page_url = None
    if usable:
        image_url, stream_hint, page_url = _opencctv_prepare_feed(
            cam_id, feed_type, feed_url, raw_id
        )
        _unmark_camera_unavailable(cam_id)
    else:
        # Sem feed tocavel de verdade — remove do mapa (bloqueio temporario).
        _mark_camera_unavailable(cam_id, raw_id)

    try:
        lat = float(cam.get("lat"))
        lon = float(cam.get("lng"))
    except (TypeError, ValueError):
        lat = lon = None

    out = {
        "id": cam_id,
        "location": location,
        "lat": lat,
        "lon": lon,
        "status": "ONLINE" if cam.get("active") else "OFFLINE",
        "source": "OpenCCTV (opencctv.org)",
        "image_url": image_url,
        "feed_type": feed_type,
        "feed_url": feed_url if usable else None,
        "page_url": page_url,
        "stream_hint": stream_hint,
        "country": country,
        "city": city,
        "state": state,
        "lite": False,
        "ocv_id": raw_id,
    }
    return out


def get_opencctv_stats():
    try:
        _, _, _, total = _load_opencctv_markers()
        return {"total_markers": total, "source": "opencctv.org"}
    except Exception as exc:
        return {"total_markers": 0, "error": str(exc)}


# As 11 cameras do menu de favoritos da CET (array gCams do HTML de
# https://cameras.cetsp.com.br/View/Cam.aspx em 2026-09), com lat/lon
# estimadas a partir do cruzamento de vias de cada uma (a CET nao
# publica coordenadas). "ativa" reflete o campo original da CET: uma
# camera com ativa=False esta cadastrada mas fora do ar / manutencao,
# nao um erro nosso.
_CETSP_CAMERAS = [
    {"pasta": 225, "titulo": "Ascendino Reis", "subTitulo": "R Pedro de Toledo",
     "detalhe": "", "ativa": True, "apImg": 1, "qtdeImagens": 50,
     "lat": -23.5926, "lon": -46.6395},
    {"pasta": 184, "titulo": "Brasil", "subTitulo": "Av Brig Luis Antônio",
     "detalhe": "Pr Armando S Oliveira", "ativa": True, "apImg": 1, "qtdeImagens": 50,
     "lat": -23.5665, "lon": -46.6580},
    {"pasta": 195, "titulo": "Brasil", "subTitulo": "Av Henrique Schaumann",
     "detalhe": "Av Rebouças", "ativa": True, "apImg": 1, "qtdeImagens": 50,
     "lat": -23.5665, "lon": -46.6790},
    {"pasta": 210, "titulo": "Brig Luis Antônio", "subTitulo": "Al Santos",
     "detalhe": "", "ativa": False, "apImg": 1, "qtdeImagens": 50,
     "lat": -23.5670, "lon": -46.6480},
    {"pasta": 220, "titulo": "Cidade Jardim", "subTitulo": "Av Nove de Julho",
     "detalhe": "Túnel Máx Feffer", "ativa": False, "apImg": 1, "qtdeImagens": 50,
     "lat": -23.5850, "lon": -46.6800},
    {"pasta": 180, "titulo": "Consolação", "subTitulo": "R Caio Prado",
     "detalhe": "", "ativa": False, "apImg": 1, "qtdeImagens": 50,
     "lat": -23.5570, "lon": -46.6480},
    {"pasta": 222, "titulo": "Hélio Pellegrino", "subTitulo": "R Diogo Jácome",
     "detalhe": "", "ativa": False, "apImg": 1, "qtdeImagens": 50,
     "lat": -23.5940, "lon": -46.6720},
    {"pasta": 224, "titulo": "Ibirapuera", "subTitulo": "R Ipê",
     "detalhe": "", "ativa": True, "apImg": 1, "qtdeImagens": 50,
     "lat": -23.6050, "lon": -46.6660},
    {"pasta": 200, "titulo": "Iguatemi", "subTitulo": "Av Brig Faria Lima",
     "detalhe": "R Jerônimo da Veiga", "ativa": True, "apImg": 1, "qtdeImagens": 50,
     "lat": -23.5840, "lon": -46.6850},
    {"pasta": 23, "titulo": "Paulista", "subTitulo": "Av Brigadeiro Luiz Antônio",
     "detalhe": "", "ativa": False, "apImg": 1, "qtdeImagens": 25,
     "lat": -23.5730, "lon": -46.6460},
    {"pasta": 22, "titulo": "Paulista", "subTitulo": "Metrô Consolação",
     "detalhe": "R Augusta", "ativa": False, "apImg": 1, "qtdeImagens": 25,
     "lat": -23.5560, "lon": -46.6620},
]


def _fetch_cetsp_cameras():
    """
    CET-SP nao tem endpoint JSON: montamos a lista a partir de
    _CETSP_CAMERAS (extraida do HTML da pagina) em vez de um requests.get.
    Ainda assim contam como fonte "real" (nao-demo) porque as imagens
    vem ao vivo do servidor da CET via proxy - so os metadados de
    localizacao/coordenadas e que sao estaticos.
    """
    cameras = []
    for cam in _CETSP_CAMERAS:
        cam_id = f"cetsp-{cam['pasta']}"
        bits = [cam["titulo"]]
        if cam.get("subTitulo"):
            bits.append(cam["subTitulo"])
        location = " - ".join(bits)
        if cam.get("detalhe"):
            location += f" ({cam['detalhe']})"

        cameras.append({
            "id": cam_id,
            "location": location,
            "lat": cam["lat"],
            "lon": cam["lon"],
            "status": "ONLINE" if cam["ativa"] else "OFFLINE",
            "source": "CET São Paulo (cetsp.com.br)",
            "image_url": f"/api/cameras/image/{cam_id}",
        })
        with _image_cache_lock:
            entry = _image_cache.setdefault(cam_id, {})
            entry["headers"] = _CETSP_IMAGE_HEADERS
            entry["cetsp_pasta"] = cam["pasta"]
            entry["cetsp_qtde"] = cam["qtdeImagens"]
            entry.setdefault("cetsp_frame", cam["apImg"])

    if not cameras:
        raise ValueError("CET-SP nao retornou nenhuma camera")
    cameras = _cap_source_list(cameras)
    return cameras


# ---------- (sem fallback simulado) ----------
# BUGFIX (2026-09): existia um _generate_demo_cameras() com 8 pontos
# fixos (Sao Paulo, NY, Londres, Toquio, Sydney, Cairo, Dubai,
# Guarapari) que eram plotados no globo e contados no total sempre que
# TODAS as fontes reais falhassem ao mesmo tempo - inclusive com
# source="DEMO" e status="ONLINE" mesmo sem nenhuma camera de verdade
# por tras. Removido: cameras que nao sao reais nao devem aparecer no
# mapa, nem ser contadas, nem existir na resposta da API. Se todas as
# fontes falharem, get_cameras() agora devolve lista vazia + source
# "demo" so como sinalizacao de estado (front mostra 0 cameras / badge
# offline), sem inventar pontos no mapa.
def _extract_first_mjpeg_frame(resp, max_bytes=2_000_000):
    """
    Le uma resposta HTTP em modo stream (multipart/x-mixed-replace) so'
    ate' capturar o primeiro frame JPEG completo (marcadores SOI
    0xFFD8...EOI 0xFFD9), depois fecha a conexao. Transforma um stream
    MJPEG ao vivo - que nunca termina sozinho - num snapshot unico,
    igual a qualquer outra fonte de imagem deste arquivo. `resp` deve
    ter sido obtida com requests.get(..., stream=True). Devolve os
    bytes do frame, ou None se nao achar um JPEG completo dentro do
    limite de `max_bytes` (camera fora do ar, ou nao e' MJPEG de
    verdade apesar do Content-Type).
    """
    buf = b""
    try:
        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                continue
            buf += chunk
            start = buf.find(b"\xff\xd8")
            if start != -1:
                end = buf.find(b"\xff\xd9", start)
                if end != -1:
                    return buf[start:end + 2]
            if len(buf) > max_bytes:
                break
    except Exception:
        pass
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return None


def get_camera_image(camera_id):
    """
    Busca (com cache curto) os bytes da imagem de uma camera, usando os
    headers corretos pra fonte dela (NYC precisa de Referer especifico
    por causa da hotlink-protection; Caltrans nao precisa, mas ganha os
    mesmos headers por consistencia).
    Retorna (content_bytes, content_type) ou (None, None) se indisponivel.
    """
    now = time.time()

    with _image_cache_lock:
        entry = _image_cache.get(camera_id)
        source_url = entry.get("source_url") if entry else None
        headers = entry.get("headers") if entry else None
        cav_page = entry.get("cav_page_url") if entry else None
        if entry and entry.get("content") and (now - entry.get("timestamp", 0)) < _IMAGE_CACHE_TTL_SECONDS:
            return entry["content"], entry["content_type"]

        # CET-SP: cicla frames numerados 1..qtdeImagens.
        if entry and entry.get("cetsp_pasta") is not None:
            pasta = entry["cetsp_pasta"]
            qtde = entry.get("cetsp_qtde") or 1
            frame = entry.get("cetsp_frame", 1)
            source_url = f"{CETSP_BASE_IMG_URL}{pasta}/{frame}.jpg"
            entry["cetsp_frame"] = (frame % qtde) + 1

    # Clima ao Vivo: snapshot S3 com URL assinada (expira ~45 min).
    if not source_url and cav_page:
        snap = _resolve_climaaovivo_snapshot(cav_page)
        if snap:
            source_url = snap
            headers = _CLIMAAOVIVO_IMAGE_HEADERS
            with _image_cache_lock:
                _image_cache.setdefault(camera_id, {})
                _image_cache[camera_id]["source_url"] = snap
                _image_cache[camera_id]["headers"] = headers

    # OpenCCTV: feed_url so vem no batch — resolve sob demanda no clique.
    needs_ocv = str(camera_id).startswith("ocv-")
    if not needs_ocv:
        with _image_cache_lock:
            ent = _image_cache.get(camera_id)
            needs_ocv = bool(ent and ent.get("ocv_raw_id"))
    if not source_url and needs_ocv:
        detail = get_opencctv_detail(camera_id)
        if detail:
            with _image_cache_lock:
                ent = _image_cache.get(camera_id) or {}
                source_url = ent.get("source_url")
                headers = ent.get("headers") or _OPENCCTV_IMAGE_HEADERS

    if not source_url:
        # cache de cameras pode ter expirado antes do clique do usuario;
        # forca um refresh da listagem pra reobter metadados/URL de origem
        get_cameras()
        with _image_cache_lock:
            entry = _image_cache.get(camera_id)
            source_url = entry.get("source_url") if entry else None
            headers = entry.get("headers") if entry else None
            cav_page = entry.get("cav_page_url") if entry else None
            if entry and entry.get("cetsp_pasta") is not None and not source_url:
                pasta = entry["cetsp_pasta"]
                frame = entry.get("cetsp_frame", 1)
                source_url = f"{CETSP_BASE_IMG_URL}{pasta}/{frame}.jpg"
        if not source_url and cav_page:
            snap = _resolve_climaaovivo_snapshot(cav_page)
            if snap:
                source_url = snap
                headers = _CLIMAAOVIVO_IMAGE_HEADERS
        if not source_url and str(camera_id).startswith("ocv-"):
            detail = get_opencctv_detail(camera_id)
            if detail:
                with _image_cache_lock:
                    entry = _image_cache.get(camera_id) or {}
                    source_url = entry.get("source_url")
                    headers = entry.get("headers") or _OPENCCTV_IMAGE_HEADERS
        if not source_url:
            _record_probe_fail(camera_id)
            return None, None

    try:
        # stream=True: algumas fontes (ex. as CCTVs de Taiwan, ver
        # TW_THB_FREEWAY_CCTV_URL) tem URL de "imagem" que na verdade e'
        # um stream MJPEG multipart infinito - com stream=False, o
        # requests tentaria baixar o corpo INTEIRO antes de devolver, o
        # que nunca termina sozinho e so' voltaria depois de estourar o
        # timeout (10s perdidos POR CLIQUE, sempre). Com stream=True
        # conseguimos olhar o Content-Type primeiro e, se for multipart,
        # extrair so' o primeiro frame JPEG e fechar a conexao na hora.
        resp = requests.get(source_url, headers=headers or DEFAULT_HEADERS, timeout=10, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        if "multipart/x-mixed-replace" in content_type.lower():
            frame = _extract_first_mjpeg_frame(resp)
            if not frame:
                _record_probe_fail(camera_id)
                return None, None
            content = frame
            content_type = "image/jpeg"
        else:
            content = resp.content
            resp.close()
    except Exception:
        _record_probe_fail(camera_id)
        return None, None

    # Cronometra a atualizacao real desta camera (ver comentario do
    # semaforo, mais acima).
    _record_probe_ok(camera_id, content)

    with _image_cache_lock:
        _image_cache.setdefault(camera_id, {})
        _image_cache[camera_id]["content"] = content
        _image_cache[camera_id]["content_type"] = content_type
        _image_cache[camera_id]["timestamp"] = now
        _image_cache[camera_id]["source_url"] = source_url
        if headers:
            _image_cache[camera_id]["headers"] = headers

    return content, content_type


# ---------- Ponto de entrada usado pelas rotas Flask ----------


def _fetch_opentrafficcammap_cameras():
    """
    OpenTrafficCamMap USA — ~7k cameras de trânsito (JPEG IMAGE_STREAM + HLS M3U8).
    Dados versionados em static/data/opentrafficcammap_usa.json.
    Dedupe leve por arredondamento de coords (0.001°) contra ids próprios.
    """
    path = _OTCM_USA_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"OpenTrafficCamMap data ausente: {path}")
    except Exception as exc:
        raise ValueError(f"OpenTrafficCamMap JSON invalido: {exc}") from exc
    if not isinstance(rows, list) or not rows:
        raise ValueError("OpenTrafficCamMap JSON vazio")

    cameras = []
    seen_geo = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        try:
            lat = float(row["lat"])
            lon = float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        geo_key = (round(lat, 3), round(lon, 3))
        if geo_key in seen_geo:
            continue
        seen_geo.add(geo_key)

        url = str(row.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        fmt = str(row.get("format") or "").upper()
        desc = (row.get("description") or "").strip()
        direction = (row.get("direction") or "").strip()
        state = (row.get("state") or "").strip()
        bits = [b for b in (state, desc, direction) if b]
        title = " · ".join(bits) if bits else f"OTCM #{i}"

        # id estável a partir da URL
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
        cam_id = f"otcm-{digest}"

        image_url = None
        page_url = None
        stream_hint = None

        if fmt == "IMAGE_STREAM" or any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".cgi")):
            _index_image(cam_id, url, headers={**DEFAULT_HEADERS, "Accept": "image/*,*/*"})
            image_url = f"/api/cameras/image/{cam_id}"
        elif fmt == "M3U8" or ".m3u8" in url.lower():
            try:
                parts = urlsplit(url)
                origin = f"{parts.scheme}://{parts.netloc}"
                with _stream_lock:
                    _stream_cache[cam_id] = {
                        "root_url": url,
                        "referer": origin + "/",
                        "origin": origin,
                        "host": parts.netloc,
                        "found_at": time.time(),
                    }
                stream_hint = {
                    "type": "hls",
                    "playlist_url": f"/api/cameras/hls/{cam_id}/playlist.m3u8",
                }
                page_url = url
            except Exception:
                page_url = url
        else:
            page_url = url

        cameras.append({
            "id": cam_id,
            "location": title[:100],
            "lat": lat,
            "lon": lon,
            "status": "ONLINE",
            "source": "OpenTrafficCamMap (USA)",
            "image_url": image_url,
            "page_url": page_url,
            "stream_hint": stream_hint,
            "feed_type": "image" if image_url else ("m3u8" if stream_hint else "link"),
        })

    if not cameras:
        raise ValueError("OpenTrafficCamMap: nenhuma camera valida")
    return _cap_source_list(cameras)



def _dedupe_cameras(cameras, grid=0.0015):
    """
    Remove duplicatas entre fontes (ex.: OTCM vs Caltrans/511 no mesmo ponto).

    Chaves:
      - grid geografico ~grid graus (~150 m em lat)
      - URL normalizada (sem query) quando existir imagem/stream externo

    Em empate, prioriza: image_url > stream_hint > page_url; depois fontes
    oficiais (nao OTCM / nao OpenCCTV); por fim id estavel.
    """
    if not cameras:
        return cameras

    def _priority(cam):
        src = str(cam.get("source") or "").lower()
        sid = str(cam.get("id") or "")
        # 0 = melhor
        has_img = 0 if cam.get("image_url") else 1
        has_stream = 0 if cam.get("stream_hint") else 1
        has_page = 0 if cam.get("page_url") else 1
        is_otcm = 1 if (sid.startswith("otcm-") or "opentrafficcammap" in src) else 0
        is_ocv = 1 if (sid.startswith("ocv-") or "opencctv" in src) else 0
        is_agg = 1 if is_otcm or is_ocv else 0
        return (has_img, has_stream, has_page, is_agg, is_otcm, is_ocv, sid)

    def _norm_url(cam):
        for key in ("image_url", "page_url"):
            u = cam.get(key)
            if not u or not isinstance(u, str):
                continue
            # so URLs absolutas externas (proxy interno /api/cameras/image/ nao serve)
            if u.startswith("/api/"):
                continue
            if not u.startswith("http"):
                continue
            try:
                parts = urlsplit(u)
                return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/").lower()
            except Exception:
                continue
        # tenta source_url do cache de imagem
        cid = cam.get("id")
        if cid:
            with _image_cache_lock:
                entry = _image_cache.get(cid) or {}
                su = entry.get("source_url")
            if su and isinstance(su, str) and su.startswith("http"):
                try:
                    parts = urlsplit(su)
                    return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/").lower()
                except Exception:
                    pass
        # stream cache
        if cid:
            try:
                with _stream_lock:
                    sc = _stream_cache.get(cid) or {}
                ru = sc.get("root_url")
                if ru and isinstance(ru, str) and ru.startswith("http"):
                    parts = urlsplit(ru)
                    return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/").lower()
            except Exception:
                pass
        return None

    # ordena para que o "melhor" fique primeiro e ocupe o slot
    ordered = sorted(cameras, key=_priority)

    by_geo = {}
    by_url = {}
    kept = []
    for cam in ordered:
        try:
            lat = float(cam["lat"])
            lon = float(cam["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        gkey = (round(lat / grid), round(lon / grid))
        ukey = _norm_url(cam)

        if gkey in by_geo:
            continue
        if ukey and ukey in by_url:
            continue

        by_geo[gkey] = cam
        if ukey:
            by_url[ukey] = cam
        kept.append(cam)

    return kept


def _refresh_base_camera_cache():
    """
    Busca as fontes regionais "fixas" (tudo, exceto OpenCCTV) em paralelo,
    dedupe e grava no cache global. Sempre roda ate' o fim (bloqueante para
    quem chama direto); get_cameras() decide se chama isso em thread de
    fundo (cache so' esta' vencido) ou na hora (cache totalmente vazio).
    """
    global _cache_refreshing
    from concurrent.futures import ThreadPoolExecutor, as_completed

    fetchers = (
        ("nyctmc_live", _fetch_nyc_cameras),
        ("caltrans_live", _fetch_caltrans_cameras),
        ("udot_live", _fetch_udot_cameras),
        ("on511_live", _fetch_ontario511_cameras),
        ("ab511_live", _fetch_alberta511_cameras),
        ("tfljam_live", _fetch_tfl_jamcam_cameras),
        ("cetsp_live", _fetch_cetsp_cameras),
        ("climaaovivo_live", _fetch_climaaovivo_cameras),
        ("digitraffic_cam", _fetch_digitraffic_cameras),
        # Todos os portais mapIcons (FL/AZ/GA/NC/NV/ID/UT + PA/NE/AK)
        *_MAPICONS_FETCHERS,
        ("deldot_live", _fetch_deldot_cameras),
        ("illinois_live", _fetch_illinois_cameras),
        ("singapore_live", _fetch_singapore_cameras),
        ("otcm_live", _fetch_opentrafficcammap_cameras),
        ("earthcam_live", _fetch_earthcam_cameras),
        ("webcamera24_live", _fetch_webcamera24_cameras),
        ("skylinewebcams_live", _fetch_skylinewebcams_cameras),
        ("hktd_live", _fetch_hk_traffic_cameras),
        ("itskorea_live", _fetch_its_korea_cameras),
        ("haifa_live", _fetch_haifa_cameras),
        ("tw_freeway_live", _fetch_taiwan_freeway_cameras),
        ("tw_provincial_live", _fetch_taiwan_provincial_cameras),
        ("tw_county_live", _fetch_taiwan_county_cameras),
    )
    combined = []
    sources_ok = []
    try:
        with ThreadPoolExecutor(max_workers=14) as ex:
            futs = {ex.submit(fn): tag for tag, fn in fetchers}
            try:
                done_iter = as_completed(futs, timeout=18)
                for fut in done_iter:
                    tag = futs[fut]
                    try:
                        batch = fut.result()
                        if batch:
                            combined.extend(batch)
                            sources_ok.append(tag)
                    except Exception:
                        continue
            except TimeoutError:
                # Alguma(s) fonte(s) não terminaram a tempo — em vez de
                # derrubar a rota com 500, aproveita o que já terminou e
                # segue (as fontes lentas ficam de fora dessa rodada).
                for fut, tag in futs.items():
                    if fut.done() and tag not in sources_ok:
                        try:
                            batch = fut.result()
                            if batch:
                                combined.extend(batch)
                                sources_ok.append(tag)
                        except Exception:
                            continue

        # Dedupe entre fontes regionais (OTCM vs Caltrans/511, etc.)
        before = len(combined)
        combined = _dedupe_cameras(combined)
        if before and len(combined) < before:
            logger.info(
                "dedupe base: %d -> %d (removeu %d duplicatas)",
                before, len(combined), before - len(combined),
            )
        base_cameras = combined
        base_source = "+".join(sources_ok) if sources_ok else ""
        with _cache_lock:
            _cache["data"] = list(base_cameras)
            _cache["source"] = base_source
            _cache["timestamp"] = time.time()
            _cache["base_only"] = True
        # So' vale a pena persistir se realmente veio algo util (nao
        # sobrescreve um cache bom no disco com um resultado vazio por
        # causa de uma instabilidade de rede passageira).
        if base_cameras:
            _save_base_cache_to_disk(base_cameras, base_source)
        return base_cameras, base_source
    finally:
        with _cache_lock:
            _cache_refreshing = False


def get_cameras(bbox=None, opencctv_limit=None):
    """
    Lista cameras de todas as fontes.

    bbox opcional (min_lon, min_lat, max_lon, max_lat) controla a fatia
    do indice OpenCCTV (~144k). Sem bbox, OpenCCTV entra com amostra
    global espacial. As demais fontes usam cache de 10 min.
    """
    now = time.time()

    # Fontes "fixas" (exceto OpenCCTV, que depende do viewport).
    with _cache_lock:
        has_cache = _cache["data"] is not None and _cache.get("base_only")
        cache_fresh = has_cache and (now - _cache["timestamp"]) < _CACHE_TTL_SECONDS
        if has_cache:
            base_cameras = list(_cache["data"])
            base_source = _cache["source"]
        else:
            base_cameras = None
            base_source = None
        # STALE-WHILE-REVALIDATE: quando o cache existe mas venceu, NAO
        # bloqueia a requisicao do usuario esperando os ~18s do refresh
        # (isso e' o que fazia as cameras "demorarem pra aparecer" toda
        # vez que passavam 10min desde o ultimo refresh). Serve o cache
        # vencido na hora e dispara UMA thread de fundo pra atualizar -
        # a proxima requisicao ja pega os dados novos, sem ninguem
        # esperar o fetch de ~30 fontes travando a resposta.
        should_spawn_refresh = has_cache and not cache_fresh and not _cache_refreshing
        if should_spawn_refresh:
            _cache_refreshing = True

    if should_spawn_refresh:
        threading.Thread(target=_refresh_base_camera_cache, daemon=True).start()

    if base_cameras is None:
        # Cache totalmente frio (nunca preenchido ainda, ex. warmup nao
        # terminou) - aqui SIM precisa bloquear pra devolver algo.
        base_cameras, base_source = _refresh_base_camera_cache()

    cameras = list(base_cameras)
    sources_ok = [s for s in base_source.split("+") if s] if base_source else []
    base_count = len(base_cameras)

    # Índice completo OpenCCTV SEMPRE (independente da amostra plotada).
    # Se a amostra falhar, o contador ainda mostra o total do catálogo.
    ocv_total_markers = 0
    try:
        ocv_total_markers = int(get_opencctv_stats().get("total_markers", 0) or 0)
    except Exception:
        ocv_total_markers = 0

    ocv_plotted = 0
    try:
        ocv = _fetch_opencctv_cameras(bbox=bbox, limit=opencctv_limit)
        cameras.extend(ocv)
        ocv_plotted = len(ocv)
        sources_ok.append("opencctv_live")
        # Confirma total a partir dos markers da amostra, se maior
        if ocv:
            sample_total = int(ocv[0].get("ocv_total") or 0)
            if sample_total > ocv_total_markers:
                ocv_total_markers = sample_total
    except Exception:
        pass

    # Dedupe final: base + amostra OpenCCTV (mesmo ponto = 1 marker)
    before = len(cameras)
    cameras = _dedupe_cameras(cameras)
    if before and len(cameras) < before:
        logger.info(
            "dedupe final: %d -> %d (removeu %d)",
            before, len(cameras), before - len(cameras),
        )
    # ocv_plotted ajustado ao que sobrou da amostra apos dedupe
    ocv_plotted = sum(
        1 for c in cameras
        if str(c.get("id") or "").startswith("ocv-")
        or "opencctv" in str(c.get("source") or "").lower()
    )

    # TOTAL catálogo = base deduplicada + índice OpenCCTV completo
    # (amostra plotada pode ser menor; contador usa o catálogo)
    global_total = int(base_count) + int(ocv_total_markers)

    if cameras:
        source = "+".join(sources_ok) if sources_ok else "demo"
    else:
        cameras = []
        source = "demo"

    # Semaforo de atualizacao por camera (verde/amarelo/vermelho/cinza).
    for cam in cameras:
        _apply_status(cam)

    # Remove do mapa cameras cujo video do YouTube esta' CONFIRMADO
    # privado/indisponivel NO MOMENTO (ver bloco "Bloqueio de canais
    # YouTube" acima). O bloqueio e' temporario: dispara aqui o sweep de
    # reverificacao em background (so' inicia a thread uma vez) pra' quem
    # voltar a ficar publico reaparecer no mapa sozinho, sem precisar que
    # alguem reabra a camera manualmente.
    _ensure_yt_sweep_thread()
    _ensure_cam_sweep_thread()
    if cameras:
        blocked_ids = _load_yt_blocked_meta()
        if blocked_ids:
            before = len(cameras)
            cameras = [
                cam for cam in cameras
                if not (
                    (yt := _youtube_id_from_url(cam.get("page_url"))) and yt in blocked_ids
                )
            ]
            removed = before - len(cameras)
            if removed:
                logger.info("get_cameras: %d camera(s) removida(s) do mapa (YouTube privado/indisponivel no momento)", removed)

    return cameras, source, global_total


# Carrega o cache do disco assim que o modulo e' importado (import
# acontece cedo no boot do Flask), pra' ja' ter dados prontos antes da
# primeira requisicao chegar - ver comentario em _BASE_CACHE_FILE.
_load_base_cache_from_disk()
