/*
 * boundaries.js
 * Camada de divisao politica (fronteiras de paises, de estados/
 * provincias e nomes de cidades).
 *
 * ------------------------------------------------------------------
 * POR QUE ESTE ARQUIVO FOI REESCRITO (bug "RangeError: invalid array
 * length" / "An error occurred while rendering. Rendering has stopped.")
 * ------------------------------------------------------------------
 * A versao anterior carregava os POLIGONOS do Natural Earth
 * (ne_50m_admin_0_countries / ne_50m_admin_1_states_provinces_lakes)
 * com Cesium.GeoJsonDataSource e pedia `polygon.fill = false` +
 * `polygon.outline = true`. Isso obriga o Cesium a construir, DENTRO
 * do loop de render, uma PolygonOutlineGeometry para cada pais/estado:
 * ele projeta o poligono num plano tangente, calcula winding order,
 * e subdivide cada aresta com
 *
 *     numVertices = 2 ^ ceil(log2(distancia / minDistance))
 *     result = new Array(numVertices * 3)
 *
 * Quando um poligono do Natural Earth tem vertice invalido/repetido ou
 * uma aresta atravessando a antimeridiana, `distancia` vira NaN ou um
 * numero enorme; `new Array(NaN)` / `new Array(gigante)` lanca
 * exatamente "RangeError: invalid array length". Como isso acontece
 * dentro do render loop, o Cesium nao consegue se recuperar e mostra o
 * dialogo "Rendering has stopped" - o globo inteiro congela, e nao so a
 * camada de divisoes (foi o que apareceu no seu print).
 *
 * A correcao NAO e' tentar limpar o poligono: e' nao usar poligono
 * nenhum. Fronteira e' linha, nao area. Agora usamos os datasets de
 * LINHA do proprio Natural Earth e montamos as polilinhas na mao:
 *
 *   - Paises (linha):   ne_50m_admin_0_boundary_lines_land  (~740 KB,
 *                        390 feicoes, 19.8 mil pontos)
 *   - Estados (linha):  ne_50m_admin_1_states_provinces_lines (~860 KB,
 *                        581 feicoes, 16.6 mil pontos)
 *
 * Alem de nao quebrar, ficou MUITO mais leve: ~985 polilinhas em vez de
 * milhares de poligonos triangulados, e ~1,6 MB em vez de ~5,4 MB de
 * GeoJSON. Tres garantias adicionais contra o crash:
 *
 *   1) `arcType: Cesium.ArcType.NONE` - desliga a subdivisao geodesica
 *      do Cesium (o codigo que lancava o RangeError). Em troca, nos
 *      mesmos densificamos cada segmento aqui, em passos de no maximo
 *      0,35 grau, o que e' deterministico e barato.
 *   2) Cada coordenada e' validada (numero finito, lat <= 90,
 *      lon <= 180) antes de virar posicao; coordenada ruim corta a
 *      linha em vez de contaminar a geometria.
 *   3) Linha que cruza a antimeridiana (salto de +/-180 graus de
 *      longitude entre dois pontos) e' cortada em duas, em vez de dar
 *      a volta no planeta.
 *
 * As linhas ficam a 300 m de altitude (nao em 0) porque o maior erro de
 * "corda" depois da densificacao e' ~60 m - assim a fronteira nunca
 * afunda dentro da esfera e some.
 *
 * ------------------------------------------------------------------
 * ROTULOS (nomes) - carregados por etapa, so quando servem
 * ------------------------------------------------------------------
 * Antes, tudo era baixado de uma vez (inclusive 4,9 MB de cidades) mesmo
 * com a camera no espaco, onde nenhum nome de cidade aparece. Agora:
 *
 *   Etapa 1 (ao ligar a camada):  linhas de pais + linhas de estado +
 *       nomes de pais (ne_110m_admin_0_countries, 177 feicoes, usa o
 *       ponto de rotulo oficial LABEL_X/LABEL_Y - nao um centroide
 *       calculado, que cairia no oceano em paises com ilhas).
 *   Etapa 2 (camera abaixo de 2.500 km): nomes de estado/provincia
 *       (ne_10m_admin_1_label_points, 11.291 pontos no mundo inteiro -
 *       cobre muito mais que o dataset 50m antigo, que so tinha 294
 *       regioes de 9 paises).
 *   Etapa 3 (camera abaixo de 1.200 km): nomes de cidade/municipio
 *       (ne_10m_populated_places_simple, 7.3 mil pontos).
 *
 * Os rotulos de estado e de cidade sao recriados a cada parada da
 * camera, filtrados pelo retangulo visivel e limitados a algumas
 * centenas por vez. Isso evita o outro jeito de derrubar o render:
 * milhares de Labels simultaneos estouram o atlas de textura de texto
 * do Cesium.
 *
 * OBS. sobre "municipios": continua nao existindo base global leve e
 * gratuita com o POLIGONO do limite municipal (as que cobrem o planeta
 * pesam gigabytes). Cidade/municipio aparece como ponto + nome.
 */

window.WTX = window.WTX || {};

(function boundariesLayer() {
  const BASE =
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/";

  const COUNTRY_LINES_URL = BASE + "ne_50m_admin_0_boundary_lines_land.geojson";
  const STATE_LINES_URL = BASE + "ne_50m_admin_1_states_provinces_lines.geojson";
  const COUNTRY_LABELS_URL = BASE + "ne_110m_admin_0_countries.geojson";
  const STATE_LABELS_URL = BASE + "ne_10m_admin_1_label_points.geojson";
  const CITIES_URL = BASE + "ne_10m_populated_places_simple.geojson";

  // Altura das linhas acima do elipsoide (ver comentario do cabecalho).
  const LINE_HEIGHT = 300;
  // Passo maximo de densificacao, em graus.
  const MAX_STEP_DEG = 0.35;
  // Teto de subdivisao por segmento - blindagem contra dado corrompido.
  const MAX_SUBDIV = 128;

  // Distancias (em metros) de visibilidade de cada nivel.
  const COUNTRY_LINE_MAX = 60000000; // fronteira de pais: sempre visivel
  const COUNTRY_LABEL_MAX = 20000000;
  const STATE_LINE_MAX = 4000000;
  const STATE_LABEL_MAX = 2500000;
  const CITY_LOAD_MAX = 1200000;

  const CITY_TIERS = [
    { maxRank: 1, distance: 700000, size: 5 },
    { maxRank: 3, distance: 350000, size: 4.5 },
    { maxRank: 5, distance: 180000, size: 4 },
    { maxRank: 7, distance: 90000, size: 3.5 },
    { maxRank: 99, distance: 40000, size: 3 },
  ];

  const COLOR_COUNTRY = Cesium.Color.fromCssColorString("#f2d94e").withAlpha(0.9);
  const COLOR_STATE = Cesium.Color.fromCssColorString("#8fae63").withAlpha(0.65);
  const CITY_POINT_COLOR = Cesium.Color.fromCssColorString("#7fd6e8");
  const CITY_LABEL_COLOR = Cesium.Color.fromCssColorString("#e8f6f9");
  const LABEL_OUTLINE = Cesium.Color.fromCssColorString("#0b0c0d");

  const MAX_STATE_LABELS = 250;
  const MAX_CITY_LABELS = 450;

  let visible = false;
  let started = false;

  let lineSource = null;   // linhas de pais + estado (estatico)
  let labelSource = null;  // rotulos (recriado por viewport)

  let countryLabels = [];  // [{name, lon, lat}]
  let stateLabels = [];    // [{name, lon, lat, rank}]
  let cityPoints = [];     // [{name, lon, lat, rank, tier, capital}]

  let stateLabelsStarted = false;
  let citiesStarted = false;
  let moveEndTimer = null;

  // ---------- helpers de geometria ----------

  function validLonLat(c) {
    return (
      Array.isArray(c) &&
      c.length >= 2 &&
      Number.isFinite(c[0]) &&
      Number.isFinite(c[1]) &&
      c[0] >= -180.5 &&
      c[0] <= 180.5 &&
      c[1] >= -90.5 &&
      c[1] <= 90.5
    );
  }

  /* Densifica o segmento a->b direto no array plano de saida. Passos
     lineares em lon/lat: previsivel, sem chamar a subdivisao geodesica
     do Cesium (que e' onde nascia o RangeError). */
  function pushDensified(flat, a, b) {
    const dLon = b[0] - a[0];
    const dLat = b[1] - a[1];
    let steps = Math.ceil(
      Math.max(Math.abs(dLon), Math.abs(dLat)) / MAX_STEP_DEG
    );
    if (!Number.isFinite(steps) || steps < 1) steps = 1;
    if (steps > MAX_SUBDIV) steps = MAX_SUBDIV;
    for (let i = 1; i <= steps; i++) {
      flat.push(a[0] + (dLon * i) / steps, a[1] + (dLat * i) / steps, LINE_HEIGHT);
    }
  }

  /* Converte um anel/linha do GeoJSON em 1..N arrays planos
     [lon,lat,alt, ...], cortando na antimeridiana e em coordenada
     invalida. */
  function toFlatSegments(line) {
    const segments = [];
    let flat = null;
    let prev = null;
    for (const c of line) {
      if (!validLonLat(c)) {
        prev = null;
        flat = null;
        continue;
      }
      const cur = [c[0], c[1]];
      if (prev === null || Math.abs(cur[0] - prev[0]) > 180) {
        flat = [cur[0], cur[1], LINE_HEIGHT];
        segments.push(flat);
      } else {
        pushDensified(flat, prev, cur);
      }
      prev = cur;
    }
    // precisa de pelo menos 2 pontos (6 numeros) pra ser uma linha
    return segments.filter((f) => f.length >= 6);
  }

  function addLines(geojson, color, width, maxDistance) {
    const ddc = new Cesium.DistanceDisplayCondition(0, maxDistance);
    const features = (geojson && geojson.features) || [];
    let added = 0;

    for (const feature of features) {
      const geom = feature && feature.geometry;
      if (!geom) continue;

      let lines;
      if (geom.type === "LineString") lines = [geom.coordinates];
      else if (geom.type === "MultiLineString") lines = geom.coordinates;
      else continue;
      if (!Array.isArray(lines)) continue;

      for (const line of lines) {
        if (!Array.isArray(line) || line.length < 2) continue;
        for (const flat of toFlatSegments(line)) {
          let positions;
          try {
            positions = Cesium.Cartesian3.fromDegreesArrayHeights(flat);
          } catch (err) {
            continue; // segmento problematico e' descartado, nao derruba o render
          }
          if (!positions || positions.length < 2) continue;
          lineSource.entities.add({
            polyline: {
              positions: positions,
              width: width,
              material: color,
              arcType: Cesium.ArcType.NONE,
              distanceDisplayCondition: ddc,
            },
          });
          added++;
        }
      }
    }
    return added;
  }

  // ---------- rotulos ----------

  function labelGraphics(text, maxDistance, opts) {
    const o = opts || {};
    return {
      text: text,
      font: o.font || "600 13px 'Rajdhani', sans-serif",
      fillColor: o.fill || Cesium.Color.fromCssColorString("#f4f2e8"),
      outlineColor: LABEL_OUTLINE,
      outlineWidth: 3,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      verticalOrigin: o.verticalOrigin || Cesium.VerticalOrigin.CENTER,
      horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
      pixelOffset: o.pixelOffset,
      disableDepthTestDistance: 0,
      distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, maxDistance),
    };
  }

  function viewRectangle() {
    try {
      const viewer = window.WTX.viewer;
      return (
        viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid) || null
      );
    } catch (err) {
      return null;
    }
  }

  function inRect(rect, lon, lat) {
    if (!rect) return false;
    try {
      return Cesium.Rectangle.contains(
        rect,
        Cesium.Cartographic.fromDegrees(lon, lat)
      );
    } catch (err) {
      return false;
    }
  }

  function cameraHeight() {
    try {
      return window.WTX.viewer.camera.positionCartographic.height;
    } catch (err) {
      return Number.POSITIVE_INFINITY;
    }
  }

  function tierForRank(rank) {
    for (const tier of CITY_TIERS) {
      if (rank <= tier.maxRank) return tier;
    }
    return CITY_TIERS[CITY_TIERS.length - 1];
  }

  /* Recria os rotulos visiveis. Chamado ao ligar a camada e a cada
     parada da camera (com debounce). Recriar e' mais barato e MUITO
     mais seguro do que manter 18 mil Labels vivos: o atlas de textura
     de texto do Cesium tem limite, e estourar esse limite e' a outra
     forma classica de matar o render loop. */
  function rebuildLabels() {
    if (!visible || !labelSource) return;

    const height = cameraHeight();
    const rect = viewRectangle();
    const entities = labelSource.entities;

    entities.suspendEvents();
    try {
      entities.removeAll();

      // Paises: poucos (177), a propria distanceDisplayCondition filtra.
      for (const c of countryLabels) {
        entities.add({
          position: Cesium.Cartesian3.fromDegrees(c.lon, c.lat),
          label: labelGraphics(c.name, COUNTRY_LABEL_MAX),
        });
      }

      // Estados: so com a camera perto, so os do retangulo visivel.
      if (height < STATE_LABEL_MAX && stateLabels.length && rect) {
        let n = 0;
        for (const s of stateLabels) {
          if (n >= MAX_STATE_LABELS) break;
          if (!inRect(rect, s.lon, s.lat)) continue;
          entities.add({
            position: Cesium.Cartesian3.fromDegrees(s.lon, s.lat),
            label: labelGraphics(s.name, STATE_LABEL_MAX, {
              font: "600 12px 'Rajdhani', sans-serif",
              fill: Cesium.Color.fromCssColorString("#cfe6a8"),
            }),
          });
          n++;
        }
      }

      // Cidades: ponto + nome, tambem so no retangulo visivel.
      if (height < CITY_LOAD_MAX && cityPoints.length && rect) {
        let n = 0;
        for (const p of cityPoints) {
          if (n >= MAX_CITY_LABELS) break;
          if (!inRect(rect, p.lon, p.lat)) continue;
          entities.add({
            position: Cesium.Cartesian3.fromDegrees(p.lon, p.lat),
            point: {
              pixelSize: p.tier.size,
              color: CITY_POINT_COLOR,
              outlineColor: Cesium.Color.BLACK,
              outlineWidth: 1,
              disableDepthTestDistance: 0,
              distanceDisplayCondition: new Cesium.DistanceDisplayCondition(
                0,
                p.tier.distance
              ),
            },
            label: labelGraphics(p.name, p.tier.distance, {
              font: p.capital
                ? "600 12px 'Rajdhani', sans-serif"
                : "500 11px 'Rajdhani', sans-serif",
              fill: CITY_LABEL_COLOR,
              verticalOrigin: Cesium.VerticalOrigin.TOP,
              pixelOffset: new Cesium.Cartesian2(0, 6),
            }),
          });
          n++;
        }
      }
    } finally {
      entities.resumeEvents();
    }
  }

  // ---------- carregamento por etapa ----------

  async function fetchJson(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${res.status} ${url}`);
    return res.json();
  }

  async function loadStage1() {
    const [countryLines, stateLines, countryLabelGeo] = await Promise.allSettled([
      fetchJson(COUNTRY_LINES_URL),
      fetchJson(STATE_LINES_URL),
      fetchJson(COUNTRY_LABELS_URL),
    ]);

    let ok = false;

    if (countryLines.status === "fulfilled") {
      addLines(countryLines.value, COLOR_COUNTRY, 2, COUNTRY_LINE_MAX);
      ok = true;
    } else {
      console.warn("Falha ao carregar fronteiras de paises.", countryLines.reason);
    }

    if (stateLines.status === "fulfilled") {
      addLines(stateLines.value, COLOR_STATE, 1, STATE_LINE_MAX);
      ok = true;
    } else {
      console.warn("Falha ao carregar fronteiras de estados.", stateLines.reason);
    }

    if (countryLabelGeo.status === "fulfilled") {
      const feats = (countryLabelGeo.value && countryLabelGeo.value.features) || [];
      countryLabels = [];
      for (const f of feats) {
        const p = f.properties || {};
        const name = p.NAME_EN || p.NAME || p.ADMIN;
        // LABEL_X/LABEL_Y e' o ponto de rotulo escolhido pelos
        // cartografos do Natural Earth - cai sempre sobre o territorio,
        // ao contrario de um centroide calculado (que ia parar no mar
        // em paises com arquipelago ou cortados pela antimeridiana).
        if (!name || !Number.isFinite(p.LABEL_X) || !Number.isFinite(p.LABEL_Y)) continue;
        countryLabels.push({ name: name, lon: p.LABEL_X, lat: p.LABEL_Y });
      }
      ok = true;
    } else {
      console.warn("Falha ao carregar nomes de paises.", countryLabelGeo.reason);
    }

    rebuildLabels();
    if (window.WTX.updateBadge) window.WTX.updateBadge("boundaries", ok);
  }

  async function loadStateLabels() {
    try {
      const geo = await fetchJson(STATE_LABELS_URL);
      const feats = (geo && geo.features) || [];
      const out = [];
      for (const f of feats) {
        const g = f.geometry;
        const p = f.properties || {};
        if (!g || g.type !== "Point" || !validLonLat(g.coordinates)) continue;
        const name = p.name || p.name_en;
        if (!name) continue;
        out.push({
          name: name,
          lon: g.coordinates[0],
          lat: g.coordinates[1],
          rank: typeof p.scalerank === "number" ? p.scalerank : 10,
        });
      }
      out.sort((a, b) => a.rank - b.rank); // mais importantes primeiro no corte
      stateLabels = out;
      rebuildLabels();
    } catch (err) {
      console.warn("Falha ao carregar nomes de estados/provincias.", err);
    }
  }

  async function loadCities() {
    try {
      const geo = await fetchJson(CITIES_URL);
      const feats = (geo && geo.features) || [];
      const out = [];
      for (const f of feats) {
        const g = f.geometry;
        const p = f.properties || {};
        if (!g || g.type !== "Point" || !validLonLat(g.coordinates)) continue;
        const name = p.name || p.nameascii;
        if (!name) continue;
        const rank = typeof p.scalerank === "number" ? p.scalerank : 10;
        out.push({
          name: name,
          lon: g.coordinates[0],
          lat: g.coordinates[1],
          rank: rank,
          tier: tierForRank(rank),
          capital: p.adm0cap === 1 || p.worldcity === 1,
        });
      }
      out.sort((a, b) => a.rank - b.rank);
      cityPoints = out;
      rebuildLabels();
    } catch (err) {
      console.warn("Falha ao carregar nomes de cidades/municipios.", err);
    }
  }

  function maybeLoadMore() {
    const h = cameraHeight();
    if (h < STATE_LABEL_MAX && !stateLabelsStarted) {
      stateLabelsStarted = true;
      loadStateLabels();
    }
    if (h < CITY_LOAD_MAX && !citiesStarted) {
      citiesStarted = true;
      loadCities();
    }
  }

  function onCameraMoveEnd() {
    if (!visible) return;
    if (moveEndTimer) clearTimeout(moveEndTimer);
    moveEndTimer = setTimeout(() => {
      maybeLoadMore();
      rebuildLabels();
    }, 300);
  }

  function applyVisibility() {
    if (lineSource) lineSource.show = visible;
    if (labelSource) labelSource.show = visible;
  }

  function start() {
    const viewer = window.WTX.viewer;
    if (!viewer) {
      setTimeout(start, 200);
      return;
    }
    lineSource = new Cesium.CustomDataSource("wtx-boundary-lines");
    labelSource = new Cesium.CustomDataSource("wtx-boundary-labels");
    viewer.dataSources.add(lineSource);
    viewer.dataSources.add(labelSource);
    applyVisibility();

    viewer.camera.moveEnd.addEventListener(onCameraMoveEnd);

    loadStage1();
    maybeLoadMore();
  }

  window.WTX.boundariesLayer = {
    setVisible(v) {
      visible = v;
      applyVisibility();
      if (v && !started) {
        started = true;
        start();
      } else if (v) {
        maybeLoadMore();
        rebuildLabels();
      }
    },
    // Usado pelo buscador (main.js): nomes de pais/cidade pra' pesquisa
    // por texto, mesmo com a camada de fronteiras desligada. Nao
    // desenha nada no globo - so' devolve o que ja foi carregado.
    searchPlaces(query) {
      const q = String(query || "").trim().toLowerCase();
      if (!q) return [];
      const out = [];
      for (const c of countryLabels) {
        if (c.name.toLowerCase().includes(q)) {
          out.push({ type: "country", name: c.name, lon: c.lon, lat: c.lat });
        }
      }
      for (const c of cityPoints) {
        if (c.name.toLowerCase().includes(q)) {
          out.push({ type: "city", name: c.name, lon: c.lon, lat: c.lat, rank: c.rank });
        }
      }
      out.sort((a, b) => (a.rank || 0) - (b.rank || 0));
      return out;
    },
  };

  // BUGFIX: nomes de pais/cidade so' carregavam quando o usuario ligava
  // a camada "DIVISÕES" - isso deixava o buscador incapaz de achar
  // cidade/pais antes disso (unico jeito de buscar era a lista fixa de
  // 9 cidades no main.js). Agora os NOMES (sem as linhas de fronteira,
  // que sao pesadas e so' fazem sentido com a camada ligada) sao
  // buscados uma vez em segundo plano assim que a pagina carrega, pra'
  // o buscador funcionar desde o inicio independente da camada.
  async function preloadNamesForSearch() {
    try {
      const [countryLabelGeo, citiesGeo] = await Promise.allSettled([
        fetchJson(COUNTRY_LABELS_URL),
        fetchJson(CITIES_URL),
      ]);
      if (countryLabelGeo.status === "fulfilled" && !countryLabels.length) {
        const feats = (countryLabelGeo.value && countryLabelGeo.value.features) || [];
        const out = [];
        for (const f of feats) {
          const p = f.properties || {};
          const name = p.NAME_EN || p.NAME || p.ADMIN;
          if (!name || !Number.isFinite(p.LABEL_X) || !Number.isFinite(p.LABEL_Y)) continue;
          out.push({ name: name, lon: p.LABEL_X, lat: p.LABEL_Y });
        }
        countryLabels = out;
      }
      if (citiesGeo.status === "fulfilled" && !cityPoints.length) {
        const feats = (citiesGeo.value && citiesGeo.value.features) || [];
        const out = [];
        for (const f of feats) {
          const g = f.geometry;
          const p = f.properties || {};
          if (!g || g.type !== "Point" || !validLonLat(g.coordinates)) continue;
          const name = p.name || p.nameascii;
          if (!name) continue;
          const rank = typeof p.scalerank === "number" ? p.scalerank : 10;
          out.push({
            name: name,
            lon: g.coordinates[0],
            lat: g.coordinates[1],
            rank: rank,
            tier: tierForRank(rank),
            capital: p.adm0cap === 1 || p.worldcity === 1,
          });
        }
        out.sort((a, b) => a.rank - b.rank);
        cityPoints = out;
      }
    } catch (err) {
      console.warn("Falha ao pre-carregar nomes pro buscador.", err);
    }
  }
  preloadNamesForSearch();
  // Lazy: as LINHAS de fronteira so' baixam quando o usuario liga a
  // camada (preloadNamesForSearch acima cobre so' os nomes, bem mais
  // leve, pro buscador funcionar sem precisar ligar nada).
})();
