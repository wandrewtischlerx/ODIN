# INFRAFISICA · Infraestrutura física global (energia + internet)

Plano de implementação da nova camada do WTX Global Monitor cobrindo a
"espinha dorsal física" do planeta: cabos submarinos, datacenters, e
usinas de geração de energia (eólica, hidrelétrica, nuclear, a carvão,
solar). Segue o mesmo padrão das camadas existentes (AIRCRAFT, SHIPS,
SATELLITES, CAMERAS): backend em `services/` + `routes/`, frontend em
`static/js/`, toggle no HUD, e `is_live` / `source` em toda resposta da
API.

Diferença importante em relação às camadas já existentes: aeronaves e
navios se movem em tempo real; infraestrutura física **não se move**.
Aqui "ao vivo" significa "consultado ao vivo na fonte" (contagens,
status de operação, novas unidades), não posição em tempo real — os
dados podem ser cacheados por muito mais tempo (dias, não segundos).

Está dividido em blocos pequenos e independentes. Cada bloco pode ser
implementado, testado e mergeado sozinho sem depender dos seguintes.

---

## Bloco 0 — Fundação comum (fazer primeiro, uma vez só) ✅ implementado

Estrutura compartilhada que todos os blocos seguintes vão reusar, em vez
de cada camada reinventar cache/rotas.

- [x] `app/services/infra_service.py`
  - Helper genérico `cached(key, ttl_seconds, loader)` — cache em
    memória com TTL (dict global + lock), mesmo espírito do que já
    existe em `ship_service.py` / `camera_service.py`, só que fatorado
    uma vez só para qualquer sub-camada de infraestrutura reusar.
  - Datasets estáticos grandes (usinas por enquanto) usam TTL de 6h —
    bem diferente do TTL de segundos das camadas dinâmicas.
- [x] `app/routes/infrastructure.py`
  - Um blueprint só, com sub-rotas por tipo: `/api/infrastructure/power-plants`
    (implementada), `/…/cables` e `/…/datacenters` (stub HTTP 501 —
    Blocos 1 e 3, ainda não feitos, mas já com o formato de resposta
    definido para não precisar quebrar contrato depois).
  - Registrado em `app/__init__.py` com
    `url_prefix="/api/infrastructure"`.
  - Mesmo formato de resposta das demais camadas: `source`, `is_live`,
    `count`, mais o array de itens (`plants`/`cables`/`datacenters`).
- [x] `app/static/js/infrastructure.js`
  - Por enquanto só a parte de usinas está implementada (pontos via
    `Cesium.PointPrimitiveCollection`, cor por tipo de combustível,
    tamanho por `sqrt(capacidade_mw)`, amostragem por viewport igual
    ships.js). Cabos (polylines) e datacenters ficam para quando os
    Blocos 1 e 3 forem implementados — o arquivo já tem um comentário
    marcando onde entram.
- [x] HUD (`main.js` + `index.html` + `style.css`): checkbox "USINAS DE
      ENERGIA" + 5 sub-checkboxes (uma por tipo de combustível), cada
      uma com contador ao lado. Blocos 1 e 3 já acrescentaram os
      checkboxes "CABOS SUBMARINOS" e "DATACENTERS" nesta mesma
      estrutura.

---

## Bloco 1 — Cabos submarinos de internet ✅ implementado

**Fonte:** API pública v3 da TeleGeography (a mesma que alimenta
`submarinecablemap.com`), publicada também no GitHub e espelhada em
vários portais (ex. `services.arcgis.com/.../SubmarineCables`,
`dataportal.saeri.org`). Dois arquivos GeoJSON:

| Arquivo | Conteúdo |
|---|---|
| `cable-geo.json` | Um `MultiLineString` por cabo (rota completa) |
| `landing-point-geo.json` | Pontos de aterrissagem (cidade/país onde o cabo sai do mar) |

Uso: **não comercial**, atribuição à TeleGeography obrigatória (feita
via `title` no checkbox do HUD e na flag do popup — ver abaixo).

- [x] `get_submarine_cables()` / `get_cable_landing_points()` em
      `infra_service.py`: **decisão diferente do plano original** —
      em vez de cópia estática versionada, faz fetch ao vivo dos dois
      GeoJSON (`requests.get`, cache de 24h via `cached()` do Bloco 0),
      seguindo o MESMO padrão de fetch-com-fallback já usado em
      `ship_service.py` (DigiTraffic/Kystverket/Hormuz/Ifremer) — faz
      sentido aqui porque a TeleGeography realmente expõe uma API HTTP
      viva (ao contrário do GPPD do Bloco 2, que é só um dump CSV sem
      endpoint de consulta). Se o fetch falhar (rede fora do ar, API
      mudou de formato), cai para um catálogo demo pequeno mas com
      cabos e pontos **reais** (coordenadas coletadas da própria API,
      não inventadas) — `source="demo"`, mesmo contrato de `is_live`
      das outras camadas. Normaliza para `{id, name, color,
      coordinates[]}` (cabos) e `{id, name, lon, lat}` (pontos) — sem
      `owners`/`rfs_year` como o plano original previa, porque esses
      campos só existem no endpoint por-cabo individual da API
      (`/api/v3/cable/{id}.json`), não no GeoJSON agregado; buscar um
      por um para ~600 cabos não compensa para uma camada que só
      mostra rota no globo. Ver bloco "Fontes descartadas" abaixo.
- [x] Rota `GET /api/infrastructure/cables` — devolve cabos e pontos
      de aterrissagem juntos na mesma resposta (`cables`,
      `landing_points`, `landing_point_count`, `attribution`), porque
      no frontend eles sempre aparecem juntos, controlados pelo mesmo
      checkbox.
- [x] Frontend (`infrastructure.js`): `Cesium.CustomDataSource` com uma
      entidade `polyline` por segmento de cada `MultiLineString`
      (`arcType: NONE`, altura 0 — "cola" no oceano), com o mesmo corte
      na antimeridiana e densificação linear de `boundaries.js`. Cor
      vem do próprio GeoJSON (`cable.color`). Pontos de aterrissagem
      como `Cesium.PointPrimitiveCollection` (ciano, mesmo raio fixo —
      não há "capacidade" por ponto individual para variar o tamanho
      como nas usinas). Sem amostragem por viewport: volume total
      (cabos + pontos) é bem menor que usinas/câmeras.
- [x] Popup ao clicar em cabo ou ponto de aterrissagem: nome + fonte +
      flag REAL/DEMO (`renderCableInfo` / `renderCableLandingPointInfo`
      em `main.js`) — mais magro que o de usinas porque o GeoJSON
      agregado não traz donos/capacidade/ano (ver acima).
- [x] Créditos: `title` no checkbox "CABOS SUBMARINOS" do HUD
      ("Fonte: TeleGeography Submarine Cable Map — uso não comercial")
      e a mesma frase no popup de cada cabo/ponto — este projeto não
      tem um painel de legenda/atribuição central (o
      `creditContainer` do Cesium é redirecionado para uma `div`
      solta, fora da tela, em `globe.js`), então a atribuição fica no
      ponto de contato mais direto com o dado em vez de um painel novo
      só para isso.

Extensão opcional (bloco 1b, depois): pontos de troca de tráfego
(Internet Exchange Points / IXPs) via **API pública do PeeringDB**
(`www.peeringdb.com/api/ix`, sem chave, JSON) — complementa os cabos
mostrando onde as redes se interconectam fisicamente em terra.

---

## Bloco 2 — Usinas de energia (eólica, hidrelétrica, nuclear, carvão, solar) ✅ implementado

Em vez de 5 integrações separadas, **uma fonte única cobre os 5 tipos**:
o **Global Power Plant Database (GPPD)**, mantido por WRI + Google +
KTH + Global Energy Observatory. CSV aberto (CC-BY 4.0), ~35.000
usinas em 167 países, com `latitude`, `longitude`, `capacitymw`,
`fuel1` (Coal, Gas, Hydro, Nuclear, Solar, Wind, Oil, Biomass,
Geothermal, Waste, …), ano de entrada em operação e dono.

- [x] Baixado `global_power_plant_database.csv` (mirror oficial no
      GitHub `wri/global-power-plant-database`, branch `master`,
      `output_database/global_power_plant_database.csv`), filtrado para
      os 5 tipos pedidos (25.690 de ~34.900 usinas no total: 10.665
      solares, 7.156 hidrelétricas, 5.344 eólicas, 2.330 a carvão, 195
      nucleares) e salvo já enxuto em
      `app/static/data/global_power_plants.json` (~5,9 MB) — mesmo
      padrão do `skylinewebcams.json` que já existia, porque não há API
      de consulta ao vivo, é um dump versionado.
- [x] `get_power_plants(fuel_filter=None)` em `infra_service.py`: usa o
      `cached()` do Bloco 0, filtra por
      `fuel in {"hydro","nuclear","coal","solar","wind"}`. Os outros
      tipos do GPPD (gás, óleo, biomassa, geotérmica, resíduos) ficam
      de fora do JSON gerado — não é filtro em tempo de request, já
      foram descartados na conversão do CSV.
- [x] Rota `GET /api/infrastructure/power-plants?type=hydro,nuclear,coal,solar,wind`
      (filtro por querystring, aceita lista separada por vírgula; sem
      filtro retorna os 5 juntos). `counts_by_fuel` na resposta sempre
      reflete o TOTAL do dataset, não o filtro aplicado — é o que a UI
      usa para mostrar a contagem ao lado de cada checkbox mesmo com
      outras desligadas.
- [x] Frontend: um único `PointPrimitiveCollection`, cor fixa por tipo
      (hydro azul, nuclear vermelho, coal cinza, solar laranja, wind
      verde), raio do ponto proporcional a `sqrt(capacity_mw)` para não
      deixar Three Gorges (22.500 MW) do mesmo tamanho que uma usina
      solar de bairro. 5 checkboxes independentes (`pp-hydro`,
      `pp-nuclear`, `pp-coal`, `pp-solar`, `pp-wind`) ligam/desligam
      cada tipo sem precisar recarregar dados.
- [x] Popup por usina ao clicar: nome, tipo, capacidade (MW), país, ano
      de entrada em operação, dono — mesmo padrão visual das demais
      camadas (`renderPowerPlantInfo` em `main.js`).

Extensão opcional (bloco 2b, depois): complementar com **OSM Overpass
API** (`power=plant` + `plant:source=wind|hydro|nuclear|coal|solar`,
sem chave, `overpass-api.de/api/interpreter`) para pegar usinas novas
que ainda não entraram no GPPD (que é atualizado com pouca frequência,
última versão pública de 2021) — mesmo espírito de "fonte primária +
fallback/complemento" já usado em SHIPS e AIRCRAFT.

---

## Bloco 3 — Datacenters ✅ implementado

Não existe uma "TeleGeography grátis" equivalente para datacenters — a
TeleGeography tem um Data Center Research Map, mas é pago/licenciado.
A fonte aberta viável é o **OpenStreetMap via Overpass API**
(`telecom=data_center` e `building=data_center`), sem chave.

- [x] `get_datacenters()` em `infra_service.py`: query Overpass QL
      (`node/way["telecom"="data_center"]` + `["building"="data_center"]`,
      bbox global, `out center` — só precisamos do centróide de cada
      elemento, não a geometria completa do prédio), normaliza nó/way
      em ponto único, cache de 24h (`_DATACENTER_TTL`, igual TTL
      sugerido no Bloco 0).
- [x] Rota `GET /api/infrastructure/datacenters`.
- [x] Frontend: pontos via `Cesium.PointPrimitiveCollection` (mesma
      base técnica das usinas), com amostragem por viewport igual
      `cameras.js`/usinas — necessário porque a cobertura do OSM para
      esta tag é muito concentrada em poucas regiões, então mesmo o
      volume total (bem menor que usinas) fica desigual na tela.
      **Diferença do plano original:** em vez de um ícone desenhado de
      "prédio/servidor", usa um ponto colorido (roxo) simples — mesma
      linguagem visual do resto da camada de infraestrutura, sem
      introduzir um estilo novo só para uma sub-camada.
- [x] Nota clara na UI (`title` no checkbox, mesmo padrão usado para a
      atribuição de cabos no Bloco 1) e neste documento: cobertura no
      OSM é **desigual por região** (EUA/Europa muito mais mapeados
      que Ásia/África/América Latina) — mesmo tipo de ressalva que já
      existe hoje para aeronaves fora dos hubs e câmeras por país.
- [x] **Fallback quando o Overpass falha** (fora do ar, sobrecarregado
      — é notoriamente instável sob carga — ou rede sem saída):
      diferente do fallback de cabos (Bloco 1), aqui não há uma
      amostra real do Overpass à disposição para usar como catálogo
      demo. Em vez de inventar coordenadas de datacenter fingindo que
      vieram do OSM, implementamos aqui o que o plano original já
      previa como **extensão opcional "Bloco 3b"**: uma pequena lista
      curada à mão (`_DEMO_CURATED_DATACENTERS`, 12 entradas) dos
      megacampi de nuvem/IA mais divulgados publicamente pelas
      próprias empresas e pela imprensa (Google em The Dalles/Council
      Bluffs/Eemshaven, Meta em Prineville/Luleå/Odense, Microsoft
      Azure em Quincy/Boydton, AWS em Ashburn, Equinix em Singapura,
      clusters de Gui'an e Dublin). Coordenadas são aproximadas (nível
      de cidade/região, não o endereço exato do prédio) — cada entrada
      tem `"approx": true` e o conjunto usa `source="demo_curated"`
      (não o `"demo"` genérico das outras camadas) para deixar
      explícito que não é uma amostra do dataset real. O frontend
      desenha esses pontos com opacidade reduzida e o popup mostra
      "LISTA CURADA · LOCAL APROXIMADO" em vez da flag "AO VIVO".

Extensão opcional (bloco 3b) → **já incorporada acima** como fallback,
em vez de ficar como camada separada — não fazia sentido manter uma UI
para "megacampi curados" à parte de "datacenters do OSM" quando o
catálogo curado só aparece justamente quando não há dados do OSM.

---

## Bloco 4 — Diagnóstico e documentação

- [ ] Estender `app/routes/diagnostics.py` com um bloco `infrastructure`
      (mesmo formato de `connectivity`/`schema_probes`/`layers` já
      existente): testar se o TeleGeography GeoJSON, o CSV do GPPD e o
      Overpass respondem, e contar quantos itens cada um devolveu.
- [ ] Atualizar `README.md` (tabela "Estado atual dos dados por
      camada") acrescentando as 3 novas linhas (Cabos, Usinas,
      Datacenters) no mesmo formato REAL/DEMO já usado.
- [ ] Atualizar `SOURCES.md` com uma seção nova "Infraestrutura física"
      listando as fontes acima e as que foram pesquisadas e descartadas
      (ex. TeleGeography Data Center Map — pago; AISHub-like exigências
      de reciprocidade, se surgirem).

---

## Ordem sugerida

1. Bloco 0 (fundação) → 2. Bloco 2 (usinas, fonte única e mais simples,
bom para validar o padrão) → 3. Bloco 1 (cabos) → 4. Bloco 3
(datacenters, o mais trabalhoso por causa do Overpass) → 5. Bloco 4
(diagnóstico/docs). Cada bloco fecha com o app rodando e no mesmo
estado "utilizável" de antes — nenhum bloco deixa a aplicação quebrada
no meio do caminho.

Estado atual: Blocos 0, 1, 2 e 3 implementados. Falta só o Bloco 4
(diagnóstico/docs).

## Fontes descartadas (para não pesquisar de novo)

- **Cabos submarinos via API paga da TeleGeography** (JSON/S3
  licenciado) — existe, mas é a versão comercial; o GeoJSON público já
  cobre bem o caso de uso de visualização.
- **Endpoint por-cabo individual da TeleGeography** (`/api/v3/cable/{id}.json`,
  que traz `owners`/`rfs_year`/capacidade) — existe e é público, mas
  buscar um por um para ~600 cabos só para enriquecer o popup não
  compensa o custo (~600 requests extras por refresh de cache) para
  uma camada cujo objetivo principal é mostrar a rota no globo; o
  GeoJSON agregado (`cable-geo.json`) já basta para isso. Ver Bloco 1.
- **Datacenters via TeleGeography Data Center Research Map** — pago,
  nível de detalhe por prédio; fora de escopo para uma fonte sem chave.
- **thewindpower.net / bases comerciais de parques eólicos por
  turbina individual** — o GPPD já cobre parques eólicos como usina
  agregada (suficiente para o zoom de globo; detalhe por turbina não
  compensa o custo/complexidade agora).
