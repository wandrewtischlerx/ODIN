/*
 * ships.js
 * Camada de trafego aquático (AIS).
 * - CONTADOR: total global da API desde o boot / refresh.
 * - PLOTAGEM: só viewport (ou amostra global), para não travar o Cesium.
 */

window.WTX = window.WTX || {};

(function shipLayer() {
  const entities = new Map();
  let visible = false;
  let refreshTimer = null;
  let started = false;
  let lastSource = "demo";
  let catalog = [];
  let moveEndTimer = null;
  const MAX_PLOT = 1800;

  const ICON_LIVE = (
    "data:image/svg+xml;charset=UTF-8," +
    encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">
        <path d="M3 12 L9 4 L15 12 L12 12 L12 14 L6 14 L6 12 Z" fill="#4fc3f7" stroke="#0277bd" stroke-width="0.6"/>
      </svg>`
    )
  );

  const ICON_DEMO = (
    "data:image/svg+xml;charset=UTF-8," +
    encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">
        <path d="M3 12 L9 4 L15 12 L12 12 L12 14 L6 14 L6 12 Z" fill="#b0752c" stroke="#6d4c1b" stroke-width="0.6"/>
      </svg>`
    )
  );

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
      if (Math.abs(east - west) > 120 || north - south > 90) return null;
      return [west, south, east, north];
    } catch (e) {
      return null;
    }
  }

  function selectForViewport(list) {
    if (!list || !list.length) return [];
    const bbox = getViewBbox();
    let pool = list;
    if (bbox) {
      const [minLon, minLat, maxLon, maxLat] = bbox;
      const inView = list.filter(
        (s) =>
          s.lat >= minLat &&
          s.lat <= maxLat &&
          s.lon >= minLon &&
          s.lon <= maxLon
      );
      // Antes: `inView.length >= 30 ? inView : list`. Com menos de 30
      // navios na tela (praticamente sempre, fora da Europa) ele
      // descartava o resultado do viewport e voltava pra lista global -
      // que depois era cortada por VELOCIDADE. Como os navios de oceano
      // aberto (VOS/NOAA) nao tem velocidade, eles ficavam sempre no
      // fim da fila e nunca eram plotados. Agora o que esta' na tela
      // sempre ganha, mesmo que seja 1 navio.
      pool = inView.length > 0 ? inView : list;
    }
    if (pool.length <= MAX_PLOT) return pool;
    // Amostra espacialmente uniforme (passo fixo) em vez de ordenar por
    // velocidade: ordenar por velocidade concentra tudo nas regioes com
    // AIS costeiro e apaga o oceano.
    const step = pool.length / MAX_PLOT;
    const out = [];
    for (let i = 0; i < MAX_PLOT; i++) out.push(pool[Math.floor(i * step)]);
    return out;
  }

  function upsertShip(ship, source) {
    const icon = source !== "demo" ? ICON_LIVE : ICON_DEMO;
    const alt = 50;
    const position = Cesium.Cartesian3.fromDegrees(ship.lon, ship.lat, alt);
    let entity = entities.get(ship.id);
    if (!entity) {
      entity = window.WTX.viewer.entities.add({
        id: `ship-${ship.id}`,
        position: position,
        billboard: {
          image: icon,
          scale: 0.85,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: 0,
          rotation: Cesium.Math.toRadians(0),
          alignedAxis: Cesium.Cartesian3.UNIT_Z,
        },
      });
      entities.set(ship.id, entity);
    } else {
      entity.position = position;
      entity.billboard.image = icon;
    }
    const course = ship.course || ship.heading || 0;
    entity.billboard.rotation = Cesium.Math.toRadians(-course);
    const payload = { ...ship, wtxType: "ship", wtxSource: source };
    entity.properties = payload;
    entity.wtxData = payload;
    entity.show = visible;
  }

  function pruneMissing(currentIds) {
    for (const [id, entity] of entities) {
      if (!currentIds.has(id)) {
        window.WTX.viewer.entities.remove(entity);
        entities.delete(id);
      }
    }
  }

  function applyCatalog(source) {
    const subset = selectForViewport(catalog);
    const ids = new Set();
    subset.forEach((s) => {
      ids.add(s.id);
      upsertShip(s, source || lastSource);
    });
    pruneMissing(ids);
  }

  function onCameraMoveEnd() {
    if (!started || !visible) return;
    if (moveEndTimer) clearTimeout(moveEndTimer);
    moveEndTimer = setTimeout(() => applyCatalog(lastSource), 400);
  }

  async function refresh() {
    try {
      const res = await fetch("/api/ships/");
      const data = await res.json();
      lastSource = data.source;
      catalog = data.ships || [];

      window.WTX.updateBadge("ships", data.is_live);
      const total =
        typeof data.total === "number" ? data.total : data.count;
      window.WTX.updateCounter("ships", total);

      applyCatalog(data.source);
    } catch (err) {
      console.error("Falha ao atualizar navios", err);
    }
  }

  // Usado pelo buscador (main.js): devolve navios ja carregados cujo
  // nome/MMSI/callsign bate com o texto digitado.
  function searchShips(query) {
    const q = String(query || "").trim().toLowerCase();
    if (!q) return [];
    const out = [];
    entities.forEach((entity) => {
      const d = entity.wtxData || {};
      const name = String(d.name || "").toLowerCase();
      const mmsi = String(d.mmsi || "").toLowerCase();
      const callsign = String(d.callsign || "").toLowerCase();
      if (name.includes(q) || mmsi.includes(q) || callsign.includes(q)) {
        out.push({ id: d.id || d.mmsi, label: d.name || `MMSI ${d.mmsi}`, lon: d.lon, lat: d.lat, entity });
      }
    });
    return out;
  }

  // Carrega navios em segundo plano (sem mostrar nada no globo) pra' o
  // buscador achar navio mesmo com a camada desligada.
  function ensureLoaded() {
    if (started) return;
    started = true;
    refresh();
    refreshTimer = setInterval(refresh, 45000);
    if (window.WTX.viewer) {
      window.WTX.viewer.camera.moveEnd.addEventListener(onCameraMoveEnd);
    }
  }

  window.WTX.shipLayer = {
    search: searchShips,
    ensureLoaded,
    setVisible(v) {
      visible = v;
      entities.forEach((e) => (e.show = v));
      if (v && !started) {
        started = true;
        refresh();
        refreshTimer = setInterval(refresh, 45000);
        if (window.WTX.viewer) {
          window.WTX.viewer.camera.moveEnd.addEventListener(onCameraMoveEnd);
        }
      } else if (!v && refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
        started = false;
      }
    },
    getSource: () => lastSource,
  };
})();
