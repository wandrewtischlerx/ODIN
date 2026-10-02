"""
Cabecalhos HTTP compartilhados para as chamadas a APIs externas.

Varias APIs publicas (adsbdb, CelesTrak, OpenSky, etc.) bloqueiam ou
tratam de forma diferente requisicoes sem um User-Agent identificavel
(o padrao da biblioteca "requests" e algo como "python-requests/2.x",
que alguns servidores rejeitam ou limitam mais agressivamente).
Usamos um User-Agent descritivo em todas as chamadas do projeto.
"""

DEFAULT_HEADERS = {
    "User-Agent": "WTXTEC-GlobalMonitor/1.0 (prototype; contato via projeto WTX Global Monitor)",
    "Accept": "application/json",
}
