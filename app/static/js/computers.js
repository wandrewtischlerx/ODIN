/*
 * computers.js
 * Camada de computadores/rede (estilo Shodan) — varredura ativa de
 * portas + banner passivo + geoIP contra uma lista curada de hosts
 * públicos conhecidos (ver TARGET_HOSTS em computer_service.py).
 *
 * Diferença de arquitetura em relação a cameras.js/ships.js: o
 * catálogo é pequeno (dezenas de hosts, não milhares), então não
 * precisa de amostragem por viewport — só entidades simples, como em
 * infrastructure.js para cabos.
 */

window.WTX = window.WTX || {};

(function computerLayer() {
  const entities = new Map();
  let visible = false;
  let started = false;
  let lastSource = "demo";
  let catalog = [];
  let rescanning = false;

  const RISK_COLOR = {
    baixo: "#38bdf8",
    medio: "#fbbf24",
    alto: "#fb923c",
    critico: "#f87171",
  };

  function riskOf(c) {
    return RISK_COLOR[c.risk_level] ? c.risk_level : "baixo";
  }

  function computerIcon(risk) {
    const color = RISK_COLOR[risk] || RISK_COLOR.baixo;
    return (
      "data:image/svg+xml;charset=UTF-8," +
      encodeURIComponent(
        `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">
          <rect x="2" y="3" width="14" height="9" rx="1" fill="none" stroke="${color}" stroke-width="1.6"/>
          <rect x="6" y="14" width="6" height="1.6" fill="${color}"/>
          <rect x="8" y="12" width="2" height="2.4" fill="${color}"/>
        </svg>`
      )
    );
  }

  const _iconCache = {};
  function iconFor(risk) {
    if (!_iconCache[risk]) _iconCache[risk] = computerIcon(risk);
    return _iconCache[risk];
  }

  function renderCounterLabel() {
    window.WTX.updateCounter("computers", catalog.length);
  }

  function upsertComputer(c) {
    const risk = riskOf(c);
    let entity = entities.get(c.id);
    if (entity && !window.WTX.viewer.entities.contains(entity)) {
      entities.delete(c.id);
      entity = null;
    }
    const wtxData = { ...c, wtxType: "computer", wtxSource: lastSource };
    if (!entity) {
      entity = window.WTX.viewer.entities.add({
        id: `computer-${c.id}`,
        position: Cesium.Cartesian3.fromDegrees(c.lon, c.lat, 0),
        billboard: {
          image: iconFor(risk),
          scale: 1.0,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: 0,
        },
      });
      entities.set(c.id, entity);
    } else {
      entity.billboard.image = iconFor(risk);
      entity.position = Cesium.Cartesian3.fromDegrees(c.lon, c.lat, 0);
    }
    entity.wtxData = wtxData;
  }

  function repaint() {
    if (!window.WTX.viewer) return;
    const seen = new Set();
    catalog.forEach((c) => {
      if (c.lat == null || c.lon == null) return;
      upsertComputer(c);
      seen.add(c.id);
    });
    entities.forEach((entity, id) => {
      if (!seen.has(id)) {
        window.WTX.viewer.entities.remove(entity);
        entities.delete(id);
      }
    });
    renderCounterLabel();
  }

  async function fetchComputers(forceRescan) {
    try {
      if (window.WTX.setLayerBusy) window.WTX.setLayerBusy("computers", true);
      const url = forceRescan ? "/api/computers/rescan" : "/api/computers/";
      const res = await fetch(url, { method: forceRescan ? "POST" : "GET" });
      const data = await res.json();
      catalog = data.computers || [];
      lastSource = data.source || "demo";
      if (window.WTX.updateBadge) window.WTX.updateBadge("computers", data.is_live);
      if (visible) repaint();
      else renderCounterLabel();
    } catch (err) {
      console.error("Falha ao carregar camada de computadores", err);
    } finally {
      rescanning = false;
      if (window.WTX.setLayerBusy) window.WTX.setLayerBusy("computers", false);
    }
  }

  function ensureLoaded() {
    if (started) return;
    started = true;
    fetchComputers(false);
  }

  function setVisible(v) {
    visible = v;
    ensureLoaded();
    if (v) repaint();
    else {
      entities.forEach((entity) => window.WTX.viewer.entities.remove(entity));
      entities.clear();
    }
  }

  function rescan() {
    if (rescanning) return;
    rescanning = true;
    fetchComputers(true);
  }

  window.WTX.computerLayer = { setVisible, ensureLoaded, rescan };
})();
