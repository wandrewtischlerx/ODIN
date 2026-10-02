# WTXTEC · Global Monitor

Protótipo funcional de plataforma de monitoramento global com globo 3D
(CesiumJS), inspirado no conceito "Eye of God" com identidade própria.

## Rodando localmente

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Acesse `http://localhost:5000`.

Camada de infraestrutura física (energia + internet) em andamento por
blocos — ver `INFRAFISICA.md` para o plano completo e o que já foi
implementado.

## Estado atual dos dados por camada

| Camada     | Fonte atual                          | Notas |
|------------|---------------------------------------|-------|
| AIRCRAFT   | **REAL** — adsb.lol + adsb.fi (militares + hubs civis ampliados, incl. ilhas/Caribe/Havaí/Islândia) + OpenSky Network | Sem chave. Mais hubs de amostragem. |
| SATELLITES | **REAL** — SatNOGS DB (TLE) + propagação SGP4; fallback CelesTrak multi-grupo (active, stations, visual, weather, gnss, resource, starlink) | Posições em tempo real; até ~250 objetos amostrados. |
| SHIPS      | **REAL** — Digitraffic (Báltico) + Kystverket (Noruega) + Hormuz + **Open Waters/aiscast** (bboxes de alta densidade: Canal da Mancha, Singapura, NY, Tóquio, Suez, etc.). Opcional: AISstream.io (global) | Open Waters sem chave. AISstream exige `AISSTREAM_API_KEY`. |
| PUBLIC CAMERAS | **REAL** — NYC/Caltrans/UDOT/ON511/AB511/TfL/CET-SP/Clima ao Vivo + Digitraffic FI + FL/AZ/GA/NC/NV/ID 511 + DelDOT + **OpenCCTV (~144k–239k no índice)** | Plotagem por viewport (não trava). **Contador = TOTAL DA TERRA** (fixo, não muda com zoom). |
| USINAS DE ENERGIA | **REAL** — Global Power Plant Database (WRI), dump estático versionado no repo | 25.690 usinas: hidrelétrica, nuclear, carvão, solar, eólica. Sem refresh periódico (dado não se move). Ver `INFRAFISICA.md`. |
| CABOS SUBMARINOS | ainda não implementado | Bloco 1 do `INFRAFISICA.md`. Rota `/api/infrastructure/cables` já existe, devolve 501. |
| DATACENTERS | ainda não implementado | Bloco 3 do `INFRAFISICA.md`. Rota `/api/infrastructure/datacenters` já existe, devolve 501. |
| COMPUTADORES (REDE) | **REAL** — TCP connect scan + leitura passiva de banner + geoIP (ip-api.com), contra uma lista curada de ~25 hosts públicos conhecidos (DNS, CDN, cloud, registries) | Sem login/exploração — só o que o serviço expõe sozinho. Cache de 30min; botão "forçar nova varredura" no painel. Histórico local em `data/computers_history.json` (não sai desta máquina — sem sync/retransmissão pra outras instâncias). Ver `app/services/computer_service.py`. |

Cada camada expõe `is_live` na API; a interface mostra o selo **AO VIVO** ou **DEMO**.
O campo `source` pode ser composto (ex.: `"adsb_lol+adsb_fi+opensky"`), refletindo
exatamente quais fontes contribuíram naquela atualização — só cai para `"demo"`
se **todas** as fontes de uma camada falharem ao mesmo tempo.

### Variáveis de ambiente opcionais

| Variável | Efeito |
|----------|--------|
| `AISSTREAM_API_KEY` | Habilita cobertura global de navios via AISstream.io (WebSocket). Sem ela, o app usa as 4 fontes REST regionais (Digitraffic, Kystverket, Hormuz, Open Waters). |

## Estrutura

```
app/
  routes/       endpoints Flask
  services/     obtenção de dados (real ou simulado)
  templates/    index.html
  static/
    css/        estilo do HUD
    js/         globe, aircraft, satellites, cameras, main
```

## Diagnóstico

```
http://localhost:5000/api/diagnostics/
```

Retorna três blocos:

- `connectivity` — teste bruto de cada endpoint externo (OK/falha, status HTTP, latência, motivo do erro).
- `schema_probes` — busca ao vivo nas fontes mais sensíveis a mudança de formato (adsb.lol, adsb.fi, Caltrans) e mostra os campos brutos reais encontrados vs. os que o código espera (`expected_keys_missing`).
- `layers` — chama as mesmas funções que a aplicação usa de verdade (`get_aircraft`/`get_ships`/`get_cameras`), com contagens por fonte e uma amostra de registros já processados. Reflete exatamente o que o globo está mostrando.

Como o ambiente de desenvolvimento não tem acesso de rede a esses hosts externos, esse endpoint funciona como uma ponte: rode localmente e cole o JSON de resposta na conversa para depuração — ele traz tudo que costuma ser preciso para investigar sem precisar de acesso de rede direto. Pode levar alguns segundos pra responder (a varredura de aeronaves por hubs é a parte mais lenta) — é normal.

## Câmeras

As imagens do NYC DOT bloqueiam hotlinking (Referer). O backend faz proxy em
`/api/cameras/image/<id>` e o painel de informações atualiza o frame a cada 5s.
Caltrans CCTV usa o mesmo proxy por consistência, mas não tem essa restrição.

## Aeronaves e navios: por que ainda pode faltar cobertura em algumas regiões

- **Aeronaves**: adsb.lol e adsb.fi cobrem trânsito civil por uma lista de
  ~35 hubs de alta densidade (aeroportos/corredores), não o globo inteiro
  homogeneamente. Regiões fora desses hubs (rural, oceano aberto) tendem a
  mostrar menos tráfego civil, ainda que militares (`/mil`, cobertura global)
  apareçam normalmente. Aumentar `HUB_POINTS` em `aircraft_service.py` melhora
  a cobertura de uma região específica.
- **Navios**: sem `AISSTREAM_API_KEY`, a cobertura real cobre Báltico,
  Noruega, Ormuz e várias zonas de alta densidade via Open Waters/aiscast
  (Canal da Mancha, Singapura, Costa Leste EUA, Tóquio, Suez, Roterdã,
  Cabo, Hong Kong). Ainda não há API AIS global gratuita sem cadastro;
  com a chave AISstream (gratuita em aisstream.io) a cobertura passa a ser mundial.
