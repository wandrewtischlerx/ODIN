/*
 * infrastructure.js
 * Camada de infraestrutura física global — ver INFRAFISICA.md na raiz
 * do projeto para o plano completo em blocos.
 *
 * Implementado aqui:
 *   - Bloco 2: USINAS DE ENERGIA — hidrelétrica, nuclear, carvão,
 *     solar e eólica, a partir do Global Power Plant Database (WRI).
 *   - Bloco 1: CABOS SUBMARINOS de internet + pontos de aterrissagem,
 *     a partir da API pública da TeleGeography.
 *   - Bloco 3: DATACENTERS, a partir do OpenStreetMap (Overpass API).
 *
 * Diferença de arquitetura em relação a ships.js/aircraft.js: aqui os
 * pontos NÃO se movem e o catálogo inteiro é baixado uma vez só, sem
 * refresh periódico — não faz sentido reconsultar a cada 45s um dado
 * que muda a cada alguns anos. Usinas e datacenters usam
 * Cesium.PointPrimitiveCollection (mais leve que billboards por
 * entidade) e o mesmo truque de amostragem por viewport já usado em
 * ships.js/cameras.js, necessário pelo volume (25k+ usinas) ou pela
 * concentração regional muito desigual (datacenters no OSM). Cabos
 * usam Cesium.CustomDataSource (precisam de entidades de verdade —
 * polyline por segmento, com corte na antimeridiana igual
 * boundaries.js) e não precisam de amostragem: o volume total (cabos
 * + pontos de aterrissagem) é bem menor.
 */

window.WTX = window.WTX || {};

(function powerPlantsLayer() {
  const FUEL_COLORS = {
    hydro: Cesium.Color.fromCssColorString("#38bdf8"),
    nuclear: Cesium.Color.fromCssColorString("#f87171"),
    coal: Cesium.Color.fromCssColorString("#94a3b8"),
    solar: Cesium.Color.fromCssColorString("#fbbf24"),
    wind: Cesium.Color.fromCssColorString("#34d399"),
  };

  // Ponto: coleção leve, mas ainda assim 25k+ objetos de uma vez
  // pesariam demais no navegador — mesmo teto de amostragem usado em
  // ships.js (MAX_PLOT), só que bem maior porque PointPrimitive é
  // muito mais barato que billboard de entidade.
  const MAX_PLOT = 4000;

  let catalog = []; // catálogo completo vindo da API (não se move, não precisa refresh)
  let collection = null; // Cesium.PointPrimitiveCollection
  let visible = false;
  let started = false;
  let lastSource = "demo";
  const enabledFuels = new Set(Object.keys(FUEL_COLORS)); // todos ligados por padrão (5 checkboxes já vêm marcados no HTML)
  let moveEndTimer = null;

  function ensureCollection() {
    if (!collection && window.WTX.viewer) {
      collection = new Cesium.PointPrimitiveCollection();
      window.WTX.viewer.scene.primitives.add(collection);
    }
    return collection;
  }

  function getViewBbox() {
    try {
      const viewer = window.WTX.viewer;
      if (!viewer) return null;
      const rect = viewer.camera.computeViewRectangle();
      if (!rect) return null;
      const west = Cesium.Math.toDegrees(rect.west);
      const south = Cesium.Math.toDegrees(rect.south);
      const east = Cesium.Math.toDegrees(rect.east);
      const north = Cesium.Math.toDegrees(rect.north);
      if (Math.abs(east - west) > 130 || north - south > 100) return null;
      return [west, south, east, north];
    } catch (e) {
      return null;
    }
  }

  function selectSubset() {
    const filtered = enabledFuels.size
      ? catalog.filter((p) => enabledFuels.has(p.fuel))
      : [];
    if (!filtered.length) return [];

    const bbox = getViewBbox();
    let pool = filtered;
    if (bbox) {
      const [minLon, minLat, maxLon, maxLat] = bbox;
      const inView = filtered.filter(
        (p) =>
          p.lat >= minLat && p.lat <= maxLat && p.lon >= minLon && p.lon <= maxLon
      );
      pool = inView.length > 0 ? inView : filtered;
    }
    if (pool.length <= MAX_PLOT) return pool;

    // Amostra espacial uniforme (passo fixo) em vez de cortar por
    // capacidade — senão usinas pequenas somem completamente de
    // regiões inteiras assim que o zoom mostra o mundo todo.
    const step = pool.length / MAX_PLOT;
    const out = [];
    for (let i = 0; i < MAX_PLOT; i++) out.push(pool[Math.floor(i * step)]);
    return out;
  }

  function pixelSizeFor(capacityMw) {
    const c = Math.max(Number(capacityMw) || 1, 1);
    // sqrt(capacidade) pra' Itaipu/Three Gorges nao virarem do mesmo
    // tamanho que uma usina solar de bairro, com um teto pra nao
    // cobrir a tela num zoom próximo.
    return Math.min(4 + Math.sqrt(c) * 0.55, 26);
  }

  function repaint() {
    const coll = ensureCollection();
    if (!coll) return;
    coll.removeAll();
    if (!visible) return;

    const subset = selectSubset();
    subset.forEach((p) => {
      coll.add({
        position: Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 200),
        pixelSize: pixelSizeFor(p.capacity_mw),
        color: FUEL_COLORS[p.fuel] || Cesium.Color.WHITE,
        outlineColor: Cesium.Color.BLACK.withAlpha(0.65),
        outlineWidth: 1,
        disableDepthTestDistance: 0,
        // PointPrimitive não é uma Cesium.Entity: não tem
        // `.properties` de verdade. main.js só abre o painel de info
        // se `picked.id.properties` for truthy, então usamos um
        // objeto simples com `properties: true` + `wtxData` (mesmo
        // atalho que ships.js/cameras.js usam via entity.wtxData).
        id: {
          properties: true,
          wtxData: { ...p, wtxType: "power_plant", wtxSource: lastSource },
        },
      });
    });
  }

  function onCameraMoveEnd() {
    if (!started || !visible) return;
    if (moveEndTimer) clearTimeout(moveEndTimer);
    moveEndTimer = setTimeout(repaint, 400);
  }

  function updateFuelCounts(countsByFuel) {
    Object.keys(FUEL_COLORS).forEach((fuel) => {
      const el = document.getElementById(`pp-count-${fuel}`);
      if (el) {
        const n = countsByFuel[fuel] || 0;
        el.textContent = `(${n.toLocaleString("pt-BR")})`;
      }
    });
  }

  async function refresh() {
    try {
      const res = await fetch("/api/infrastructure/power-plants");
      const data = await res.json();
      lastSource = data.source;
      catalog = data.plants || [];

      window.WTX.updateBadge("powerplants", data.is_live);
      updateFuelCounts(data.counts_by_fuel || {});

      repaint();
    } catch (err) {
      console.error("Falha ao carregar usinas de energia", err);
      const badge = document.getElementById("badge-powerplants");
      if (badge) {
        badge.textContent = "FALHA";
        badge.classList.remove("live");
      }
    }
  }

  function ensureLoaded() {
    if (started) return;
    started = true;
    refresh();
    if (window.WTX.viewer) {
      window.WTX.viewer.camera.moveEnd.addEventListener(onCameraMoveEnd);
    }
  }

  window.WTX.powerPlantsLayer = {
    ensureLoaded,
    setVisible(v) {
      visible = v;
      if (v && !started) {
        ensureLoaded();
      } else {
        repaint();
      }
    },
    setFuelEnabled(fuel, on) {
      if (on) enabledFuels.add(fuel);
      else enabledFuels.delete(fuel);
      repaint();
    },
    getSource: () => lastSource,
  };
})();

// ---------------------------------------------------------------------
// Bloco 1 — Cabos submarinos de internet (INFRAFISICA.md)
// ---------------------------------------------------------------------
//
// Duas coisas na mesma resposta da API (`/api/infrastructure/cables`):
// as rotas dos cabos (MultiLineString por cabo) e os pontos de
// aterrissagem (onde o cabo sai do mar). Desenhamos os dois juntos,
// controlados pelo mesmo checkbox — não faz sentido separar visão de
// "rota" e "ponta da rota" em toggles diferentes.
//
// Ao contrário das usinas (Bloco 2), aqui não precisamos de amostragem
// por viewport: mesmo o dataset completo da TeleGeography (~600 cabos,
// ~1200 pontos) é uma fração do volume de usinas (25k+) ou câmeras, e
// CustomDataSource/PointPrimitiveCollection aguentam isso de sobra.
//
// Geometria das linhas usa o MESMO cuidado de boundaries.js (corte na
// antimeridiana + densificação linear em vez de deixar o Cesium
// interpolar geodesicamente) — ver os comentários lá para o histórico
// do bug que motivou isso. `LINE_HEIGHT = 0`: ao contrário das
// fronteiras (que sobem um pouco pra não se misturar ao terreno), aqui
// faz sentido "colar" no oceano.
(function cablesLayer() {
  const LINE_WIDTH = 1.3;
  const LINE_HEIGHT = 0;
  const MAX_STEP_DEG = 8; // cabos já vêm com pontos razoavelmente densos na fonte
  const MAX_SUBDIV = 30;
  const LANDING_POINT_COLOR = Cesium.Color.fromCssColorString("#22d3ee");

  let cablesCatalog = [];
  let landingPointsCatalog = [];
  let cableDataSource = null;
  let landingPointsCollection = null;
  let visible = false;
  let started = false;
  let lastSource = "demo";

  // ---------- geometria (mesma receita de boundaries.js) ----------

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

  function pushDensified(flat, a, b) {
    const dLon = b[0] - a[0];
    const dLat = b[1] - a[1];
    let steps = Math.ceil(Math.max(Math.abs(dLon), Math.abs(dLat)) / MAX_STEP_DEG);
    if (!Number.isFinite(steps) || steps < 1) steps = 1;
    if (steps > MAX_SUBDIV) steps = MAX_SUBDIV;
    for (let i = 1; i <= steps; i++) {
      flat.push(a[0] + (dLon * i) / steps, a[1] + (dLat * i) / steps, LINE_HEIGHT);
    }
  }

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
    return segments.filter((f) => f.length >= 6);
  }

  // ---------- coleções ----------

  function ensureCableDataSource() {
    if (!cableDataSource && window.WTX.viewer) {
      cableDataSource = new Cesium.CustomDataSource("submarine-cables");
      window.WTX.viewer.dataSources.add(cableDataSource);
    }
    return cableDataSource;
  }

  function ensureLandingPointsCollection() {
    if (!landingPointsCollection && window.WTX.viewer) {
      landingPointsCollection = new Cesium.PointPrimitiveCollection();
      window.WTX.viewer.scene.primitives.add(landingPointsCollection);
    }
    return landingPointsCollection;
  }

  function repaintCables() {
    const ds = ensureCableDataSource();
    if (!ds) return;
    ds.entities.removeAll();
    if (!visible) return;

    cablesCatalog.forEach((cable) => {
      const lines = cable.coordinates;
      if (!Array.isArray(lines)) return;
      const color = Cesium.Color.fromCssColorString(cable.color || "#939597").withAlpha(0.85);
      const payload = {
        id: cable.id,
        name: cable.name,
        wtxType: "submarine_cable",
        wtxSource: lastSource,
      };

      lines.forEach((line) => {
        if (!Array.isArray(line) || line.length < 2) return;
        toFlatSegments(line).forEach((flat) => {
          let positions;
          try {
            positions = Cesium.Cartesian3.fromDegreesArrayHeights(flat);
          } catch (err) {
            return; // segmento problemático descartado, não derruba o resto do cabo
          }
          if (!positions || positions.length < 2) return;

          const entity = ds.entities.add({
            polyline: {
              positions: positions,
              width: LINE_WIDTH,
              material: color,
              arcType: Cesium.ArcType.NONE,
            },
          });
          // Mesmo atalho de ships.js: PropertyBag de verdade (pro check
          // `picked.id.properties` em main.js) + wtxData como caminho
          // rápido de leitura em showInfoForEntity/unwrapEntityProps.
          entity.properties = payload;
          entity.wtxData = payload;
        });
      });
    });
  }

  function repaintLandingPoints() {
    const coll = ensureLandingPointsCollection();
    if (!coll) return;
    coll.removeAll();
    if (!visible) return;

    landingPointsCatalog.forEach((p) => {
      if (!Number.isFinite(p.lon) || !Number.isFinite(p.lat)) return;
      coll.add({
        position: Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 0),
        pixelSize: 5,
        color: LANDING_POINT_COLOR,
        outlineColor: Cesium.Color.BLACK.withAlpha(0.7),
        outlineWidth: 1,
        disableDepthTestDistance: 0,
        id: {
          properties: true,
          wtxData: { ...p, wtxType: "cable_landing_point", wtxSource: lastSource },
        },
      });
    });
  }

  function repaint() {
    repaintCables();
    repaintLandingPoints();
  }

  function updateCount(count) {
    const el = document.getElementById("count-cables");
    if (el) el.textContent = `(${(count || 0).toLocaleString("pt-BR")})`;
  }

  async function refresh() {
    try {
      const res = await fetch("/api/infrastructure/cables");
      const data = await res.json();
      lastSource = data.source;
      cablesCatalog = data.cables || [];
      landingPointsCatalog = data.landing_points || [];

      window.WTX.updateBadge("cables", data.is_live);
      updateCount(data.count);

      repaint();
    } catch (err) {
      console.error("Falha ao carregar cabos submarinos", err);
      const badge = document.getElementById("badge-cables");
      if (badge) {
        badge.textContent = "FALHA";
        badge.classList.remove("live");
      }
    }
  }

  function ensureLoaded() {
    if (started) return;
    started = true;
    refresh();
  }

  window.WTX.cablesLayer = {
    ensureLoaded,
    setVisible(v) {
      visible = v;
      if (v && !started) {
        ensureLoaded();
      } else {
        repaint();
      }
    },
    getSource: () => lastSource,
  };
})();

// ---------------------------------------------------------------------
// Bloco 3 — Datacenters (INFRAFISICA.md)
// ---------------------------------------------------------------------
//
// Fonte ao vivo: OpenStreetMap via Overpass API (`telecom=data_center`
// + `building=data_center`, bbox global). Volume mundial no OSM é bem
// menor que o de usinas (milhares, não decenas de milhares), mas ainda
// assim usamos a mesma amostragem por viewport de ships.js/cameras.js
// (via PointPrimitiveCollection, igual usinas) porque a cobertura do
// OSM é MUITO concentrada em algumas regiões (EUA/Europa) — sem
// amostragem, um zoom nessas áreas ainda tentaria desenhar centenas de
// pontos empilhados de uma vez.
//
// Marcador: ponto colorido simples (roxo), igual ao resto da camada de
// infraestrutura — em vez de um ícone de "prédio/servidor" desenhado à
// mão, pra manter consistência visual com usinas/cabos em vez de
// introduzir um estilo novo só para esta camada.
(function datacentersLayer() {
  const MAX_PLOT = 1500;
  const DC_COLOR = Cesium.Color.fromCssColorString("#a78bfa");
  const DC_COLOR_APPROX = Cesium.Color.fromCssColorString("#a78bfa").withAlpha(0.55);

  let catalog = [];
  let collection = null;
  let visible = false;
  let started = false;
  let lastSource = "demo_curated";
  let moveEndTimer = null;

  function ensureCollection() {
    if (!collection && window.WTX.viewer) {
      collection = new Cesium.PointPrimitiveCollection();
      window.WTX.viewer.scene.primitives.add(collection);
    }
    return collection;
  }

  // Mesma função (bbox + teto) usada em powerPlantsLayer acima —
  // duplicada em vez de compartilhada porque cada camada tem seu
  // próprio catálogo/estado e o corpo é pequeno; não compensa extrair
  // um módulo comum só para isso ainda.
  function getViewBbox() {
    try {
      const viewer = window.WTX.viewer;
      if (!viewer) return null;
      const rect = viewer.camera.computeViewRectangle();
      if (!rect) return null;
      const west = Cesium.Math.toDegrees(rect.west);
      const south = Cesium.Math.toDegrees(rect.south);
      const east = Cesium.Math.toDegrees(rect.east);
      const north = Cesium.Math.toDegrees(rect.north);
      if (Math.abs(east - west) > 130 || north - south > 100) return null;
      return [west, south, east, north];
    } catch (e) {
      return null;
    }
  }

  function selectSubset() {
    if (!catalog.length) return [];
    const bbox = getViewBbox();
    let pool = catalog;
    if (bbox) {
      const [minLon, minLat, maxLon, maxLat] = bbox;
      const inView = catalog.filter(
        (p) => p.lat >= minLat && p.lat <= maxLat && p.lon >= minLon && p.lon <= maxLon
      );
      pool = inView.length > 0 ? inView : catalog;
    }
    if (pool.length <= MAX_PLOT) return pool;
    const step = pool.length / MAX_PLOT;
    const out = [];
    for (let i = 0; i < MAX_PLOT; i++) out.push(pool[Math.floor(i * step)]);
    return out;
  }

  function repaint() {
    const coll = ensureCollection();
    if (!coll) return;
    coll.removeAll();
    if (!visible) return;

    selectSubset().forEach((p) => {
      if (!Number.isFinite(p.lon) || !Number.isFinite(p.lat)) return;
      coll.add({
        position: Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 100),
        pixelSize: 7,
        // Lista curada (fallback) marca approx=true nos pontos: usamos
        // um alfa menor pra diferenciar visualmente "localização real
        // do OSM" de "aproximação de cidade/região" sem precisar de
        // outro checkbox só pra isso.
        color: p.approx ? DC_COLOR_APPROX : DC_COLOR,
        outlineColor: Cesium.Color.BLACK.withAlpha(0.7),
        outlineWidth: 1,
        disableDepthTestDistance: 0,
        id: {
          properties: true,
          wtxData: { ...p, wtxType: "datacenter", wtxSource: lastSource },
        },
      });
    });
  }

  function onCameraMoveEnd() {
    if (!started || !visible) return;
    if (moveEndTimer) clearTimeout(moveEndTimer);
    moveEndTimer = setTimeout(repaint, 400);
  }

  function updateCount(count) {
    const el = document.getElementById("count-datacenters");
    if (el) el.textContent = `(${(count || 0).toLocaleString("pt-BR")})`;
  }

  async function refresh() {
    try {
      const res = await fetch("/api/infrastructure/datacenters");
      const data = await res.json();
      lastSource = data.source;
      catalog = data.datacenters || [];

      window.WTX.updateBadge("datacenters", data.is_live);
      updateCount(data.count);

      repaint();
    } catch (err) {
      console.error("Falha ao carregar datacenters", err);
      const badge = document.getElementById("badge-datacenters");
      if (badge) {
        badge.textContent = "FALHA";
        badge.classList.remove("live");
      }
    }
  }

  function ensureLoaded() {
    if (started) return;
    started = true;
    refresh();
    if (window.WTX.viewer) {
      window.WTX.viewer.camera.moveEnd.addEventListener(onCameraMoveEnd);
    }
  }

  window.WTX.datacentersLayer = {
    ensureLoaded,
    setVisible(v) {
      visible = v;
      if (v && !started) {
        ensureLoaded();
      } else {
        repaint();
      }
    },
    getSource: () => lastSource,
  };
})();
