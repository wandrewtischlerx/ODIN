"""
Camada de "computadores" (rede/OSINT) do ODIN Map — inspirada no
protótipo standalone Ophanim/NETSCOPE, adaptada à arquitetura do
projeto (service + route + cache em memória, como infra_service.py).

O que este módulo faz, em 4 passos, sempre contra hosts públicos
conhecidos (ver TARGET_HOSTS): 1) TCP connect scan numa lista fixa de
portas comuns; 2) leitura PASSIVA do banner que o serviço manda por
conta própria ao aceitar a conexão (sem enviar credenciais, sem tentar
login, sem enviar comandos que alterem estado do lado remoto); 3)
geolocalização do IP via API pública; 4) correlação do serviço
detectado com uma pequena tabela de CVEs/avisos conhecidos (só o
nome/severidade do CVE — nenhum código de exploração).

Diferença deliberada em relação ao Ophanim original: lá o fingerprint
de FTP chega a enviar USER/PASS para testar login anônimo. Aqui isso
foi removido — só lemos o banner que o servidor manda sozinho. Dá pra
saber que uma porta está aberta e qual serviço/versão está rodando
sem chegar a "tentar entrar" em nada.

Persistência local (histórico entre execuções): cada varredura é
mesclada em `data/computers_history.json` (contagem de portas abertas
ao longo do tempo, primeiro/último avistamento). Isso fica só no disco
local do servidor — nada daqui é enviado, sincronizado ou retransmitido
para fora desta máquina.
"""

import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
import urllib3

from .http_headers import DEFAULT_HEADERS

# verify=False é intencional aqui: estamos só lendo headers/certificado
# publicamente expostos por hosts de terceiros para fins de inventário,
# não validando identidade para uma conexão de confiança nossa.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_DIR = os.path.dirname(_APP_DIR)
_DATA_DIR = os.path.join(_PROJECT_DIR, "data")
_HISTORY_FILE = os.path.join(_DATA_DIR, "computers_history.json")

CONNECT_TIMEOUT = 1.5
BANNER_TIMEOUT = 2.0
MAX_HOST_WORKERS = 12   # hosts escaneados em paralelo
MAX_PORT_WORKERS = 20   # portas escaneadas em paralelo por host

_SCAN_TTL = 30 * 60  # recache a cada 30min — não tem sentido reescanear a cada request
_GEO_TTL = 7 * 24 * 3600  # geolocalização de IP quase não muda

_cache_lock = threading.Lock()
_cache = {}  # "computers" -> (expires_at, value)

_geo_lock = threading.Lock()
_geo_cache = {}  # ip -> (expires_at, geo_dict)

_history_lock = threading.Lock()

_scan_state = {"running": False, "last_run": None, "last_error": None}


def cached(key, ttl_seconds, loader):
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry[0] > now:
            return entry[1]
    value = loader()
    with _cache_lock:
        _cache[key] = (now + ttl_seconds, value)
    return value


def invalidate_cache():
    with _cache_lock:
        _cache.pop("computers", None)


# ═══════════════════════════════════════════════════════════════════
#  Portas alvo + metadados de serviço
# ═══════════════════════════════════════════════════════════════════

TARGET_PORTS = sorted(set([
    21, 22, 23, 25, 53, 80, 110, 123, 143, 161, 179, 389, 443, 445,
    465, 587, 631, 636, 993, 995,
    1433, 1521, 3306, 3389, 5432, 5900, 5984, 6379,
    7474, 8080, 8086, 8443, 8888, 9042, 9090, 9100, 9200, 27017,
]))

# port -> (nome, risco, emoji, descrição)
PORT_META = {
    21:    ("FTP",            "alto",    "📁", "File Transfer Protocol"),
    22:    ("SSH",            "medio",   "🔐", "Secure Shell"),
    23:    ("Telnet",         "critico", "☠️", "Telnet — credenciais em texto puro"),
    25:    ("SMTP",           "medio",   "📧", "Mail Transfer Agent"),
    53:    ("DNS",            "baixo",   "🌐", "Domain Name System"),
    80:    ("HTTP",           "baixo",   "🌍", "Servidor Web (texto puro)"),
    110:   ("POP3",           "medio",   "📬", "Post Office Protocol v3"),
    123:   ("NTP",            "baixo",   "🕐", "Network Time Protocol"),
    143:   ("IMAP",           "medio",   "📬", "Internet Message Access Protocol"),
    161:   ("SNMP",           "critico", "📡", "Simple Network Management — community string"),
    179:   ("BGP",            "alto",    "🔀", "Border Gateway Protocol"),
    389:   ("LDAP",           "alto",    "🗂️", "Lightweight Directory Access Protocol"),
    443:   ("HTTPS",          "baixo",   "🔒", "Servidor Web seguro"),
    445:   ("SMB",            "critico", "💀", "Server Message Block"),
    465:   ("SMTPS",          "baixo",   "📧", "Envio de e-mail com TLS"),
    587:   ("SMTP-TLS",       "baixo",   "📧", "Submissão de e-mail com TLS"),
    631:   ("IPP",            "medio",   "🖨️", "Internet Printing Protocol"),
    636:   ("LDAPS",          "baixo",   "🗂️", "LDAP sobre SSL"),
    993:   ("IMAPS",          "baixo",   "📬", "IMAP seguro"),
    995:   ("POP3S",          "baixo",   "📬", "POP3 seguro"),
    1433:  ("MSSQL",          "alto",    "🗄️", "Microsoft SQL Server"),
    1521:  ("Oracle",         "alto",    "🗄️", "Oracle TNS Listener"),
    3306:  ("MySQL",          "alto",    "🗄️", "Banco de dados MySQL"),
    3389:  ("RDP",            "critico", "🖥️", "Remote Desktop Protocol"),
    5432:  ("PostgreSQL",     "alto",    "🗄️", "Banco de dados PostgreSQL"),
    5900:  ("VNC",            "critico", "👁️", "Virtual Network Computing"),
    5984:  ("CouchDB",        "alto",    "🗄️", "CouchDB REST API"),
    6379:  ("Redis",          "critico", "🔴", "Redis — acesso sem autenticação é comum"),
    7474:  ("Neo4j",          "alto",    "🔵", "Neo4j Graph Database"),
    8080:  ("HTTP-Alt",       "baixo",   "🌍", "HTTP alternativo"),
    8086:  ("InfluxDB",       "alto",    "📊", "InfluxDB HTTP API"),
    8443:  ("HTTPS-Alt",      "baixo",   "🔒", "HTTPS alternativo"),
    8888:  ("Jupyter",        "critico", "📓", "Jupyter Notebook"),
    9042:  ("Cassandra-CQL",  "alto",    "🗄️", "Cassandra CQL Native"),
    9090:  ("Prometheus",     "alto",    "📊", "Prometheus Metrics"),
    9100:  ("Printer-JetDirect", "alto", "🖨️", "HP JetDirect / Printer RawPort"),
    9200:  ("Elasticsearch",  "critico", "🔍", "Elasticsearch"),
    27017: ("MongoDB",        "critico", "🗄️", "MongoDB"),
}

RISK_ORDER = {"baixo": 0, "medio": 1, "alto": 2, "critico": 3}

# Nome do serviço -> avisos conhecidos (só id + descrição + severidade;
# nenhum código ou passo de exploração — é o mesmo nível de detalhe de
# um advisory público, não um how-to).
CVE_HINTS = {
    "SMB":           [("CVE-2017-0144", "EternalBlue — RCE pré-autenticação (WannaCry/NotPetya)", "CRITICO")],
    "RDP":           [("CVE-2019-0708", "BlueKeep — RCE pré-autenticação no RDP", "CRITICO")],
    "Redis":         [("MISCONFIG", "Redis exposto à internet sem autenticação é um padrão de risco comum", "CRITICO")],
    "MongoDB":       [("MISCONFIG", "MongoDB sem autenticação — exposição de dados", "CRITICO")],
    "Elasticsearch": [("MISCONFIG", "Elasticsearch sem autenticação — leitura/escrita anônima possível", "CRITICO")],
    "Jupyter":       [("MISCONFIG", "Jupyter Notebook sem senha permite execução de código arbitrário", "CRITICO")],
    "Telnet":        [("MISCONFIG", "Telnet transmite credenciais em texto puro", "CRITICO")],
    "SNMP":          [("MISCONFIG", "Community string 'public' permite enumeração completa da rede", "ALTO")],
    "VNC":           [("MISCONFIG", "VNC sem autenticação — acesso visual direto ao sistema", "CRITICO")],
    "FTP":           [("MISCONFIG", "FTP anônimo habilitado permite leitura/escrita pública", "ALTO")],
    "SSH":           [("INFO", "Verificar a versão do OpenSSH contra CVEs recentes", "BAIXO")],
    "HTTP":          [("INFO", "Checar cabeçalhos de segurança (HSTS, CSP, X-Frame-Options)", "BAIXO")],
    "Prometheus":    [("MISCONFIG", "Métricas do Prometheus expostas podem vazar dados internos", "MEDIO")],
}

# ═══════════════════════════════════════════════════════════════════
#  Alvos — infraestrutura pública, conhecida, já documentada por seus
#  próprios operadores (DNS públicos, CDNs, clouds, registries).
#  Mesma lista de espírito do protótipo original, resumida.
# ═══════════════════════════════════════════════════════════════════

TARGET_HOSTS = [
    ("8.8.8.8",         "Google Public DNS",            "EUA"),
    ("8.8.4.4",         "Google Public DNS 2",          "EUA"),
    ("1.1.1.1",         "Cloudflare DNS",                "EUA"),
    ("1.0.0.1",         "Cloudflare DNS 2",              "EUA"),
    ("9.9.9.9",         "Quad9 DNS / IBM",               "EUA"),
    ("208.67.222.222",  "OpenDNS / Cisco",               "EUA"),
    ("140.82.112.4",    "GitHub",                        "EUA"),
    ("192.0.47.72",     "ICANN",                         "EUA"),
    ("198.41.0.4",      "Verisign Root DNS (a.root-servers.net)", "EUA"),
    ("91.198.174.192",  "Wikipedia / Wikimedia",         "Holanda"),
    ("195.148.127.250", "Hetzner Cloud",                 "Alemanha"),
    ("5.45.96.220",     "OVHcloud",                      "França"),
    ("193.63.75.10",    "CERN",                          "Suíça"),
    ("195.14.130.59",   "RIPE NCC",                      "Holanda"),
    ("77.88.8.8",       "Yandex DNS",                    "Rússia"),
    ("180.76.76.76",    "Baidu DNS",                     "China"),
    ("223.5.5.5",       "Alibaba AliDNS",                "China"),
    ("114.114.114.114", "114DNS",                        "China"),
    ("202.12.27.33",    "APNIC",                         "Austrália"),
    ("168.126.63.1",    "KT Corp DNS",                   "Coreia do Sul"),
    ("200.160.7.186",   "NIC.br / Registro.br",          "Brasil"),
    ("177.71.128.67",   "Amazon AWS São Paulo",          "Brasil"),
    ("200.221.11.100",  "UOL",                           "Brasil"),
    ("200.3.13.10",     "LACNIC",                        "Uruguai"),
    ("41.206.188.66",   "AFRINIC",                       "Maurício"),
    ("80.78.66.66",     "Turk Telekom",                  "Turquia"),
]


def _log_scan_error(msg):
    _scan_state["last_error"] = msg


# ═══════════════════════════════════════════════════════════════════
#  Fingerprint — leitura PASSIVA de banner (sem enviar credenciais)
# ═══════════════════════════════════════════════════════════════════

def _recv_banner(sock, size=1024):
    try:
        data = sock.recv(size)
        return data.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _probe_generic(ip, port):
    """Conecta e só espera o que o serviço manda por conta própria."""
    info = {}
    try:
        with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT) as s:
            s.settimeout(BANNER_TIMEOUT)
            banner = _recv_banner(s)
            if banner:
                info["banner"] = banner[:200]
    except Exception:
        pass
    return info


def _probe_ssh(ip, port=22):
    info = {}
    try:
        with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT) as s:
            s.settimeout(BANNER_TIMEOUT)
            banner = _recv_banner(s, 256)
            info["banner"] = banner[:150]
            m = re.search(r"SSH-[\d.]+-(\S+)", banner)
            if m:
                sw = m.group(1)
                info["software"] = sw[:80]
                low = sw.lower()
                if "openssh" in low:
                    info["vendor"] = "OpenSSH"
                elif "dropbear" in low:
                    info["vendor"] = "Dropbear (embarcado/IoT)"
                elif "cisco" in low:
                    info["vendor"] = "Cisco IOS"
    except Exception:
        pass
    return info


def _probe_http(ip, port=80, use_tls=False):
    info = {}
    try:
        scheme = "https" if use_tls else "http"
        url = f"{scheme}://{ip}:{port}/"
        resp = requests.get(
            url, timeout=BANNER_TIMEOUT, headers=DEFAULT_HEADERS,
            verify=False, allow_redirects=False,
        )
        info["status_code"] = resp.status_code
        server = resp.headers.get("Server")
        if server:
            info["server_header"] = server[:120]
        powered = resp.headers.get("X-Powered-By")
        if powered:
            info["x_powered_by"] = powered[:120]
    except Exception:
        pass
    return info


def _probe_tls_cert(ip, port=443):
    """Metadados do certificado — nenhum handshake fora do padrão TLS."""
    info = {}
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=ip) as ssock:
                cert = ssock.getpeercert(binary_form=False)
                if cert:
                    subject = dict(x[0] for x in cert.get("subject", []))
                    issuer = dict(x[0] for x in cert.get("issuer", []))
                    if "commonName" in subject:
                        info["cert_cn"] = subject["commonName"]
                    if "organizationName" in issuer:
                        info["cert_issuer"] = issuer["organizationName"]
    except Exception:
        pass
    return info


_PROBE_BY_PORT = {
    22: _probe_ssh,
    80: lambda ip, port: _probe_http(ip, port, use_tls=False),
    443: lambda ip, port: {**_probe_http(ip, port, use_tls=True), **_probe_tls_cert(ip, port)},
    8080: lambda ip, port: _probe_http(ip, port, use_tls=False),
    8443: lambda ip, port: _probe_http(ip, port, use_tls=True),
}


def _is_port_open(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT):
            return True
    except Exception:
        return False


def _scan_port(ip, port):
    if not _is_port_open(ip, port):
        return None
    probe = _PROBE_BY_PORT.get(port, _probe_generic)
    try:
        extra = probe(ip, port)
    except Exception:
        extra = {}
    name, risk, emoji, desc = PORT_META.get(port, ("Desconhecido", "baixo", "❔", ""))
    result = {
        "port": port,
        "service": name,
        "risk": risk,
        "emoji": emoji,
        "description": desc,
        **extra,
    }
    hints = CVE_HINTS.get(name)
    if hints:
        result["advisories"] = [
            {"id": h[0], "description": h[1], "severity": h[2]} for h in hints
        ]
    return result


def scan_host_ports(ip, ports=None):
    ports = ports or TARGET_PORTS
    open_ports = []
    with ThreadPoolExecutor(max_workers=MAX_PORT_WORKERS) as ex:
        futs = {ex.submit(_scan_port, ip, p): p for p in ports}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                open_ports.append(r)
    open_ports.sort(key=lambda r: r["port"])
    return open_ports


# ═══════════════════════════════════════════════════════════════════
#  Geolocalização (ip-api.com — pública, sem chave, uso não comercial)
# ═══════════════════════════════════════════════════════════════════

def _geo_lookup_batch(ips):
    now = time.monotonic()
    result = {}
    need = []
    with _geo_lock:
        for ip in ips:
            entry = _geo_cache.get(ip)
            if entry is not None and entry[0] > now:
                result[ip] = entry[1]
            else:
                need.append(ip)

    for i in range(0, len(need), 100):
        chunk = need[i:i + 100]
        payload = [{"query": ip, "fields": "status,country,city,lat,lon,isp,org,as,query"} for ip in chunk]
        try:
            resp = requests.post(
                "http://ip-api.com/batch", json=payload,
                timeout=6, headers=DEFAULT_HEADERS,
            )
            data = resp.json()
        except Exception:
            data = []
        for row in data:
            ip = row.get("query")
            if not ip:
                continue
            geo = {
                "country": row.get("country"),
                "city": row.get("city"),
                "lat": row.get("lat"),
                "lon": row.get("lon"),
                "isp": row.get("isp"),
                "org": row.get("org"),
                "asn": row.get("as"),
            } if row.get("status") == "success" else {}
            with _geo_lock:
                _geo_cache[ip] = (now + _GEO_TTL, geo)
            result[ip] = geo
    return result


# ═══════════════════════════════════════════════════════════════════
#  Histórico local persistente (JSON em disco — nada sai desta máquina)
# ═══════════════════════════════════════════════════════════════════

def _load_history():
    with _history_lock:
        if not os.path.isfile(_HISTORY_FILE):
            return {}
        try:
            with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def _save_history(history):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with _history_lock:
        tmp = _HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _HISTORY_FILE)


def _merge_history(record):
    history = _load_history()
    ip = record["ip"]
    now_iso = datetime.now(timezone.utc).isoformat()
    entry = history.get(ip, {"first_seen": now_iso, "scan_count": 0, "port_history": []})
    entry["last_seen"] = now_iso
    entry["scan_count"] = entry.get("scan_count", 0) + 1
    entry["label"] = record.get("label")
    entry.setdefault("port_history", []).append({
        "timestamp": now_iso,
        "open_ports": [p["port"] for p in record.get("open_ports", [])],
    })
    # Mantém só as últimas 50 varreduras por host — histórico útil sem
    # o arquivo crescer sem limite num servidor que fica sempre ligado.
    entry["port_history"] = entry["port_history"][-50:]
    history[ip] = entry
    _save_history(history)
    return entry


# ═══════════════════════════════════════════════════════════════════
#  Orquestração
# ═══════════════════════════════════════════════════════════════════

def _overall_risk(open_ports):
    if not open_ports:
        return "baixo"
    return max((p["risk"] for p in open_ports), key=lambda r: RISK_ORDER.get(r, 0))


def _scan_one_host(ip, label, region):
    open_ports = scan_host_ports(ip)
    record = {
        "id": ip,
        "ip": ip,
        "label": label,
        "region_hint": region,
        "open_ports": open_ports,
        "risk_level": _overall_risk(open_ports),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    hist_entry = _merge_history(record)
    record["first_seen"] = hist_entry.get("first_seen")
    record["scan_count"] = hist_entry.get("scan_count")
    return record


def _run_full_scan():
    _scan_state["running"] = True
    _scan_state["last_error"] = None
    try:
        records = []
        with ThreadPoolExecutor(max_workers=MAX_HOST_WORKERS) as ex:
            futs = {
                ex.submit(_scan_one_host, ip, label, region): ip
                for ip, label, region in TARGET_HOSTS
            }
            for fut in as_completed(futs):
                try:
                    records.append(fut.result())
                except Exception as ex2:
                    _log_scan_error(str(ex2)[:200])

        geo = _geo_lookup_batch([r["ip"] for r in records])
        for r in records:
            g = geo.get(r["ip"], {})
            r["lat"] = g.get("lat")
            r["lon"] = g.get("lon")
            r["city"] = g.get("city")
            r["country"] = g.get("country") or r["region_hint"]
            r["isp"] = g.get("isp")
            r["org"] = g.get("org")
            r["asn"] = g.get("asn")

        # Sem geo (ex.: sem rede/API fora do ar): mantém o host na
        # lista só se soubermos aproximar a posição pela região
        # curada — senão ele não teria onde ser plotado no globo.
        records = [r for r in records if r.get("lat") is not None]

        _scan_state["last_run"] = datetime.now(timezone.utc).isoformat()
        return records, "scan_ativo"
    except Exception as ex:
        _log_scan_error(str(ex)[:300])
        return [], "erro"
    finally:
        _scan_state["running"] = False


def get_computers(force_rescan=False):
    if force_rescan:
        invalidate_cache()
    records, source = cached("computers", _SCAN_TTL, _run_full_scan)
    return records, source


def get_scan_status():
    return dict(_scan_state)


def get_computer_details(ip):
    history = _load_history()
    return history.get(ip)
