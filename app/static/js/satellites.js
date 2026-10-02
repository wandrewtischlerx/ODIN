/*
 * satellites.js
 * Camada de satelites.
 *
 * OTIMIZACAO (2026-09): antes esta camada chamava /api/satellites/ a cada
 * 5 segundos so pra mover os pontos no globo. Isso e desperdicio: a
 * posicao de um satelite e 100% previsivel a partir do seu TLE (elementos
 * orbitais), que so muda de fato a cada poucas horas quando o SatNOGS/
 * CelesTrak publica um TLE novo. Nao precisamos perguntar de novo pro
 * servidor onde o satelite esta - da pra CALCULAR isso localmente com o
 * mesmo modelo SGP4 usado no backend (biblioteca satellite.js), sem
 * nenhuma chamada de rede extra.
 *
 * Fluxo novo:
 *   1) Busca /api/satellites/tle UMA VEZ (e depois so de novo a cada 6h,
 *      mesmo TTL do cache do backend) - devolve nome + linhas de TLE.
 *   2) satellite.twoline2satrec() converte cada TLE num "satrec" local.
 *   3) A cada tick (SGP4_TICK_MS), propaga TODOS os satrecs pro instante
 *      atual (satellite.propagate + gstime + eciToGeodetic) e atualiza as
 *      entidades do Cesium - tudo isso e so matematica no navegador,
 *      sem fetch.
 *   4) Fallback: se o servidor nao tiver TLEs reais (fontes fora do ar),
 *      cai pro modo demo antigo (poucas dezenas de pontos sinteticos),
 *      que continua vindo de /api/satellites/ periodicamente.
 */

window.WTX = window.WTX || {};

(function satelliteLayer() {
  const entities = new Map();
  const orbitLines = new Map();
  const satrecs = new Map(); // id -> { satrec, meta }
  let visible = false;
  let started = false;
  let orbitsVisible = false;
  let orbitOnlyId = null; // se definido, mostra só a órbita deste satélite
  let lastSource = "demo";

  let tleLoaded = false;
  let tleRefreshTimer = null;

  // Modo demo (fallback, sem TLE real disponivel)
  let demoRefreshTimer = null;
  let usingDemo = false;

  const TLE_REFRESH_MS = 6 * 60 * 60 * 1000; // mesmo TTL do cache no backend
  // OTIMIZACAO 2 (suavidade): antes recalculava a cada 2s via setInterval,
  // o que fazia o ponto "saltar" de posicao em posicao (dava pra perceber
  // o degrau visualmente). Como o SGP4 e barato (so matematica, sem rede),
  // trocamos pra um loop de requestAnimationFrame que recalcula a posicao
  // REAL (nao uma interpolacao/estimativa) varias vezes por segundo -
  // continua sendo a rota exata calculada a partir do TLE, so que com
  // passo bem menor, entao o movimento fica continuo em vez de saltado.
  //
  // TICK ADAPTATIVO (2026-09): o teto de satelites subiu bastante
  // (MAX_SATELLITES no backend), entao o tick fixo de 120ms podia
  // sobrecarregar o navegador com dezenas de milhares de satrecs. Em vez
  // de um numero fixo, a frequencia se ajusta pela quantidade real de
  // satelites recebida - poucos satelites = atualizacao bem mais suave
  // (quase continua); muitos satelites = ainda suave, so que com um
  // passo um pouco maior pra nao travar a aba.
  function adaptiveTickMs(count) {
    if (count <= 2500) return 120; // ~8x/s
    if (count <= 5000) return 250; // ~4x/s
    return 500; // ~2x/s - ainda bem mais suave que o antigo fixo de 2s
  }
  let lastTickAt = 0;
  let rafHandle = null;

  // Satelites civis em branco/prata; militares sempre em vermelho;
  // demo em tom neutro mais escuro.
  function iconSvg(fill, bar) {
    return (
      "data:image/svg+xml;charset=UTF-8," +
      encodeURIComponent(
        `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">
          <circle cx="10" cy="10" r="3" fill="${fill}" />
          <rect x="1" y="9" width="6" height="2" fill="${bar}" />
          <rect x="13" y="9" width="6" height="2" fill="${bar}" />
        </svg>`
      )
    );
  }
  const ICON_LIVE = iconSvg("#f2f2ee", "#c7c9c1");
  const ICON_MILITARY = iconSvg("#d9534f", "#a94442"); // vermelho fixo para militar
  const ICON_DEMO = iconSvg("#9a9c94", "#71736c");

  function altitudeToHeight(altKm) {
    // escala visual: exagera altitude para tornar a orbita legivel
    return Math.min(altKm * 1000 * 3, 4000000) + 300000;
  }

  /** Órbita aproximada (fallback demo / sem satrec). */
  function orbitPositionsApprox(sat) {
    const positions = [];
    const height = altitudeToHeight(sat.altitude_km);
    const steps = 90;
    for (let i = 0; i <= steps; i++) {
      const lon = sat.lon - 180 + (360 * i) / steps;
      const lat = sat.lat * Math.cos((i / steps) * Math.PI - Math.PI / 2);
      positions.push(Cesium.Cartesian3.fromDegrees(lon, lat, height));
    }
    return positions;
  }

  /**
   * Órbita real a partir do TLE (SGP4): propaga ~1 período orbital
   * (ou 90 min se desconhecido) em passos uniformes.
   */
  function orbitPositionsFromSatrec(satrec, meta) {
    const positions = [];
    const now = new Date();
    // Período aproximado a partir do mean motion (rev/dia) no TLE.
    // satrec.no é rad/min; 2π / no = minutos por revolução.
    let periodMin = 90;
    try {
      if (satrec.no && satrec.no > 0) {
        periodMin = (2 * Math.PI) / satrec.no;
      }
    } catch (_) {}
    const steps = 120;
    const stepMs = (periodMin * 60 * 1000) / steps;
    for (let i = 0; i <= steps; i++) {
      const t = new Date(now.getTime() + i * stepMs);
      const pv = satellite.propagate(satrec, t);
      if (!pv || !pv.position || satrec.error) continue;
      const gmst = satellite.gstime(t);
      const geo = satellite.eciToGeodetic(pv.position, gmst);
      const lat = satellite.degreesLat(geo.latitude);
      const lon = satellite.degreesLong(geo.longitude);
      const altKm = geo.height;
      if (!isFinite(altKm) || altKm < 0 || altKm > 45000) continue;
      positions.push(
        Cesium.Cartesian3.fromDegrees(lon, lat, altitudeToHeight(altKm))
      );
    }
    return positions.length >= 2 ? positions : null;
  }

  function orbitPositions(sat) {
    const entry = satrecs.get(sat.id);
    if (entry && entry.satrec) {
      const real = orbitPositionsFromSatrec(entry.satrec, entry.meta);
      if (real) return real;
    }
    return orbitPositionsApprox(sat);
  }

  function orbitShouldShow(id) {
    if (!visible) return false;
    if (orbitOnlyId != null) return orbitOnlyId === id;
    return orbitsVisible;
  }

  function upsertSatellite(sat, source) {
    const height = altitudeToHeight(sat.altitude_km);
    const position = Cesium.Cartesian3.fromDegrees(sat.lon, sat.lat, height);
    let icon = ICON_DEMO;
    if (source !== "demo") {
      icon = sat.is_military ? ICON_MILITARY : ICON_LIVE;
    }

    let entity = entities.get(sat.id);
    if (!entity) {
      entity = window.WTX.viewer.entities.add({
        id: `satellite-${sat.id}`,
        position,
        billboard: {
          image: icon,
          scale: 0.8,
          disableDepthTestDistance: 0, // respeita a oclusao do globo (nao mostra do outro lado)
        },
      });
      entities.set(sat.id, entity);
    } else {
      entity.position = position;
      entity.billboard.image = icon;
    }
    const payload = { ...sat, wtxType: "satellite", wtxSource: source };
    entity.properties = payload;
    entity.wtxData = payload;
    entity.show = visible; // militares sempre visíveis (sem filtro)

    let orbit = orbitLines.get(sat.id);
    const showThisOrbit = orbitShouldShow(sat.id);
    if (!orbit) {
      const orbitColor = sat.is_military
        ? Cesium.Color.fromCssColorString("#d9534f").withAlpha(0.35)
        : Cesium.Color.fromCssColorString("#c7c9c1").withAlpha(0.25);
      orbit = window.WTX.viewer.entities.add({
        id: `orbit-${sat.id}`,
        polyline: {
          positions: showThisOrbit ? orbitPositions(sat) : [],
          width: 2,
          material: orbitColor,
        },
      });
      orbitLines.set(sat.id, orbit);
    } else if (showThisOrbit) {
      // Atualiza a curva quando a órbita está visível (modo único ou global).
      orbit.polyline.positions = orbitPositions(sat);
    }
    orbit.properties = payload;
    orbit.wtxData = payload;
    orbit.show = showThisOrbit;
  }

  // ---------- Modo real: TLE + SGP4 local ----------

  function tickRealPositions() {
    if (!satrecs.size) return;
    const now = new Date();
    const gmst = satellite.gstime(now);

    satrecs.forEach(({ satrec, meta }, id) => {
      const pv = satellite.propagate(satrec, now);
      // BUGFIX: satelites decaidos/reentrados (ex.: ASTROCAST-0201) ainda
      // aparecem no catalogo de TLE do SatNOGS mesmo depois de reentrar -
      // o SGP4 sinaliza isso via satrec.error != 0 quando o modelo diverge
      // ao propagar um TLE muito antigo/pos-decaimento pro instante atual
      // (o sintoma classico e uma altitude absurda, tipo milhoes de km).
      // O backend ja filtrava isso (error_code != 0) na propagacao server-
      // side, mas o calculo local no navegador nao replicava essa checagem -
      // por isso o satelite "fantasma" aparecia so na visualizacao ao vivo.
      // Uma vez invalido, remove de vez (nao volta a ficar valido).
      if (!pv || !pv.position || satrec.error) {
        const staleEntity = entities.get(id);
        if (staleEntity) {
          window.WTX.viewer.entities.remove(staleEntity);
          entities.delete(id);
        }
        const staleOrbit = orbitLines.get(id);
        if (staleOrbit) {
          window.WTX.viewer.entities.remove(staleOrbit);
          orbitLines.delete(id);
        }
        satrecs.delete(id);
        return;
      }
      const geo = satellite.eciToGeodetic(pv.position, gmst);
      const lat = satellite.degreesLat(geo.latitude);
      const lon = satellite.degreesLong(geo.longitude);
      const altitude_km = geo.height;

      // Cinto e suspensorio: mesmo com error===0, descarta leituras
      // fisicamente impossiveis (nenhum satelite catalogado passa de
      // ~45 mil km de altitude - GEO fica em ~35.786 km).
      if (!isFinite(altitude_km) || altitude_km < 0 || altitude_km > 45000) {
        return;
      }

      let velocity_kms = meta.velocity_kms;
      if (pv.velocity) {
        velocity_kms = Math.sqrt(
          pv.velocity.x ** 2 + pv.velocity.y ** 2 + pv.velocity.z ** 2
        );
      }

      upsertSatellite(
        {
          id,
          norad_id: meta.norad_id,
          name: meta.name,
          orbit_class: meta.orbit_class,
          altitude_km: Math.round(Math.max(altitude_km, 0)),
          velocity_kms: Math.round((velocity_kms || 0) * 100) / 100,
          inclination_deg: meta.inclination_deg,
          lat: Math.round(lat * 10000) / 10000,
          lon: Math.round(lon * 10000) / 10000,
          status: "TRACKING",
          is_military: meta.is_military,
        },
        lastSource
      );
    });

    window.WTX.updateBadge("satellites", lastSource !== "demo");
    window.WTX.updateCounter("satellites", satrecs.size);
  }

  function rafLoop(timestamp) {
    if (!started || usingDemo) {
      rafHandle = null;
      return;
    }
    if (timestamp - lastTickAt >= adaptiveTickMs(satrecs.size)) {
      lastTickAt = timestamp;
      tickRealPositions();
    }
    rafHandle = requestAnimationFrame(rafLoop);
  }

  function startRafLoop() {
    if (rafHandle) return;
    lastTickAt = 0;
    rafHandle = requestAnimationFrame(rafLoop);
  }

  function stopRafLoop() {
    if (rafHandle) {
      cancelAnimationFrame(rafHandle);
      rafHandle = null;
    }
  }

  async function loadTle() {
    try {
      const res = await fetch("/api/satellites/tle");
      const data = await res.json();

      if (!data.satellites || !data.satellites.length) {
        // Sem TLE real disponivel agora - cai pro fallback demo.
        tleLoaded = false;
        startDemoFallback();
        return;
      }

      usingDemo = false;
      stopDemoFallback();
      lastSource = data.source;
      catalogTotal =
        typeof data.catalog_total === "number" ? data.catalog_total : data.count;
      satrecs.clear();

      data.satellites.forEach((sat) => {
        try {
          const rec = satellite.twoline2satrec(sat.line1, sat.line2);
          satrecs.set(sat.id, {
            satrec: rec,
            meta: {
              norad_id: sat.norad_id,
              name: sat.name,
              orbit_class: sat.orbit_class,
              inclination_deg: sat.inclination_deg,
              velocity_kms: null,
              is_military: !!sat.is_military,
            },
          });
        } catch (err) {
          // TLE individual invalido - ignora so esse satelite
        }
      });

      tleLoaded = true;
      tickRealPositions(); // primeira posicao imediata, sem esperar o proximo tick
      startRafLoop();
    } catch (err) {
      console.error("Falha ao buscar TLE de satelites", err);
      if (!tleLoaded) startDemoFallback();
    }
  }

  // ---------- Fallback demo (sem TLE real) ----------

  async function refreshDemo() {
    try {
      const res = await fetch("/api/satellites/");
      const data = await res.json();
      lastSource = data.source;

      if (data.source !== "demo") {
        // Fontes reais voltaram - troca pro modo TLE real.
        stopDemoFallback();
        loadTle();
        return;
      }

      usingDemo = true;
      data.satellites.forEach((sat) => upsertSatellite(sat, data.source));
      window.WTX.updateBadge("satellites", false);
      window.WTX.updateCounter("satellites", data.count);
    } catch (err) {
      console.error("Falha ao atualizar satelites (demo)", err);
    }
  }

  function startDemoFallback() {
    if (demoRefreshTimer) return;
    usingDemo = true;
    refreshDemo();
    demoRefreshTimer = setInterval(refreshDemo, 5000);
  }

  function stopDemoFallback() {
    if (demoRefreshTimer) {
      clearInterval(demoRefreshTimer);
      demoRefreshTimer = null;
    }
  }

  // Usado pelo buscador (main.js): satelites ja carregados cujo nome
  // bate com o texto digitado.
  function searchSatellites(query) {
    const q = String(query || "").trim().toLowerCase();
    if (!q) return [];
    const out = [];
    entities.forEach((entity) => {
      const d = entity.wtxData || {};
      const name = String(d.name || "").toLowerCase();
      if (name.includes(q)) {
        out.push({ id: d.id, label: d.name, lon: d.lon, lat: d.lat, entity });
      }
    });
    return out;
  }

  // Carrega TLE em segundo plano (sem mostrar nada no globo) pra' o
  // buscador achar satelite mesmo com a camada desligada.
  function ensureLoaded() {
    if (started) return;
    started = true;
    loadTle();
    if (!tleRefreshTimer) {
      tleRefreshTimer = setInterval(loadTle, TLE_REFRESH_MS);
    }
  }

  window.WTX.satelliteLayer = {
    search: searchSatellites,
    ensureLoaded,
    setVisible(v) {
      visible = v;
      entities.forEach((e) => (e.show = v));
      orbitLines.forEach((o, id) => (o.show = orbitShouldShow(id)));
      if (v && !started) {
        started = true;
        loadTle();
        if (!tleRefreshTimer) {
          tleRefreshTimer = setInterval(loadTle, TLE_REFRESH_MS);
        }
      } else if (!v) {
        // Mantem os satrecs/entidades vivos (nao reprocessa TLE),
        // so pausa o calculo/timers enquanto a camada estiver escondida -
        // volta a computar do zero quando o usuario reativar a camada.
        stopRafLoop();
        stopDemoFallback();
        started = false;
      }
    },
    setOrbitsVisible(v) {
      orbitsVisible = v;
      // Ligar "órbitas (todas)" limpa o modo órbita-única.
      if (v) orbitOnlyId = null;
      orbitLines.forEach((o, id) => (o.show = orbitShouldShow(id)));
    },
    /** Alterna a órbita de um único satélite. Retorna true se agora está só nele. */
    toggleOrbitOnly(id) {
      if (orbitOnlyId === id) {
        orbitOnlyId = null;
      } else {
        orbitOnlyId = id;
        orbitsVisible = false; // modo "só este" desliga o global
        const cb = document.getElementById("layer-orbits");
        if (cb) cb.checked = false;
      }
      orbitLines.forEach((o, oid) => (o.show = orbitShouldShow(oid)));
      return orbitOnlyId === id;
    },
    isOrbitOnly(id) {
      return orbitOnlyId === id;
    },
    getSource: () => lastSource,
    getCatalogTotal: () => catalogTotal,
  };
})();
