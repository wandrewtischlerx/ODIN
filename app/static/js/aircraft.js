/*
 * aircraft.js
 * Camada de aeronaves. Consome /api/aircraft/, que retorna dados reais
 * (adsb.lol + adsb.fi + airplanes.live + OpenSky Network, mesclados)
 * quando disponivel, ou dados simulados claramente identificados via
 * campo "source".
 *
 * SUAVIZACAO (2026-09): antes a posicao da entidade so mudava quando um
 * novo /api/aircraft/ chegava (a cada 60s) - ou seja, o aviao ficava
 * parado por 1 minuto inteiro e depois "teleportava" pro proximo ponto.
 * Agora, entre uma resposta e outra, a posicao e EXTRAPOLADA localmente
 * a cada frame usando o rumo (heading) e a velocidade que o proprio
 * ADS-B/OpenSky ja manda (dead reckoning / calculo de rota real, nao
 * um efeito visual falso) - o aviao anda de fato ao longo da rota que
 * ele estava seguindo. Quando o proximo dado real chega, em vez de
 * saltar direto pro ponto novo, a posicao é corrigida suavemente (lerp
 * curto) - absorve a diferenca entre a extrapolacao e a leitura real.
 */

window.WTX = window.WTX || {};

(function aircraftLayer() {
  const entities = new Map();
  const states = new Map(); // id -> { anchorLat, anchorLon, headingDeg, speedKmh, altitude_m, anchorTimeMs, blendFromLat, blendFromLon, blendStartMs }
  const missingStreak = new Map(); // id -> quantas atualizacoes seguidas sem essa aeronave
  // As fontes reais (adsb.lol/adsb.fi/OpenSky) as vezes falham so em
  // PARTE da varredura (ex.: um hub deu rate-limit desta vez), entao um
  // avmatch real some do JSON de um refresh e volta no proximo. Antes
  // isso apagava a entidade na hora (pruneMissing rodava a cada 90s com
  // qualquer ausencia), causando o "some e depois de um tempo volta"
  // reportado. Agora uma aeronave so e removida depois de ficar ausente
  // por streaks seguidos - suaviza falhas passageiras sem esconder
  // aeronaves que realmente sairam da area por muito tempo.
  const MISSING_GRACE = 3; // mais tolerancia: refresh mais frequente
  let visible = false;
  let refreshTimer = null;
  let started = false;
  let lastSource = "demo";
  let refreshInFlight = false;
  let militaryOnly = false; // filtro "somente militares" (main.js)

  const CORRECTION_MS = 1200; // suavizacao ao chegar dado novo
  const MIN_TICK_MS = 100; // ~10x/s de extrapolacao (so CPU)
  // Limites do dead-reckoning: sem isso o aviao "voa sozinho" com
  // heading=0 ou speed estagnado por 60s e parece bugado.
  const MAX_DRIFT_MS = 22000; // para de extrapolar apos 22s sem dado novo
  const MIN_SPEED_KMH = 80; // abaixo disso fica parado na ancora
  const REFRESH_MS = 15000; // poll da API (antes 60s)
  const MAX_PLOT = 2200; // entidades Cesium por vez (viewport / amostra global)
  let catalog = []; // lista completa da ultima resposta da API
  let moveEndTimer = null;
  let lastTickAt = 0;
  let rafHandle = null;

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
      const span = Math.abs(east - west);
      // Vista muito aberta: trata como global (amostra)
      if (span > 120 || north - south > 90) return null;
      return [west, south, east, north];
    } catch (e) {
      return null;
    }
  }

  function selectForViewport(list) {
    if (!list || !list.length) return [];
    if (militaryOnly) list = list.filter((a) => a.is_military_confirmed);
    if (!list.length) return [];
    const bbox = getViewBbox();
    let pool = list;
    if (bbox) {
      const [minLon, minLat, maxLon, maxLat] = bbox;
      const inView = list.filter(
        (a) =>
          a.lat >= minLat &&
          a.lat <= maxLat &&
          a.lon >= minLon &&
          a.lon <= maxLon
      );
      // Se poucas na caixa, amplia um pouco o pool
      pool = inView.length >= 50 ? inView : list;
    }
    if (pool.length <= MAX_PLOT) return pool;
    // Prioriza militares, depois amostra uniforme
    const mil = pool.filter((a) => a.is_military_confirmed);
    const civil = pool.filter((a) => !a.is_military_confirmed);
    const out = mil.slice(0, MAX_PLOT);
    const remain = MAX_PLOT - out.length;
    if (remain > 0 && civil.length) {
      const step = civil.length / remain;
      for (let i = 0; i < remain; i++) out.push(civil[Math.floor(i * step)]);
    }
    return out;
  }

  function applyCatalog(source) {
    const subset = selectForViewport(catalog);
    const currentIds = new Set();
    subset.forEach((a) => {
      currentIds.add(a.id);
      upsertAircraft(a, source || lastSource);
    });
    pruneMissing(currentIds);
  }

  function onCameraMoveEnd() {
    if (!started || !visible) return;
    if (moveEndTimer) clearTimeout(moveEndTimer);
    moveEndTimer = setTimeout(() => applyCatalog(lastSource), 400);
  }

  function iconSvg(color) {
    return (
      "data:image/svg+xml;charset=UTF-8," +
      encodeURIComponent(
        `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
          <path d="M12 2 L14 10 L22 13 L14 14.5 L13 22 L11 22 L10 14.5 L2 13 L10 10 Z" fill="${color}" />
        </svg>`
      )
    );
  }

  function iconSvgHelicopter(color) {
    // Simbolo diferente (rotor + cauda) em vez da seta usada para aviao
    // de asa fixa - antes um helicoptero contato (raro, mas existe nos
    // dados reais: category "A7" no adsb.lol/fi/airplanes.live) ficava
    // visualmente identico a um aviao comum, entao passava despercebido
    // mesmo quando os dados chegavam.
    return (
      "data:image/svg+xml;charset=UTF-8," +
      encodeURIComponent(
        `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
          <circle cx="12" cy="9" r="2.6" fill="${color}" />
          <rect x="11.2" y="11" width="1.6" height="9" fill="${color}" />
          <rect x="7" y="18.3" width="10" height="1.6" fill="${color}" />
          <rect x="2" y="8.3" width="20" height="1.4" fill="${color}" />
        </svg>`
      )
    );
  }

  const ICON_LIVE = iconSvg("#f2d94e");
  const ICON_MILITARY = iconSvg("#d9534f"); // vermelho para militar confirmado
  const ICON_DEMO = iconSvg("#9c8a2e");
  const ICON_HELICOPTER = iconSvgHelicopter("#5bc8f2"); // azul claro - civil
  const ICON_HELICOPTER_MILITARY = iconSvgHelicopter("#d9534f"); // vermelho - militar

  // Fonte real e composta agora (ex.: "adsb_lol+adsb_fi+opensky"), entao
  // o teste correto e "!= demo" - o mesmo padrao ja usado em ships.js.
  function isLiveSource(source) {
    return !!source && source !== "demo";
  }

  // Formula do "ponto de destino" em navegacao esferica: dada uma
  // posicao, um rumo (heading) e uma distancia percorrida, devolve onde
  // a aeronave estara. E a mesma matematica usada em GPS/aviacao para
  // projetar uma rota reta sobre a esfera terrestre.
  function destinationPoint(lat, lon, headingDeg, distKm) {
    const R = 6371;
    const delta = distKm / R;
    const theta = Cesium.Math.toRadians(headingDeg || 0);
    const phi1 = Cesium.Math.toRadians(lat);
    const lambda1 = Cesium.Math.toRadians(lon);

    const sinPhi2 =
      Math.sin(phi1) * Math.cos(delta) + Math.cos(phi1) * Math.sin(delta) * Math.cos(theta);
    const phi2 = Math.asin(Math.max(-1, Math.min(1, sinPhi2)));
    const y = Math.sin(theta) * Math.sin(delta) * Math.cos(phi1);
    const x = Math.cos(delta) - Math.sin(phi1) * sinPhi2;
    const lambda2 = lambda1 + Math.atan2(y, x);

    const lat2 = Cesium.Math.toDegrees(phi2);
    const lon2 = ((Cesium.Math.toDegrees(lambda2) + 540) % 360) - 180;
    return [lat2, lon2];
  }

  function deadReckon(state, nowMs) {
    const elapsedMs = Math.max(0, nowMs - state.anchorTimeMs);
    // Sem velocidade util ou sem rumo confiavel: fica na ultima posicao real
    const speed = state.speedKmh || 0;
    const heading = state.headingDeg;
    if (speed < MIN_SPEED_KMH || heading == null || Number.isNaN(heading)) {
      return [state.anchorLat, state.anchorLon];
    }
    // Cap de extrapolacao: evita "voo fantasma" enquanto a API demora
    const cappedMs = Math.min(elapsedMs, MAX_DRIFT_MS);
    const distKm = speed * (cappedMs / 3600000);
    if (distKm <= 0.01) return [state.anchorLat, state.anchorLon];
    return destinationPoint(state.anchorLat, state.anchorLon, heading, distKm);
  }

  // Posicao exibida agora: extrapolada pela rota real, com uma curta
  // suavizacao (lerp) logo apos chegar um dado novo do servidor, pra
  // absorver a diferenca sem "pular" visualmente.
  function currentPosition(id, nowMs) {
    const state = states.get(id);
    if (!state) return null;
    const [lat, lon] = deadReckon(state, nowMs);
    if (state.blendStartMs != null) {
      const t = Math.min(1, (nowMs - state.blendStartMs) / CORRECTION_MS);
      if (t >= 1) {
        state.blendStartMs = null;
        return [lat, lon];
      }
      const blendLat = state.blendFromLat + (lat - state.blendFromLat) * t;
      const blendLon = state.blendFromLon + (lon - state.blendFromLon) * t;
      return [blendLat, blendLon];
    }
    return [lat, lon];
  }

  function upsertAircraft(a, source) {
    let icon = ICON_DEMO;
    if (isLiveSource(source)) {
      if (a.is_helicopter) {
        icon = a.is_military_confirmed ? ICON_HELICOPTER_MILITARY : ICON_HELICOPTER;
      } else {
        icon = a.is_military_confirmed ? ICON_MILITARY : ICON_LIVE;
      }
    }

    const now = performance.now();
    let entity = entities.get(a.id);
    let blendFrom = null;

    if (entity) {
      // Captura a posicao exibida ANTES de trocar a ancora - e o ponto
      // de partida da suavizacao (evita saltar direto pro dado novo).
      blendFrom = currentPosition(a.id, now);
    }

    const hdg =
      a.heading == null || a.heading === "" || Number.isNaN(Number(a.heading))
        ? null
        : Number(a.heading);
    const state = {
      anchorLat: a.lat,
      anchorLon: a.lon,
      headingDeg: hdg,
      speedKmh: a.speed_kmh || 0,
      altitude_m: a.altitude_m || 0,
      anchorTimeMs: now,
      blendFromLat: blendFrom ? blendFrom[0] : a.lat,
      blendFromLon: blendFrom ? blendFrom[1] : a.lon,
      blendStartMs: blendFrom ? now : null,
    };
    states.set(a.id, state);

    const position = Cesium.Cartesian3.fromDegrees(a.lon, a.lat, (a.altitude_m || 0) + 3000);

    if (!entity) {
      entity = window.WTX.viewer.entities.add({
        id: `aircraft-${a.id}`,
        position,
        billboard: {
          image: icon,
          scale: 0.7,
          rotation: Cesium.Math.toRadians(0),
          alignedAxis: Cesium.Cartesian3.ZERO,
          disableDepthTestDistance: 0, // respeita a oclusao do globo (nao mostra do outro lado)
        },
        properties: {},
      });
      entities.set(a.id, entity);
    } else {
      entity.billboard.image = icon;
    }

    // BUGFIX: aeronaves paradas/taxiando (on_ground=true) agora chegam
    // ate aqui (antes eram descartadas no backend, ver aircraft_service.py).
    // Opacidade reduzida so' pra distinguir visualmente de quem esta' em
    // rota - continuam clicaveis e com todos os dados normalmente.
    entity.billboard.color = a.on_ground
      ? Cesium.Color.WHITE.withAlpha(0.55)
      : Cesium.Color.WHITE;

    if (hdg != null) {
      entity.billboard.rotation = Cesium.Math.toRadians(360 - hdg);
    }
    const payload = { ...a, wtxType: "aircraft", wtxSource: source };
    entity.properties = payload;
    entity.wtxData = payload;
    entity.show = visible; // militares sempre visíveis e em vermelho
  }

  function pruneMissing(currentIds) {
    for (const [id, entity] of entities) {
      if (currentIds.has(id)) {
        missingStreak.delete(id);
        continue;
      }
      const streak = (missingStreak.get(id) || 0) + 1;
      if (streak >= MISSING_GRACE) {
        window.WTX.viewer.entities.remove(entity);
        entities.delete(id);
        states.delete(id);
        missingStreak.delete(id);
      } else {
        missingStreak.set(id, streak);
      }
    }
  }

  // rAF: aplica a extrapolacao de rota (dead reckoning) a cada frame -
  // e so calculo local, nenhuma chamada de rede aqui.
  function rafLoop(timestamp) {
    if (!started) {
      rafHandle = null;
      return;
    }
    if (timestamp - lastTickAt >= MIN_TICK_MS) {
      lastTickAt = timestamp;
      const now = performance.now();
      states.forEach((state, id) => {
        const entity = entities.get(id);
        if (!entity) return;
        const [lat, lon] = currentPosition(id, now);
        entity.position = Cesium.Cartesian3.fromDegrees(lon, lat, (state.altitude_m || 0) + 3000);
      });
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

  async function refresh() {
    if (refreshInFlight) return;
    refreshInFlight = true;
    try {
      const res = await fetch("/api/aircraft/");
      const data = await res.json();
      lastSource = data.source;
      catalog = data.aircraft || [];

      // TOTAL global no contador; plotagem so do viewport / amostra.
      // Com o filtro "somente militares" ligado, o contador mostra o
      // total de militares confirmados (nao o total geral) — senao o
      // numero exibido nao bateria com o que esta' de fato no globo.
      window.WTX.updateBadge("aircraft", data.is_live);
      const total = militaryOnly
        ? catalog.filter((a) => a.is_military_confirmed).length
        : typeof data.total === "number"
          ? data.total
          : data.count;
      window.WTX.updateCounter("aircraft", total);

      applyCatalog(data.source);
    } catch (err) {
      console.error("Falha ao atualizar aeronaves", err);
    } finally {
      refreshInFlight = false;
    }
  }

  // Usado pelo buscador (main.js): devolve {id, label, lon, lat, entity}
  // de cada aviao ja carregado, pra' busca por texto (callsign/icao).
  // So' acha o que ja foi buscado da API - se a camada nunca foi
  // ligada, entities esta' vazio e a busca nao acha nada aqui (o
  // buscador ainda assim liga a camada quando o usuario clica um
  // resultado, entao a proxima busca ja encontra).
  function searchAircraft(query) {
    const q = String(query || "").trim().toLowerCase();
    if (!q) return [];
    const out = [];
    entities.forEach((entity) => {
      const d = entity.wtxData || {};
      const callsign = String(d.callsign || "").toLowerCase();
      const icao = String(d.id || d.icao24 || "").toLowerCase();
      if (callsign.includes(q) || icao.includes(q)) {
        out.push({
          id: d.id,
          label: d.callsign || d.id,
          lon: d.lon,
          lat: d.lat,
          entity,
        });
      }
    });
    return out;
  }

  // Dispara o carregamento em segundo plano (sem acender nada no globo)
  // pra' que o buscador ache aviao mesmo com a camada desligada. Chamado
  // pelo buscador na primeira letra digitada.
  function ensureLoaded() {
    if (started) return;
    started = true;
    refresh();
    refreshTimer = setInterval(refresh, REFRESH_MS);
    startRafLoop();
    if (window.WTX.viewer) {
      window.WTX.viewer.camera.moveEnd.addEventListener(onCameraMoveEnd);
    }
  }

  window.WTX.aircraftLayer = {
    search: searchAircraft,
    ensureLoaded,
    setVisible(v) {
      visible = v;
      entities.forEach((e) => (e.show = v));
      if (v && !started) {
        started = true;
        refresh();
        refreshTimer = setInterval(refresh, REFRESH_MS);
        startRafLoop();
        if (window.WTX.viewer) {
          window.WTX.viewer.camera.moveEnd.addEventListener(onCameraMoveEnd);
        }
      } else if (!v && refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
        stopRafLoop();
        started = false;
      }
    },
    getSource: () => lastSource,
    setMilitaryOnly(v) {
      militaryOnly = !!v;
      if (started) {
        if (militaryOnly) {
          // Troca imediata: sem esperar o "grace period" do pruneMissing
          // (pensado pra falha passageira de rede, nao pra um filtro que
          // o usuario acabou de ligar) - remove civis na hora.
          for (const [id, entity] of entities) {
            const isMil = entity.wtxData && entity.wtxData.is_military_confirmed;
            if (!isMil) {
              window.WTX.viewer.entities.remove(entity);
              entities.delete(id);
              states.delete(id);
              missingStreak.delete(id);
            }
          }
        }
        applyCatalog(lastSource);
        const total = militaryOnly
          ? catalog.filter((a) => a.is_military_confirmed).length
          : catalog.length;
        window.WTX.updateCounter("aircraft", total);
      }
    },
  };
  // Nao carrega no boot — usuario ativa a camada no painel.
})();
