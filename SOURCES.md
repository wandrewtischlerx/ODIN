


## Aeronaves (ADS-B)

| Fonte | Auth | Cobertura | Status no app |
|-------|------|-----------|---------------|
| **OpenSky Network** `opensky-network.org/api/states/all` | Sem chave (rate limit) | Global (incl. oceano) | ✅ Integrado — prioritário |
| **adsb.lol** `/v2/mil`, `/v2/ladd`, `/v2/point/...` | Sem chave | Global / hubs | ✅ Integrado |
| **adsb.fi** `/v2/mil`, `/v2/lat/.../lon/...` | Sem chave | Espelho comunitário | ✅ Integrado |
| **airplanes.live** `/v2/mil`, `/v2/point/...` | Sem chave | Global / hubs (3º espelho comunitário) | ✅ Integrado — não testado ao vivo (sandbox sem acesso de rede a esse host) |
| adsb.one | — | — | ❌ 403 |
| ADSB Exchange API | Key / pago | Global | ❌ Pago |
| AviationStack / AeroDataBox | Key freemium | — | ❌ Key |
| ADSB IQ | Feeder | — | ❌ Exige alimentar rede |

## Navios (AIS)

| Fonte | Auth | Cobertura | Status |
|-------|------|-----------|--------|
| **Digitraffic / Fintraffic** | Sem chave | Báltico / FI | ✅ |
| **Kystverket / Kystdatahuset** | Sem chave (NLOD) | Noruega | ✅ |
| **Hormuz Ship Monitor** | Sem chave (CC BY) | Golfo / Ormuz | ✅ |
| **Open Waters / aiscast** REST GeoJSON | Sem chave (bbox limitado) | Multi-região agregada | ✅ (várias bboxes) |
| **IFREMER** frota oceanográfica FR | Sem chave | Navios de pesquisa FR | ✅ |
| **AISstream.io** WebSocket | Key grátis | Global | ✅ Opcional (`AISSTREAM_API_KEY`) |
| AISHub | Reciprocidade (estação) | Global | ❌ Exige alimentar |
| MarineTraffic / VesselFinder / Datalastic | Pago | Global | ❌ |

## Satélites (TLE)

| Fonte | Auth | Status |
|-------|------|--------|
| **SatNOGS DB** TLE API | Sem chave | ✅ Primário |
| **CelesTrak** grupos GP/TLE | Sem chave | ✅ Fallback multi-grupo: active, stations, visual, weather, noaa, goes, gnss, gps-ops, galileo, beidou, resource, science, amateur, cubesat, starlink, oneweb, iridium-NEXT, military |
| Space-Track.org | Conta grátis + ToS | ❌ Conta + restrições de redistribuição |

## Câmeras

| Fonte | Auth | Status |
|-------|------|--------|
| NYC DOT | Sem chave (+ proxy imagem) | ✅ |
| Caltrans CCTV ArcGIS | Sem chave | ✅ |
| UDOT / Ontario 511 / Alberta 511 (CARS) | Sem chave | ✅ |
| TfL JamCam | Sem chave | ✅ |
| CET-SP | Lista estática + frames | ✅ |
| Clima ao Vivo | Sem chave | ✅ |
| Digitraffic weathercam FI | Sem chave | ✅ |
| Florida / Arizona / Georgia / NC / Nevada / Idaho / Utah **mapIcons** | Sem chave | ✅ |
| DelDOT videocameras JSON | Sem chave | ✅ |
| **EarthCam Network** (~300 webcams globais) | Sem chave | ✅ |
| **Webcamera24** mapa (~4k pontos lat/lng) | Sem chave | ✅ (marcadores; sem snapshot em lote) |
| **SkylineWebcams** (~1851 webcams cênicas globais) | Dados embutidos (static/data) | ✅ (coords; YT thumb + link página) |
| **Pennsylvania 511** mapIcons | Sem chave | ✅ |
| **New England 511** mapIcons | Sem chave | ✅ |
| **Alaska 511** mapIcons | Sem chave | ✅ |
| **New York 511** mapIcons | Sem chave | ✅ |
| **Louisiana 511** mapIcons | Sem chave | ✅ |
| **Illinois / Travel Midwest** ArcGIS (~3.6k) | Sem chave | ✅ |
| **Singapore LTA** data.gov.sg traffic-images | Sem chave | ✅ |
| **OpenTrafficCamMap** USA (~7k JPEG+HLS) | Dump embutido (CC community) | ✅ |
| **OpenCCTV** markers (~144k–239k) | Sem chave | ✅ (amostra espacial + viewport; feeds: image, mjpeg, m3u8/HLS, iframe/YouTube, mp4 — não só image) |
| **HK Transport Dept** (China, RAE de Hong Kong) | Sem chave | ⚠️ Endpoint público (data.gov.hk), schema não confirmado ao vivo neste ambiente — parser defensivo, some silenciosamente se o formato mudou |
| **ITS Korea** (Coreia do Sul) | `key=test` (demo) ou key grátis | ⚠️ Corrigido (2026-09): parâmetro `type` não aceita `"all"` — os valores reais são `ex` (rodovias) e `its` (nacionais), agora chamados separadamente. Nomes de campo (`coordx`/`coordy`/`cctvname`/`cctvurl` em `response.data`) confirmados contra exemplo de terceiro testado na API real, mas o endpoint em si **não foi testado ao vivo nesta sessão** (its.go.kr bloqueia a sandbox e o robots.txt bloqueia o `web_fetch` de pesquisa). Se a cota do modo `test` for baixa, cadastrar key grátis em its.go.kr e setar `ITS_KOREA_API_KEY` |
| **Haifa Municipality** (Israel — só a cidade, não o país) | Sem chave | ⚠️ **não testado ao vivo**, mesmo motivo acima. Cobre só Haifa |
| **Taiwan THB** — 國道 (rodovias nacionais) + 省道 (estradas provinciais) | Sem chave | ✅ **Testado ao vivo com sucesso nesta sessão** (`thbapp.thb.gov.tw/services/cctv/freeway` e `/thb`) — milhares de câmeras reais, JSON puro, sem chave. Terceiro endpoint (`/county`, vias municipais) devolveu lista vazia no teste; mantido, tratado como "sem câmeras agora" e não como erro |
| Road511 unificado | Key trial | ❌ Key |
| Windy Webcams | Key | ❌ Key |


## Infraestrutura física (energia + internet)

Ver `INFRAFISICA.md` na raiz do projeto para o plano completo em blocos.

| Fonte | Auth | Cobertura | Status no app |
|-------|------|-----------|---------------|
| **Global Power Plant Database (GPPD)** — WRI + Google + KTH + Global Energy Observatory | Sem chave (CC-BY-4.0), dump CSV baixado 1x e versionado no repo | ~34.900 usinas / 167 países; usadas aqui só hydro/nuclear/coal/solar/wind (25.690) | ✅ Integrado (Bloco 2) |
| **TeleGeography Submarine Cable Map** (GeoJSON aberto, uso não-comercial) | Sem chave | Cabos submarinos + landing points, global | ⏳ Planejado (Bloco 1) |
| **PeeringDB** `peeringdb.com/api/ix` | Sem chave | Internet Exchange Points (IXPs), global | ⏳ Planejado (Bloco 1b, extensão opcional) |
| **OpenStreetMap Overpass API** (`telecom=data_center` / `building=data_center`) | Sem chave | Datacenters mapeados no OSM — cobertura desigual (EUA/Europa >> Ásia/África) | ⏳ Planejado (Bloco 3) |
| TeleGeography Data Center Research Map | Licença paga | Datacenters por prédio | ❌ Pago, fora de escopo |
| thewindpower.net (turbina por turbina) | Pago/parcial | Parques eólicos em detalhe | ❌ GPPD já cobre como usina agregada, suficiente para o zoom de globo |

## Notas de pesquisa

- Não existe API AIS **global** REST gratuita e estável sem cadastro; Open Waters + regionais + AISstream (key grátis) é o melhor conjunto open.
- ADS-B sobre oceano depende sobretudo do **OpenSky** (e satélites AIS comerciais, fora de escopo).
- Câmeras: cada estado US publica schema diferente; mapIcons (Castle Rock) e CARS/511 são os padrões reutilizáveis sem key.
