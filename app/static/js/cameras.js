/*
 * cameras.js
 * Camada de cameras publicas.
 *
 * - Plotagem: amostragem por viewport (bbox) para não travar o Cesium.
 * - CONTADOR: sempre mostra o TOTAL DA TERRA (global_total), não a
 *   quantidade da região visível. Assim o número não muda ao dar zoom.
 * - Detalhe/imagem sob demanda no clique.
 */

window.WTX = window.WTX || {};

(function cameraLayer() {
  const entities = new Map();
  let visible = false;
  let started = false;
  let listenersBound = false; // moveEnd so' e' registrado 1x na vida (ver boot())
  let lastSource = "demo";
  let lastOcvTotal = 0;
  let globalTotal = 0;   // TOTAL DA TERRA (fixo no contador)
  let refreshTimer = null;
  let moveEndTimer = null;
  let lastBboxKey = "";

  function renderCounterLabel() {
    // Sempre o TOTAL DO MUNDO, nunca a quantidade da região do zoom.
    window.WTX.updateCounter("cameras", globalTotal);
  }

  // Remove marker fantasma (sem imagem utilizável). Não altera o contador global.
  function dropCamera(id) {
    const entity = entities.get(id);
    if (entity) {
      window.WTX.viewer.entities.remove(entity);
      entities.delete(id);
    }
  }

  /* ---------------- Semaforo de atualizacao ----------------
   * O backend ainda manda em cada camera:
   *   status_color     "verde" | "amarelo" | "vermelho" | "cinza"
   *   refresh_s        intervalo de atualizacao em segundos
   *   refresh_measured true = cronometrado de verdade nesta camera;
   *                    false = estimativa da cadencia tipica da fonte
   * BUGFIX: isso NAO vira mais a cor do pino nem um filtro no painel
   * lateral (removidos - viravam ruido de UI). Todo pino de camera
   * agora e' sempre o mesmo verde-claro, como era antes do semaforo
   * existir. A info de velocidade de atualizacao continua disponivel,
   * so' que exclusivamente na PREVIA (painel de info) ao clicar na
   * camera - ver paintCameraInfo() em main.js.
   */
  const PIN_COLOR = "#7fe6a8"; // verde-claro fixo, igual pra toda camera
  let lastStatusCounts = { verde: 0, amarelo: 0, vermelho: 0, cinza: 0 };

  /* ---------------- Filtro por fonte de imagem ----------------
   * Cada camera traz `source` (rótulo da API de origem). O usuário
   * liga/desliga fontes no painel; padrão = todas ativas. Fontes novas
   * que aparecem no viewport entram no filtro já marcadas.
   */
  const sourceFilter = Object.create(null); // label -> bool
  let lastSourceCounts = Object.create(null); // label -> count plotado

  function camSourceLabel(cam) {
    const s = (cam && (cam.source || cam.wtxCamSource)) || "Desconhecida";
    return String(s).trim() || "Desconhecida";
  }

  function isSourceAllowed(label) {
    if (!label) return true;
    // Fonte ainda não registrada no filtro = mostrar (default on)
    if (!(label in sourceFilter)) return true;
    return sourceFilter[label] !== false;
  }

  function rebuildSourceCounts() {
    const counts = Object.create(null);
    entities.forEach((entity) => {
      const label = entity.wtxCamSource || (entity.wtxData && entity.wtxData.source) || "Desconhecida";
      counts[label] = (counts[label] || 0) + 1;
      if (!(label in sourceFilter)) sourceFilter[label] = true;
    });
    lastSourceCounts = counts;
    if (window.WTX.updateCameraSourceCounts) {
      window.WTX.updateCameraSourceCounts(counts, { ...sourceFilter });
    }
  }

  function cameraIcon() {
    return (
      "data:image/svg+xml;charset=UTF-8," +
      encodeURIComponent(
        `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">
          <rect x="2" y="5" width="10" height="8" rx="1.5" fill="${PIN_COLOR}" />
          <polygon points="12,7 16,5 16,13 12,11" fill="${PIN_COLOR}" />
        </svg>`
      )
    );
  }

  let _iconCache = null;
  function cameraIconFixed() {
    if (!_iconCache) _iconCache = cameraIcon();
    return _iconCache;
  }

  // Mantido so' pra' preencher o campo status_color no payload (usado
  // na PREVIA), sem influenciar mais a cor do pino nem nenhum filtro.
  function statusOf(cam) {
    const c = cam && cam.status_color;
    return c === "verde" || c === "amarelo" || c === "vermelho" || c === "cinza" ? c : "cinza";
  }

  function getViewBbox() {
    const viewer = window.WTX.viewer;
    if (!viewer) return null;
    try {
      const rect = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid);
      if (!rect) return null;
      const west = Cesium.Math.toDegrees(rect.west);
      const south = Cesium.Math.toDegrees(rect.south);
      const east = Cesium.Math.toDegrees(rect.east);
      const north = Cesium.Math.toDegrees(rect.north);
      // Vista quase global: nao manda bbox (backend usa amostra espacial).
      const spanLon = Math.abs(east - west);
      const spanLat = Math.abs(north - south);
      if (spanLon > 120 || spanLat > 80) return null;
      return [west, south, east, north];
    } catch (e) {
      return null;
    }
  }

  function upsertCamera(cam, source) {
    const color = statusOf(cam); // so' guardado pra' PREVIA, nao pinta o pino
    const icon = cameraIconFixed();
    const camSrc = camSourceLabel(cam);
    if (!(camSrc in sourceFilter)) sourceFilter[camSrc] = true;
    let entity = entities.get(cam.id);
    if (entity && !window.WTX.viewer.entities.contains(entity)) {
      // BUGFIX (liga/desliga camadas): a entidade sumiu da colecao do
      // Cesium por algum motivo externo (ex. troca de cena/reset), mas
      // ainda estava presa aqui no Map local. Sem isso, o "else" abaixo
      // so' atualizaria um objeto fantasma que nunca mais aparece no
      // globo - mesmo religando a camada. Descartar e recriar do zero.
      entities.delete(cam.id);
      entity = null;
    }
    if (!entity) {
      entity = window.WTX.viewer.entities.add({
        id: `camera-${cam.id}`,
        position: Cesium.Cartesian3.fromDegrees(cam.lon, cam.lat, 0),
        billboard: {
          image: icon,
          scale: 0.9,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: 0,
          scaleByDistance: undefined,
        },
      });
      entities.set(cam.id, entity);
    } else {
      entity.billboard.image = icon;
      entity.position = Cesium.Cartesian3.fromDegrees(cam.lon, cam.lat, 0);
    }
    const payload = { ...cam, wtxType: "camera", wtxSource: source, wtxCamSource: camSrc };
    entity.properties = payload;
    entity.wtxData = payload;
    entity.wtxStatus = color;
    entity.wtxCamSource = camSrc;
    entity.show = visible && isSourceAllowed(camSrc);
  }

  function applyFilter() {
    entities.forEach((entity) => {
      const src = entity.wtxCamSource || (entity.wtxData && entity.wtxData.source) || "";
      entity.show = visible && isSourceAllowed(src);
    });
  }

  function removeMissing(keepIds) {
    for (const [id, entity] of entities) {
      if (!keepIds.has(id)) {
        window.WTX.viewer.entities.remove(entity);
        entities.delete(id);
      }
    }
  }

  async function refresh(force) {
    if (!window.WTX.viewer) return;
    const bbox = getViewBbox();
    const bboxKey = bbox ? bbox.map((n) => n.toFixed(3)).join(",") : "global";
    if (!force && bboxKey === lastBboxKey && entities.size > 0) return;
    lastBboxKey = bboxKey;

    try {
      let url = "/api/cameras/";
      const params = [];
      if (bbox) {
        params.push(`bbox=${bbox.map((n) => n.toFixed(5)).join(",")}`);
        // Mais markers ao aproximar; o CONTADOR usa global_total (mundo inteiro)
        params.push("limit=3500");
      } else {
        params.push("limit=2000");
      }
      if (params.length) url += "?" + params.join("&");

      const res = await fetch(url);
      const data = await res.json();
      lastSource = data.source;
      lastOcvTotal = data.opencctv_total || 0;

      // CONTADOR: SEMPRE o total do catálogo (mundo), NUNCA data.count
      // (que é só a amostra plotada no viewport, ~2000).
      const catalogTotal = Number(data.global_total) || 0;
      const ocvCatalog = Number(data.opencctv_total) || 0;
      if (catalogTotal > 0) {
        globalTotal = catalogTotal;
      } else if (ocvCatalog > 0) {
        globalTotal = ocvCatalog;
      }
      // Nunca deixar o contador cair para o tamanho da amostra plotada.
      if (globalTotal > 0 && data.count && globalTotal <= data.count && ocvCatalog > data.count) {
        globalTotal = ocvCatalog;
      }

      const keep = new Set();
      const camList = data.cameras || [];

      // Plotagem em lotes (chunks) por frame em vez de um forEach único.
      // Com milhares de cameras, montar tudo de uma vez numa única
      // chamada síncrona prende a thread principal e os pinos só
      // aparecem no globo (todos de uma vez) depois que o loop inteiro
      // termina — dando a sensação de demora. Quebrando em lotes com
      // requestAnimationFrame, os primeiros pinos aparecem quase na
      // hora e o resto vai entrando progressivamente, sem travar a UI.
      const CHUNK_SIZE = 150;
      let idx = 0;

      function plotChunk() {
        const end = Math.min(idx + CHUNK_SIZE, camList.length);
        for (; idx < end; idx++) {
          const cam = camList[idx];
          upsertCamera(cam, data.source);
          keep.add(cam.id);
        }
        if (idx < camList.length) {
          (window.requestAnimationFrame || window.setTimeout)(plotChunk, 0);
          return;
        }
        finishRefresh();
      }

      function finishRefresh() {
        // Remove só markers OpenCCTV fora do viewport; mantém fontes fixas.
        for (const [id, entity] of entities) {
          const isOcv = String(id).startsWith("ocv-");
          if (isOcv && !keep.has(id)) {
            window.WTX.viewer.entities.remove(entity);
            entities.delete(id);
          }
        }

        if (data.status_counts) {
          lastStatusCounts = data.status_counts;
          if (window.WTX.updateCameraStatusCounts) {
            window.WTX.updateCameraStatusCounts(lastStatusCounts);
          }
        }
        applyFilter();
        rebuildSourceCounts();

        window.WTX.updateBadge("cameras", data.is_live);
        renderCounterLabel();
      }

      if (camList.length) {
        plotChunk();
      } else {
        finishRefresh();
      }
    } catch (err) {
      console.error("Falha ao atualizar cameras", err);
    }
  }

  function onCameraMoveEnd() {
    if (moveEndTimer) clearTimeout(moveEndTimer);
    moveEndTimer = setTimeout(() => refresh(true), 450);
  }

  function boot() {
    if (!window.WTX.viewer) {
      setTimeout(boot, 200);
      return;
    }
    // BUGFIX (liga/desliga camadas): antes, boot() rodava de novo a
    // cada vez que a camada era religada (porque "started" virava
    // false ao desligar) e registrava OUTRO listener de moveEnd sem
    // nunca remover o anterior - a cada ciclo desligar/religar sobrava
    // mais um listener idêntico grudado pra sempre. "listenersBound"
    // garante que isso só acontece 1 vez na vida da página; religar a
    // camada nunca mais acumula nada.
    if (!listenersBound) {
      listenersBound = true;
      window.WTX.viewer.camera.moveEnd.addEventListener(onCameraMoveEnd);
    }
    if (!refreshTimer) {
      refreshTimer = setInterval(() => refresh(true), 60000);
    }
  }

  window.WTX.cameraLayer = {
    setVisible(v) {
      visible = v;
      applyFilter();
      if (v) {
        started = true;
        boot();
        // BUGFIX (liga/desliga camadas): antes, refresh(true) só era
        // chamado dentro de boot(), que só rodava fundo-a-fundo
        // (listener + timer) na PRIMEIRA vez que a camada ligava -
        // religar dependia inteiramente de applyFilter() reaproveitar
        // as entidades já existentes no Map. Qualquer dessincronia
        // entre esse Map e a coleção real do Cesium (troca de cena,
        // uma entidade removida por outro motivo, uma resposta que
        // chegou fora de ordem) deixava a câmera "presa" invisível,
        // sem nenhum jeito de trazê-la de volta a não ser recarregar a
        // página. Chamando refresh(true) toda vez que a camada liga
        // (não só na primeira), o mesmo caminho que funcionou da
        // primeira vez roda de novo sempre - impossível ficar preso.
        refresh(true);
      } else {
        if (refreshTimer) {
          clearInterval(refreshTimer);
          refreshTimer = null;
        }
        started = false;
      }
    },
    getSource: () => lastSource,
    getOcvTotal: () => lastOcvTotal,
    refresh: () => refresh(true),
    dropCamera,
    getStatusCounts: () => ({ ...lastStatusCounts }),
    setSourceFilter(label, on) {
      if (!label) return;
      sourceFilter[label] = !!on;
      applyFilter();
      rebuildSourceCounts();
    },
    setAllSourceFilters(on) {
      Object.keys(sourceFilter).forEach((k) => {
        sourceFilter[k] = !!on;
      });
      applyFilter();
      rebuildSourceCounts();
    },
    getSourceFilter: () => ({ ...sourceFilter }),
    getSourceCounts: () => ({ ...lastSourceCounts }),
  };

  // Enrichment no clique: detalhe OpenCCTV sob demanda.
  window.WTX.resolveCameraDetail = async function (cam) {
    if (!cam || !cam.id) return cam;
    if (!cam.lite && !String(cam.id).startsWith("ocv-")) return cam;
    if (cam.location && cam.location !== "OpenCCTV" && !cam.lite) return cam;
    try {
      const res = await fetch(`/api/cameras/detail/${encodeURIComponent(cam.id)}`);
      if (!res.ok) return cam;
      const data = await res.json();
      if (data.camera) {
        const merged = { ...cam, ...data.camera, lite: false, wtxType: "camera", wtxSource: cam.wtxSource || "opencctv_live" };
        // So remove se NAO tem nenhum feed tocavel (imagem, HLS, YT, mjpeg…).
        // stream_hint / page_url / feed_type usaveis mantem a camera no mapa.
        const hasUsableFeed =
          !!merged.image_url ||
          !!(merged.stream_hint && (merged.stream_hint.type === "hls" || merged.stream_hint.type === "youtube")) ||
          !!(merged.page_url && /(?:youtube\.com|youtu\.be)/i.test(merged.page_url)) ||
          ["image", "mjpeg", "m3u8", "iframe", "mp4"].includes(String(merged.feed_type || "").toLowerCase());
        if (!hasUsableFeed && merged.wtxSource !== "demo") {
          dropCamera(cam.id);
          return { ...merged, _unavailable: true };
        }
        const entity = entities.get(cam.id);
        if (entity) {
          entity.wtxData = merged;
          entity.properties = merged;
          // O status agora so' e' recalculado pra' PREVIA (nao pinta
          // mais o pino nem afeta o que fica visivel no globo).
          entity.wtxStatus = statusOf(merged);
          const srcLabel = entity.wtxCamSource || (merged && merged.source) || "";
          entity.show = visible && isSourceAllowed(srcLabel);
          // BUGFIX: o marker era plantado uma vez com a coordenada do
          // indice "leve" (/api/cameras/markers, ~144k pontos, menos
          // confiavel/mais generico) e nunca reposicionado quando o
          // detalhe real da camera (/api/cameras/batch) trazia seu
          // proprio lat/lng - podendo deixar o pino no lugar errado
          // pra sempre, mesmo com o dado correto em maos. Agora, se o
          // detalhe trouxe coordenada valida e ela difere da usada pro
          // marker, o pino e corrigido na hora.
          const hasValidCoords =
            typeof merged.lat === "number" && typeof merged.lon === "number" &&
            !Number.isNaN(merged.lat) && !Number.isNaN(merged.lon);
          if (hasValidCoords && (merged.lat !== cam.lat || merged.lon !== cam.lon)) {
            entity.position = Cesium.Cartesian3.fromDegrees(merged.lon, merged.lat, 0);
          }
        }
        return merged;
      }
    } catch (e) {
      console.warn("detalhe camera falhou", e);
    }
    return cam;
  };

  // lazy: boot só quando o usuário ativa a camada
})();
