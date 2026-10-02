/*
 * main.js
 * Amarra a interface (HUD) aos dados e ao globo: relogio, badges,
 * contadores, toggles de camadas, cliques em objetos e busca.
 */

window.WTX = window.WTX || {};

// ---------- Relogio e status de conexao ----------

function tickClock() {
  const el = document.getElementById("clock");
  if (!el) return;
  // Horario de Brasilia (America/Sao_Paulo) em vez de UTC. Usa
  // Intl.DateTimeFormat com timeZone fixo — cobre o horario certo
  // independente do fuso do navegador de quem acessa, e nao depende de
  // horario de verao (Brasil nao usa mais desde 2019).
  const hora = new Intl.DateTimeFormat("pt-BR", {
    timeZone: "America/Sao_Paulo",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
  el.textContent = hora + " BRASÍLIA";
}
setInterval(tickClock, 1000);
tickClock();

setTimeout(() => {
  const conn = document.getElementById("connStatus");
  if (conn) conn.textContent = "CONECTADO";
}, 900);

// ---------- Badges DEMO / AO VIVO ----------

window.WTX.updateBadge = function (layer, isLive) {
  const el = document.getElementById(`badge-${layer}`);
  if (!el) return;
  el.textContent = isLive ? "AO VIVO" : "DEMO";
  el.classList.toggle("live", !!isLive);
};

window.WTX.updateCounter = function (layer, value) {
  const el = document.getElementById(`count-${layer}`);
  if (!el) return;
  el.textContent =
    typeof value === "number" ? value.toLocaleString("pt-BR") : String(value);
};

// ---------- Toggle de camadas ----------

const layerHandlers = {
  aircraft: (v) => window.WTX.aircraftLayer && window.WTX.aircraftLayer.setVisible(v),
  satellites: (v) => window.WTX.satelliteLayer && window.WTX.satelliteLayer.setVisible(v),
  cameras: (v) => window.WTX.cameraLayer && window.WTX.cameraLayer.setVisible(v),
  ships: (v) => window.WTX.shipLayer && window.WTX.shipLayer.setVisible(v),
  boundaries: (v) => window.WTX.boundariesLayer && window.WTX.boundariesLayer.setVisible(v),
  powerplants: (v) => window.WTX.powerPlantsLayer && window.WTX.powerPlantsLayer.setVisible(v),
  cables: (v) => window.WTX.cablesLayer && window.WTX.cablesLayer.setVisible(v),
  datacenters: (v) => window.WTX.datacentersLayer && window.WTX.datacentersLayer.setVisible(v),
  computers: (v) => window.WTX.computerLayer && window.WTX.computerLayer.setVisible(v),
};

Object.keys(layerHandlers).forEach((layer) => {
  const checkbox = document.getElementById(`layer-${layer}`);
  if (!checkbox) return;
  checkbox.addEventListener("change", (e) => layerHandlers[layer](e.target.checked));
});

// Varredura da camada de computadores é sob demanda (cache de 30min no
// backend) — este botão força ignorar o cache e escanear de novo.
const computersRescanBtn = document.getElementById("computersRescanBtn");
if (computersRescanBtn) {
  computersRescanBtn.addEventListener("click", () => {
    if (window.WTX.computerLayer) window.WTX.computerLayer.rescan();
  });
}

// ---------- Filtro por tipo de usina (INFRAFISICA.md, Bloco 2) ----------
// 5 sub-checkboxes fixos (hidrelétrica/nuclear/carvão/solar/eólica), ao
// contrário do filtro de câmeras que é dinâmico — aqui o conjunto de
// tipos é conhecido de antemão, então não precisa montar a lista em
// runtime.
["hydro", "nuclear", "coal", "solar", "wind"].forEach((fuel) => {
  const checkbox = document.getElementById(`pp-${fuel}`);
  if (!checkbox) return;
  checkbox.addEventListener("change", (e) => {
    if (window.WTX.powerPlantsLayer) {
      window.WTX.powerPlantsLayer.setFuelEnabled(fuel, e.target.checked);
    }
  });
});

// A caixa "FONTES DE IMAGEM" so' faz sentido com a camada de cameras
// ligada e dados na tela - antes ela ficava sempre visivel com um
// texto de placeholder ("Ative a camada..."), o que so' era ruido no
// painel. Agora fica escondida ate' updateCameraSourceCounts() receber
// pelo menos 1 fonte, e some de novo se a camada for desligada.
const camSourceFilterBox = document.getElementById("camSourceFilter");
const camerasCheckbox = document.getElementById("layer-cameras");
if (camerasCheckbox && camSourceFilterBox) {
  camerasCheckbox.addEventListener("change", (e) => {
    if (!e.target.checked) camSourceFilterBox.style.display = "none";
  });
}

// BUGFIX: removido o filtro por cor/velocidade de atualizacao (semaforo)
// do painel lateral - a velocidade de atualizacao de cada camera agora
// so aparece na PREVIA (painel de info) ao clicar numa camera
// especifica, dentro de paintCameraInfo() (ver "Atualização" logo
// abaixo do status). O globo nao filtra mais por essa cor.

// ---------- Filtro por fonte de imagem das cameras ----------
// Lista dinâmica: só mostra fontes presentes na região plotada.
// Cores estáveis por hash do nome para o "dot" de cada linha.
const SOURCE_DOT_PALETTE = [
  "#38bdf8", "#a78bfa", "#34d399", "#fb923c", "#f472b6",
  "#22d3ee", "#c084fc", "#4ade80", "#fbbf24", "#e879f9",
  "#2dd4bf", "#60a5fa", "#f87171", "#a3e635", "#e2e8f0",
];
function sourceDotColor(label) {
  let h = 0;
  const s = String(label || "");
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return SOURCE_DOT_PALETTE[h % SOURCE_DOT_PALETTE.length];
}
function shortSourceName(label) {
  // Encurta rótulos longos do backend para caber no painel.
  const map = {
    "NYC DOT (nyctmc.org)": "NYC DOT",
    "Caltrans CCTV (dot.ca.gov)": "Caltrans CCTV",
    "UDOT Traffic (Utah, EUA)": "UDOT Utah",
    "Ontario 511 (Canadá)": "Ontario 511",
    "Alberta 511 (Canadá)": "Alberta 511",
    "TfL JamCams (Londres, Reino Unido)": "TfL JamCams",
    "Clima ao Vivo (climaaovivo.com.br)": "Clima ao Vivo",
    "Digitraffic / Fintraffic (FI)": "Digitraffic FI",
    "Florida 511 (US)": "Florida 511",
    "Arizona 511 (US)": "Arizona 511",
    "Georgia 511 (US)": "Georgia 511",
    "North Carolina 511 (US)": "N. Carolina 511",
    "Nevada 511 (US)": "Nevada 511",
    "Idaho 511 (US)": "Idaho 511",
    "Utah mapIcons (US)": "Utah map",
    "DelDOT (US)": "DelDOT",
    "EarthCam Network": "EarthCam",
    "Webcamera24": "Webcamera24",
    "SkylineWebcams": "SkylineWebcams",
    "OpenCCTV (opencctv.org)": "OpenCCTV",
    "CET São Paulo (cetsp.com.br)": "CET São Paulo",
    "HK Transport Dept (data.gov.hk)": "HK Transport Dept",
  };
  if (map[label]) return map[label];
  // fallback: corta em parêntese ou limita tamanho
  const cut = String(label).split(" (")[0].trim();
  return cut.length > 22 ? cut.slice(0, 20) + "…" : cut;
}

window.WTX.updateCameraSourceCounts = function (counts, filterState) {
  const list = document.getElementById("camSourceList");
  const box = document.getElementById("camSourceFilter");
  if (!list) return;
  const entries = Object.keys(counts || {}).sort((a, b) => {
    // fontes com mais cameras primeiro; desempate alfabético
    const d = (counts[b] || 0) - (counts[a] || 0);
    return d !== 0 ? d : a.localeCompare(b);
  });
  if (!entries.length) {
    if (box) box.style.display = "none";
    list.innerHTML = "";
    return;
  }
  if (box) box.style.display = "";
  // Preserva estado dos checkboxes ao re-renderizar
  const prevChecked = {};
  list.querySelectorAll(".cam-source-row").forEach((row) => {
    const key = row.dataset.source;
    const box = row.querySelector("input");
    if (key && box) prevChecked[key] = box.checked;
  });

  list.innerHTML = entries
    .map((label) => {
      const checked =
        filterState && label in filterState
          ? filterState[label] !== false
          : prevChecked[label] !== false;
      const n = counts[label] || 0;
      const short = shortSourceName(label);
      const dot = sourceDotColor(label);
      const esc = String(label)
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;");
      const escShort = short
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;");
      return (
        `<label class="cam-status-row cam-source-row" data-source="${esc}" title="${esc}">` +
        `<input type="checkbox"${checked ? " checked" : ""} />` +
        `<span class="cam-status-dot" style="--dot:${dot}"></span>` +
        `<span class="cam-status-name">${escShort}</span>` +
        `<span class="cam-status-count">${n}</span>` +
        `</label>`
      );
    })
    .join("");

  list.querySelectorAll(".cam-source-row").forEach((rowEl) => {
    const label = rowEl.dataset.source;
    const box = rowEl.querySelector("input");
    if (!label || !box) return;
    box.addEventListener("change", () => {
      if (window.WTX.cameraLayer) {
        window.WTX.cameraLayer.setSourceFilter(label, box.checked);
      }
    });
  });
};

const camSourceAllBtn = document.getElementById("camSourceAll");
const camSourceNoneBtn = document.getElementById("camSourceNone");
if (camSourceAllBtn) {
  camSourceAllBtn.addEventListener("click", () => {
    if (window.WTX.cameraLayer) window.WTX.cameraLayer.setAllSourceFilters(true);
  });
}
if (camSourceNoneBtn) {
  camSourceNoneBtn.addEventListener("click", () => {
    if (window.WTX.cameraLayer) window.WTX.cameraLayer.setAllSourceFilters(false);
  });
}

// Toggle independente das orbitas dos satelites (nao afeta os pontos).
// Quando ligado, mostra órbitas de TODOS; quando desligado, ainda é possível
// mostrar a órbita de um único satélite pelo painel de info.
const orbitsCheckbox = document.getElementById("layer-orbits");
if (orbitsCheckbox) {
  orbitsCheckbox.addEventListener("change", (e) => {
    if (window.WTX.satelliteLayer) window.WTX.satelliteLayer.setOrbitsVisible(e.target.checked);
  });
}

// Filtro "somente militares" das aeronaves — independente do toggle
// principal da camada, igual ao padrao das orbitas acima.
const aircraftMilCheckbox = document.getElementById("layer-aircraft-mil");
if (aircraftMilCheckbox) {
  aircraftMilCheckbox.addEventListener("change", (e) => {
    if (window.WTX.aircraftLayer) window.WTX.aircraftLayer.setMilitaryOnly(e.target.checked);
  });
}

// ---------- Controles do globo ----------

document.getElementById("ctrl-zoom-in").addEventListener("click", () => window.WTX.zoomBy(0.5));
document.getElementById("ctrl-zoom-out").addEventListener("click", () => window.WTX.zoomBy(-1));
document.getElementById("ctrl-reset").addEventListener("click", () => window.WTX.resetCamera());
document.getElementById("ctrl-locate").addEventListener("click", () => window.WTX.locateUser());
document.getElementById("ctrl-fullscreen").addEventListener("click", () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen();
  else document.exitFullscreen();
});

// ---------- Painel de informacoes de objeto ----------

const infoPanel = document.getElementById("infoPanel");
const infoTitle = document.getElementById("infoTitle");
const infoBody = document.getElementById("infoBody");
document.getElementById("infoClose").addEventListener("click", () => {
  infoPanel.classList.add("hidden");
  if (window.WTX._teardownCameraStream) window.WTX._teardownCameraStream();
});

function row(label, value) {
  return `<div class="info-row"><span>${label}</span><span>${value}</span></div>`;
}

function renderAircraftInfo(p) {
  infoTitle.textContent = "AERONAVE";
  // wtxSource agora pode ser composto (ex.: "adsb_lol+adsb_fi+opensky"),
  // entao o teste e generico - igual ao usado em renderShipInfo. A
  // versao antiga so reconhecia 3 valores fixos e nunca incluia
  // "adsb_lol" corretamente combinado, entao aeronaves reais podiam
  // cair no rotulo de "SIMULAÇÃO / DADOS DEMO".
  const isLiveAircraft = !!p.wtxSource && p.wtxSource !== "demo";
  const flag = isLiveAircraft
    ? `<div class="info-flag live">AO VIVO · ${String(p.wtxSource).toUpperCase().replace(/_/g, ".").replace(/\+/g, " + ")}</div>`
    : '<div class="info-flag">SIMULAÇÃO / DADOS DEMO</div>';
  // So mostra o selo de militar confirmado se o dado tambem for de
  // fonte real - nunca em cima de dados demo, mesmo que algo mude
  // is_military_confirmed inesperadamente no futuro.
  const isConfirmedMilitary = isLiveAircraft && !!p.is_military_confirmed;
  const confirmedMilitary = isConfirmedMilitary
    ? '<div class="info-mil-badge military">MILITAR CONFIRMADO</div>'
    : "";
  // is_helicopter vem do campo "category" (A7) do adsb.lol/fi/airplanes.live,
  // ou do tipo ICAO como fallback - ver _looks_like_helicopter() no backend.
  // OpenSky nao manda essa info, entao fica null (nao mostra o selo, mas
  // tambem nao afirma "nao e helicoptero" por engano).
  const helicopterBadge =
    isLiveAircraft && p.is_helicopter
      ? '<div class="info-mil-badge" style="background:#5bc8f2">HELICÓPTERO</div>'
      : "";

  infoBody.innerHTML =
    row("Voo", p.callsign) +
    row("Altitude", `${(p.altitude_ft ?? 0).toLocaleString("pt-BR")} ft`) +
    row("Velocidade", `${p.speed_kmh ?? "—"} km/h`) +
    row("Direção", `${Math.round(p.heading || 0)}°`) +
    row("País (OpenSky)", p.origin_country || "—") +
    // BUGFIX: antes "EM VOO" era fixo - agora que aeronaves paradas no
    // chao (on_ground=true) tambem chegam ate aqui (ver aircraft_service.py),
    // o status precisa refletir isso de verdade.
    row("Status", p.on_ground ? "NO SOLO" : "EM VOO") +
    flag +
    confirmedMilitary +
    helicopterBadge +
    '<div id="routeDetails" class="info-extra">Carregando origem / destino...</div>' +
    '<div id="extraDetails" class="info-extra">Carregando detalhes da aeronave...</div>';

  if (isLiveAircraft) {
    // So faz sentido consultar a base publica real (adsbdb) quando a
    // aeronave em si vem de uma fonte real - para dados demo o ID e
    // fabricado e a consulta so devolveria "nao encontrado", o que
    // parecia (erradamente) um aviso sobre um aviao real oculto.
    fetchAndRenderAircraftDetails(p.id, isConfirmedMilitary);
    fetchAndRenderAircraftRoute(p.callsign);
  } else {
    const box = document.getElementById("extraDetails");
    if (box) {
      box.innerHTML = '<div class="info-note">Esta é uma aeronave simulada (dado de demonstração) - não existe consulta real a fazer sobre ela.</div>';
    }
    const routeBox = document.getElementById("routeDetails");
    if (routeBox) {
      routeBox.innerHTML = '<div class="info-note">Rota indisponível para dados de demonstração.</div>';
    }
  }
}

async function fetchAndRenderAircraftRoute(callsign) {
  const box = document.getElementById("routeDetails");
  if (!box) return;
  const cs = (callsign || "").trim().toUpperCase();
  // Callsigns de linha aérea costumam ser 3 letras + números (ex.: BAW123).
  // Matrículas / GA / militares quase nunca têm rota cadastrada e só geram 404.
  if (!cs || !/^[A-Z]{3}\d/.test(cs)) {
    box.innerHTML =
      '<div class="info-extra-title">ROTA DO VOO</div>' +
      '<div class="info-note">Sem rota cadastrada para este callsign (comum em voos GA, militares ou callsign sem padrão de linha aérea).</div>';
    return;
  }
  try {
    const res = await fetch(`/api/aircraft/route/${encodeURIComponent(cs)}`);
    const data = await res.json();
    if (!box.isConnected) return;
    const r = data.route;
    if (!r) {
      box.innerHTML =
        '<div class="info-extra-title">ROTA DO VOO</div>' +
        '<div class="info-note">Rota não encontrada na base pública para este callsign.</div>';
      return;
    }
    const originLabel = r.origin
      ? `${r.origin.name || r.origin.icao_code || "—"} (${r.origin.icao_code || r.origin.iata_code || "—"}) — ${r.origin.municipality || ""} ${r.origin.country_name || ""}`.trim()
      : "—";
    const destLabel = r.destination
      ? `${r.destination.name || r.destination.icao_code || "—"} (${r.destination.icao_code || r.destination.iata_code || "—"}) — ${r.destination.municipality || ""} ${r.destination.country_name || ""}`.trim()
      : "—";
    const airline = r.airline
      ? `${r.airline.name || "—"} (${r.airline.icao || r.airline.iata || "—"})`
      : "—";
    box.innerHTML =
      '<div class="info-extra-title">ROTA DO VOO</div>' +
      row("Companhia", airline) +
      row("De (origem)", originLabel) +
      row("Para (destino)", destLabel) +
      '<div class="info-note">Rota provável associada ao callsign (adsbdb). Pode não refletir o voo real do momento se o callsign for reutilizado.</div>';
  } catch (err) {
    if (box.isConnected) {
      box.innerHTML =
        '<div class="info-extra-title">ROTA DO VOO</div>' +
        '<div class="info-note">Falha ao consultar origem/destino.</div>';
    }
  }
}

async function fetchAndRenderAircraftDetails(icao24, isMilitaryConfirmed) {
  const box = document.getElementById("extraDetails");
  if (!box) return;

  try {
    const res = await fetch(`/api/aircraft/${encodeURIComponent(icao24)}/details`);
    const data = await res.json();
    const d = data.details;

    if (!box.isConnected) return; // usuario ja trocou de objeto

    if (!d) {
      const msg = data.source === "not_found"
        ? "Esta aeronave não está cadastrada na base pública consultada (comum em aeronaves militares, privadas ou de matrícula bloqueada)."
        : "Não foi possível consultar a base de dados agora (falha de conexão). Tente clicar novamente em instantes.";
      box.innerHTML = `<div class="info-note">${msg}</div>`;
      return;
    }

    const flagImg = d.flag_url ? `<img class="info-flag-icon" src="${d.flag_url}" alt="${d.country_name || ''}" />` : "";
    const photoImg = d.photo_url ? `<img class="info-photo" src="${d.photo_url}" alt="Foto da aeronave" />` : "";
    // Se a origem confirmou militar (airplanes.live), nao usamos mais a
    // heuristica por palavra-chave nem seu aviso de estimativa.
    const militaryBadge = isMilitaryConfirmed
      ? ""
      : d.is_military
        ? '<div class="info-mil-badge military">PROVÁVEL MILITAR/GOVERNAMENTAL</div>'
        : '<div class="info-mil-badge civil">PROVÁVEL CIVIL</div>';
    const note = isMilitaryConfirmed
      ? ""
      : '<div class="info-note">Classificação militar/civil é uma estimativa por palavras-chave, não uma confirmação oficial.</div>';

    box.innerHTML =
      '<div class="info-extra-title">DETALHES DA AERONAVE</div>' +
      photoImg +
      row("Fabricante", d.manufacturer || "—") +
      row("Modelo", d.type || d.icao_type || "—") +
      row("Registro", d.registration || "—") +
      row("Operador", d.registered_owner || "—") +
      `<div class="info-row"><span>País</span><span>${flagImg} ${d.country_name || "—"}</span></div>` +
      militaryBadge +
      note;
  } catch (err) {
    if (box.isConnected) box.innerHTML = '<div class="info-note">Falha ao buscar detalhes da aeronave.</div>';
  }
}

function renderSatelliteInfo(p) {
  infoTitle.textContent = "SATÉLITE";
  const src = p.wtxSource || "";
  const isLiveSat =
    src === "satnogs_live" ||
    src === "celestrak_live" ||
    src === "celestrak_multi";
  const flag = isLiveSat
    ? `<div class="info-flag live">AO VIVO · ${
        src === "satnogs_live" ? "SATNOGS" : "CELESTRAK / NORAD"
      }</div>`
    : '<div class="info-flag">SIMULAÇÃO / DADOS DEMO</div>';
  const milBadge = p.is_military
    ? '<div class="info-mil-badge military">PROVÁVEL MILITAR/GOVERNAMENTAL</div>'
    : "";
  infoBody.innerHTML =
    row("Nome", p.name) +
    row("Altitude", `${(p.altitude_km ?? 0).toLocaleString("pt-BR")} km`) +
    row("Velocidade", `${p.velocity_kms ?? "—"} km/s`) +
    row("Inclinação", `${p.inclination_deg ?? "—"}°`) +
    row("Órbita", p.orbit_class) +
    row("Status", "RASTREANDO") +
    flag +
    milBadge +
    `<button class="info-track-btn" id="trackBtn">RASTREAR OBJETO</button>` +
    `<button class="info-track-btn" id="orbitBtn" style="margin-top:6px;">MOSTRAR ÓRBITA DESTE</button>` +
    '<div id="extraDetails" class="info-extra">Carregando detalhes do satélite...</div>';

  fetchAndRenderSatelliteDetails(p.norad_id);

  const orbitBtn = document.getElementById("orbitBtn");
  if (orbitBtn && window.WTX.satelliteLayer) {
    const onlyThis = window.WTX.satelliteLayer.isOrbitOnly(p.id);
    orbitBtn.textContent = onlyThis
      ? "OCULTAR ÓRBITA DESTE"
      : "MOSTRAR ÓRBITA DESTE";
    orbitBtn.addEventListener("click", () => {
      const nowOnly = window.WTX.satelliteLayer.toggleOrbitOnly(p.id);
      orbitBtn.textContent = nowOnly
        ? "OCULTAR ÓRBITA DESTE"
        : "MOSTRAR ÓRBITA DESTE";
    });
  }
}

async function fetchAndRenderSatelliteDetails(noradId) {
  const box = document.getElementById("extraDetails");
  if (!box) return;

  if (!noradId) {
    box.innerHTML = '<div class="info-note">Satélite simulado (dado de demonstração) - não existe registro real no catálogo NORAD para consultar.</div>';
    return;
  }

  try {
    const res = await fetch(`/api/satellites/${encodeURIComponent(noradId)}/details`);
    const data = await res.json();
    const d = data.details;

    if (!box.isConnected) return;

    if (!d) {
      const msg = data.source === "not_found"
        ? "Este NORAD ID não foi encontrado no catálogo SATCAT."
        : "Não foi possível consultar o CelesTrak agora (falha de conexão). Tente clicar novamente em instantes.";
      box.innerHTML = `<div class="info-note">${msg}</div>`;
      return;
    }

    const flagImg = d.flag_url ? `<img class="info-flag-icon" src="${d.flag_url}" alt="${d.owner_name || ''}" />` : "";
    const photoImg = d.image_url ? `<img class="info-photo" src="${d.image_url}" alt="Foto do satélite" />` : "";
    const militaryBadge = d.is_military
      ? '<div class="info-mil-badge military">PROVÁVEL MILITAR/GOVERNAMENTAL</div>'
      : '<div class="info-mil-badge civil">PROVÁVEL CIVIL/COMERCIAL</div>';

    box.innerHTML =
      '<div class="info-extra-title">DETALHES DO CATÁLOGO (SATCAT)</div>' +
      photoImg +
      row("Nome oficial", d.object_name || "—") +
      row("Tipo de objeto", d.object_type || "—") +
      `<div class="info-row"><span>País/Operador</span><span>${flagImg} ${d.owner_name || "—"}</span></div>` +
      row("Lançamento", d.launch_date || "—") +
      row("Local de lançamento", d.launch_site || "—") +
      row("Perigeu", d.perigee_km ? `${d.perigee_km} km` : "—") +
      row("Apogeu", d.apogee_km ? `${d.apogee_km} km` : "—") +
      militaryBadge +
      '<div class="info-note">Classificação militar/civil é uma estimativa por palavras-chave, não uma confirmação oficial.</div>';
  } catch (err) {
    if (box.isConnected) box.innerHTML = '<div class="info-note">Falha ao buscar detalhes do satélite.</div>';
  }
}

let _cameraRefreshTimer = null;
let _cameraLightboxRefreshTimer = null;
let _lightboxHls = null;
let _lastStreamInfo = null; // { type, playlist_url | video_id } da ultima extracao no painel

const cameraLightbox = document.getElementById("cameraLightbox");
const cameraLightboxImg = document.getElementById("cameraLightboxImg");
const cameraLightboxMedia = document.getElementById("cameraLightboxMedia");
const cameraLightboxTitle = document.getElementById("cameraLightboxTitle");
const cameraLightboxFlag = document.getElementById("cameraLightboxFlag");
const cameraLightboxSource = document.getElementById("cameraLightboxSource");
const cameraLightboxStatus = document.getElementById("cameraLightboxStatus");
const cameraLightboxClose = document.getElementById("cameraLightboxClose");
const cameraLightboxFullscreen = document.getElementById("cameraLightboxFullscreen");

function _teardownLightboxMedia() {
  if (_lightboxHls) {
    try { _lightboxHls.destroy(); } catch (err) { /* noop */ }
    _lightboxHls = null;
  }
  if (_cameraLightboxRefreshTimer) {
    clearInterval(_cameraLightboxRefreshTimer);
    _cameraLightboxRefreshTimer = null;
  }
  if (cameraLightboxMedia) {
    // Mantém só o img base; remove video/iframe extras
    const extras = cameraLightboxMedia.querySelectorAll("video, iframe");
    extras.forEach((el) => el.remove());
  }
  if (cameraLightboxImg) {
    cameraLightboxImg.src = "";
    cameraLightboxImg.style.display = "";
  }
}

function closeCameraLightbox() {
  _teardownLightboxMedia();
  cameraLightbox.classList.add("hidden");
}

function _fillLightboxMeta(p) {
  cameraLightboxTitle.textContent = p.location || "CÂMERA";
  cameraLightboxFlag.textContent = "AO VIVO";
  if (cameraLightboxSource) {
    cameraLightboxSource.textContent = String(p.source || p.wtxSource || "—").toUpperCase();
  }
  if (cameraLightboxStatus) {
    const st = p.status || "ONLINE";
    const refresh = typeof p.refresh_s === "number"
      ? (p.refresh_s < 60 ? `~${p.refresh_s}s` : `~${Math.round(p.refresh_s / 60)} min`)
      : "";
    cameraLightboxStatus.textContent = refresh ? `${st} · ${refresh}` : String(st);
  }
}

async function _playHlsInLightbox(playlistUrl) {
  cameraLightboxImg.style.display = "none";
  const video = document.createElement("video");
  video.controls = true;
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;
  video.style.objectFit = "contain";
  cameraLightboxMedia.appendChild(video);

  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = playlistUrl;
    return;
  }
  const Hls = await _loadHlsLib();
  if (!Hls || !Hls.isSupported()) {
    video.replaceWith(Object.assign(document.createElement("div"), {
      className: "info-video",
      textContent: "◉ NAVEGADOR SEM SUPORTE A HLS",
      style: "height:200px;display:flex;align-items:center;justify-content:center;",
    }));
    return;
  }
  const hls = new Hls({ lowLatencyMode: true });
  _lightboxHls = hls;
  hls.loadSource(playlistUrl);
  hls.attachMedia(video);
}

function openCameraLightbox(p, streamInfo) {
  _teardownLightboxMedia();
  _fillLightboxMeta(p);
  cameraLightbox.classList.remove("hidden");

  const info = streamInfo || _lastStreamInfo;

  // Stream YouTube
  if (info && info.type === "youtube" && info.video_id) {
    cameraLightboxImg.style.display = "none";
    const iframe = document.createElement("iframe");
    iframe.src = `https://www.youtube-nocookie.com/embed/${info.video_id}?autoplay=1&mute=1&playsinline=1`;
    iframe.title = "Transmissão ao vivo";
    iframe.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
    iframe.allowFullscreen = true;
    cameraLightboxMedia.appendChild(iframe);
    return;
  }

  // Stream HLS
  if (info && info.type === "hls" && info.playlist_url) {
    _playHlsInLightbox(info.playlist_url).catch(() => {
      if (p.image_url) {
        cameraLightboxImg.style.display = "";
        cameraLightboxImg.src = `${p.image_url}?t=${Date.now()}`;
      }
    });
    return;
  }

  // Snapshot (comportamento original)
  if (p.image_url) {
    cameraLightboxImg.style.display = "";
    cameraLightboxImg.src = `${p.image_url}?t=${Date.now()}`;
    _cameraLightboxRefreshTimer = setInterval(() => {
      if (!cameraLightbox.isConnected || cameraLightbox.classList.contains("hidden")) {
        clearInterval(_cameraLightboxRefreshTimer);
        _cameraLightboxRefreshTimer = null;
        return;
      }
      cameraLightboxImg.src = `${p.image_url}?t=${Date.now()}`;
    }, 5000);
    return;
  }

  cameraLightboxImg.style.display = "none";
  const note = document.createElement("div");
  note.className = "info-video";
  note.style.cssText = "height:200px;display:flex;align-items:center;justify-content:center;";
  note.textContent = "◉ SEM FEED PARA AMPLIAR";
  cameraLightboxMedia.appendChild(note);
}

cameraLightboxClose.addEventListener("click", closeCameraLightbox);
cameraLightbox.addEventListener("click", (e) => {
  if (e.target === cameraLightbox) closeCameraLightbox();
});

if (cameraLightboxFullscreen) {
  cameraLightboxFullscreen.addEventListener("click", () => {
    const media = cameraLightboxMedia;
    const target =
      media.querySelector("video") ||
      media.querySelector("iframe") ||
      cameraLightboxImg;
    if (!target) return;
    const el = target.requestFullscreen
      ? target
      : media.requestFullscreen
        ? media
        : null;
    if (el && el.requestFullscreen) {
      el.requestFullscreen().catch(() => {
        // fallback: tela cheia do container inteiro do lightbox
        if (cameraLightbox.requestFullscreen) cameraLightbox.requestFullscreen();
      });
    } else if (cameraLightbox.requestFullscreen) {
      cameraLightbox.requestFullscreen();
    }
  });
}

function paintCameraInfo(p) {
  if (_cameraRefreshTimer) {
    clearInterval(_cameraRefreshTimer);
    _cameraRefreshTimer = null;
  }
  _teardownStream(); // troca de camera (ou mesma camera reaberta) derruba o player anterior
  _lastStreamInfo = null;

  infoTitle.textContent = "CÂMERA PÚBLICA";
  // BUGFIX: "sem imagem" e "demo" eram tratados como o mesmo caso (o
  // else generico), entao uma camera REAL (ex.: OpenCCTV, ONLINE) cujo
  // feed nao e uma imagem proxyavel (HLS/iframe) aparecia rotulada
  // como "SIMULAÇÃO / DADOS DEMO" - o que e falso, ela existe de
  // verdade. Agora "demo" so dispara quando a fonte e literalmente
  // "demo" (fallback que so roda se TODAS as fontes reais falharem, e
  // que nem planta mais cameras fake no mapa - ver camera_service.py).
  const isDemo = p.wtxSource === "demo";
  // BUGFIX: cameras do SkylineWebcams que sao canais do YouTube ja
  // chegam com "image_url" preenchido (thumbnail estatica do YouTube,
  // usada so' como preview no marker). Antes isso fazia isLive=true e
  // o painel mostrava so' essa mesma thumbnail parada pra sempre
  // (recarregada a cada 5s sem nunca mudar) - a transmissao de
  // verdade nunca tocava. Camera do YouTube tem que sempre ir pro
  // player embutido (ver loadCameraStream), nunca pro modo "imagem".
  //
  // Mesma lógica para EarthCam: tem snapshot estático (image.php), mas
  // a página expõe HLS ao vivo — preferimos o vídeo real no painel.
  const isYoutube = !!p.page_url && /(?:youtube\.com|youtu\.be)/i.test(p.page_url);
  const isEarthCam = !!p.page_url && /earthcam\.com/i.test(p.page_url);
  const isWebcamera24 = !!p.page_url && /webcamera24\.com/i.test(p.page_url) && !p.page_url_generic;
  const hasStreamHint = !!(p.stream_hint && (p.stream_hint.type === "hls" || p.stream_hint.type === "youtube"));
  const isOcvStream = String(p.id || "").startsWith("ocv-") && (
    hasStreamHint || ["m3u8", "iframe", "mp4"].includes(String(p.feed_type || "").toLowerCase())
  );
  // MJPEG/image do OpenCCTV continua no caminho de imagem; HLS/YT preferem player.
  const prefersStream = isYoutube || isEarthCam || isWebcamera24 || hasStreamHint || isOcvStream;
  const isLive = !isDemo && !prefersStream && !!p.wtxSource && !!p.image_url;
  // BUGFIX: quando nao' da pra' tocar a camera aqui dentro, o unico
  // jeito de ve-la e' abrindo a pagina da fonte - antes, se essa
  // camera nao tinha uma pagina PROPRIA (so' um "melhor esforço" tipo
  // o mapa geral do Webcamera24), o link nem aparecia, e o usuario
  // ficava sem nenhum jeito de ver a camera. Agora sempre aparece
  // algum link quando ha' page_url, e o texto avisa quando e' so' o
  // mapa geral da fonte (nao a camera especifica).
  const pageLink = p.page_url
    ? `<div class="info-row" style="margin-top:8px;"><span>Transmissão</span><span><a href="${p.page_url}" target="_blank" rel="noopener noreferrer" style="color:#7dd3fc;text-decoration:underline;">${p.page_url_generic ? "Abrir mapa da fonte ↗" : "Abrir ao vivo ↗"}</a></span></div>`
    : "";
  const media = isLive
    ? `<div class="info-video-wrap"><img class="info-video" id="cameraImg" style="object-fit:cover;" src="${p.image_url}?t=${Date.now()}" alt="Imagem ao vivo da camera" onerror="this.onerror=null;this.replaceWith(Object.assign(document.createElement('div'),{className:'info-video',textContent:'◉ IMAGEM INDISPONÍVEL NO MOMENTO'}));" /><button class="info-video-expand" id="cameraExpandBtn" title="Ver ampliado">⤢ AMPLIAR</button></div>${pageLink}`
    : p.lite
      ? '<div class="info-video">◉ Carregando feed OpenCCTV…</div>'
      : isDemo
        ? '<div class="info-video">◉ TRANSMISSÃO SIMULADA — SEM SINAL</div>'
        // Antes isso ja' terminava aqui, so' com o link "abrir no site".
        // Agora e' um placeholder: renderCameraInfo() chama
        // loadCameraStream() logo em seguida pra' tentar tocar o video
        // DE VERDADE (YouTube embed ou HLS) dentro do proprio painel -
        // o link de abrir no site vira so' o fallback final, se nao
        // rolar transmissao incorporavel.
        : `<div class="info-video-wrap" id="cameraStreamBox" data-cam-id="${p.id}">
             <div class="info-video">◉ Procurando transmissão ao vivo…</div>
           </div>${pageLink}`;
  const flag = isLive
    ? `<div class="info-flag live">AO VIVO · ${String(p.source || p.wtxSource).toUpperCase()}</div>`
    : p.lite
      ? '<div class="info-flag live">OPENCCTV · RESOLVENDO DETALHE</div>'
      : isDemo
        ? '<div class="info-flag">SIMULAÇÃO / DADOS DEMO</div>'
        : `<div class="info-flag live">${String(p.source || p.wtxSource || "").toUpperCase()}${
            hasStreamHint || prefersStream ? " · STREAM" : (p.page_url ? " · LINK AO VIVO" : " · SEM FEED DE IMAGEM")
          }</div>`;

  // Linha do semaforo: cor + intervalo de atualizacao. Diz claramente
  // se o numero foi CRONOMETRADO nesta camera ou se e' a cadencia
  // tipica estimada da fonte (prefixo "~").
  const STATUS_LABEL = {
    verde: ["ATUALIZAÇÃO RÁPIDA", "#4ade80"],
    amarelo: ["ATUALIZAÇÃO MÉDIA", "#facc15"],
    vermelho: ["ATUALIZAÇÃO LENTA", "#f87171"],
    cinza: ["DESLIGADA / SEM IMAGEM", "#7c828c"],
  };
  const st = STATUS_LABEL[p.status_color] || STATUS_LABEL.cinza;
  const segundos = typeof p.refresh_s === "number" ? p.refresh_s : null;
  const tempo =
    segundos === null
      ? "—"
      : segundos < 60
        ? `${segundos}s`
        : `${Math.round(segundos / 60)} min`;
  const medido = p.refresh_measured ? "medido" : "estimado pela fonte";
  // Quando esta' cinza, dizer POR QUE. "sem imagem indexada" e' falha
  // do nosso parser daquela fonte, nao camera fora do ar - sao coisas
  // diferentes e o usuario precisa conseguir separar.
  const MOTIVO = {
    fonte_offline: "Cinza: a própria fonte marcou esta câmera como fora do ar.",
    proxy_falhou: "Cinza: as últimas tentativas de buscar a imagem falharam.",
    sem_imagem_indexada:
      "Cinza: a fonte não expôs nem imagem nem página para esta câmera.",
  };
  const motivo = p.status_reason && MOTIVO[p.status_reason]
    ? `<div class="info-note">${MOTIVO[p.status_reason]}</div>`
    : "";
  const statusRow =
    `<div class="info-row"><span>Atualização</span><span>` +
    `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${st[1]};box-shadow:0 0 6px ${st[1]};margin-right:6px;"></span>` +
    `${st[0]} · ${p.refresh_measured ? "" : "~"}${tempo} (${medido})</span></div>`;

  infoBody.innerHTML =
    row("Local", p.location || "—") +
    row("Status", p.status === "ONLINE" ? "ONLINE" : (p.status || "—")) +
    statusRow +
    row("Fonte", p.source || "—") +
    motivo +
    media +
    flag;

  if (isLive) {
    _cameraRefreshTimer = setInterval(() => {
      const img = document.getElementById("cameraImg");
      if (!img || !img.isConnected) {
        clearInterval(_cameraRefreshTimer);
        _cameraRefreshTimer = null;
        return;
      }
      img.src = `${p.image_url}?t=${Date.now()}`;
    }, 5000);

    const expandBtn = document.getElementById("cameraExpandBtn");
    if (expandBtn) {
      expandBtn.addEventListener("click", () => openCameraLightbox(p));
    }
  }
}

// ---------- Transmissao ao vivo da camera (YouTube / HLS) ----------
//
// Antes, camera "somente pagina" (sem snapshot proprio - a maioria do
// SkylineWebcams) so' oferecia um link "abrir no site de origem". Isso
// pede /api/cameras/stream/<id> (ver camera_service.get_camera_stream_info
// pro limite honesto de quando a extracao NAO acha nada) e toca o video
// de verdade aqui dentro quando da'.
let _activeHls = null;
let _hlsLibPromise = null;

function _teardownStream() {
  if (_activeHls) {
    try { _activeHls.destroy(); } catch (err) { /* noop */ }
    _activeHls = null;
  }
}
window.WTX._teardownCameraStream = _teardownStream;

function _loadHlsLib() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (_hlsLibPromise) return _hlsLibPromise;
  _hlsLibPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.15/hls.min.js";
    script.async = true;
    script.onload = () => resolve(window.Hls);
    script.onerror = () => reject(new Error("falha ao carregar hls.js"));
    document.head.appendChild(script);
  });
  return _hlsLibPromise;
}

function _streamFallbackHtml(p, note) {
  // BUGFIX: o link "Transmissão / Abrir ao vivo" já é adicionado uma
  // vez em paintCameraInfo() (fora do #cameraStreamBox, então
  // sobrevive à troca de conteúdo feita aqui). Repeti-lo aqui dentro
  // duplicava a linha "Transmissão" na caixa de informações.
  return `<div class="info-video">◉ ${note}</div>`;
}

async function loadCameraStream(p) {
  _teardownStream();
  const box = document.getElementById("cameraStreamBox");
  if (!box || box.dataset.camId !== String(p.id)) return; // painel ja' trocou de camera

  // OpenCCTV (e outros) podem ja' trazer stream_hint resolvido no detail
  // (HLS direto / YouTube) — evita scrape de page_url e hosts fora da allowlist.
  let info = null;
  if (p.stream_hint && (p.stream_hint.type === "hls" || p.stream_hint.type === "youtube")) {
    info = p.stream_hint;
  } else if (p.page_url) {
    try {
      const url = `/api/cameras/stream/${encodeURIComponent(p.id)}?page_url=${encodeURIComponent(p.page_url)}`;
      const res = await fetch(url);
      info = await res.json();
    } catch (err) {
      info = { type: "none" };
    }
  } else {
    info = { type: "none" };
  }

  // Painel pode ter mudado de camera enquanto o fetch estava em voo.
  const stillBox = document.getElementById("cameraStreamBox");
  if (!stillBox || stillBox.dataset.camId !== String(p.id)) return;

  _lastStreamInfo = info && (info.type === "youtube" || info.type === "hls") ? info : null;

  function _attachExpandBtn(wrap, streamInfo) {
    const btn = document.createElement("button");
    btn.className = "info-video-expand";
    btn.id = "cameraExpandBtn";
    btn.title = "Ver ampliado";
    btn.textContent = "⤢ AMPLIAR";
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openCameraLightbox(p, streamInfo || _lastStreamInfo);
    });
    wrap.appendChild(btn);
  }

  if (info.type === "youtube") {
    const wrap = document.createElement("div");
    wrap.className = "info-video-wrap";
    // Mini player (painel lateral): controls=0 tira a barra de reproducao/
    // pause que o YouTube mostra no hover, e a classe "info-video--mini"
    // (ver style.css) bloqueia clique/hover no iframe - a unica forma de
    // interagir com esse video e pelo botao AMPLIAR, que abre o lightbox
    // com o player completo (esse sim com controles, ver openCameraLightbox).
    wrap.innerHTML =
      `<iframe class="info-video info-video--mini" src="https://www.youtube-nocookie.com/embed/${info.video_id}?autoplay=1&mute=1&playsinline=1&controls=0&disablekb=1&modestbranding=1&rel=0" ` +
      `title="Transmissão ao vivo" frameborder="0" allow="autoplay; encrypted-media; picture-in-picture"></iframe>`;
    _attachExpandBtn(wrap, info);
    stillBox.innerHTML = "";
    stillBox.appendChild(wrap);
    return;
  }

  if (info.type === "hls") {
    stillBox.innerHTML = '<div class="info-video">◉ Conectando à transmissão…</div>';
    try {
      const video = document.createElement("video");
      video.className = "info-video info-video--mini";
      video.style.objectFit = "cover";
      // Mini player (painel lateral): sem barra de controles/pause no
      // hover. Interacao com o video (play/pause, barra) so no lightbox
      // (AMPLIAR), que tem seu proprio <video controls> em openCameraLightbox.
      video.controls = false;
      video.autoplay = true;
      video.muted = true;
      video.playsInline = true;

      const wrap = document.createElement("div");
      wrap.className = "info-video-wrap";

      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = info.playlist_url;
        wrap.appendChild(video);
        _attachExpandBtn(wrap, info);
        stillBox.innerHTML = "";
        stillBox.appendChild(wrap);
      } else {
        const Hls = await _loadHlsLib();
        if (!Hls || !Hls.isSupported()) {
          stillBox.innerHTML = _streamFallbackHtml(p, "NAVEGADOR SEM SUPORTE A HLS");
          return;
        }
        const hls = new Hls({ lowLatencyMode: true });
        _activeHls = hls;
        hls.on(Hls.Events.ERROR, (_evt, data) => {
          if (data && data.fatal) {
            const stillHere = document.getElementById("cameraStreamBox");
            if (stillHere && stillHere.dataset.camId === String(p.id)) {
              stillHere.innerHTML = _streamFallbackHtml(p, "TRANSMISSÃO CAIU — TENTE ABRIR NO SITE");
            }
          }
        });
        hls.loadSource(info.playlist_url);
        hls.attachMedia(video);
        wrap.appendChild(video);
        _attachExpandBtn(wrap, info);
        stillBox.innerHTML = "";
        stillBox.appendChild(wrap);
      }
    } catch (err) {
      stillBox.innerHTML = _streamFallbackHtml(p, "FALHA AO CARREGAR O PLAYER DE VÍDEO");
    }
    return;
  }

  // type "none": a extracao nao achou nada incorporavel nesta fonte -
  // ver comentario em camera_service.get_camera_stream_info sobre por
  // que isso acontece (URL de stream montada via JS, nao no HTML bruto).
  _lastStreamInfo = null;
  stillBox.innerHTML = _streamFallbackHtml(p, "SEM TRANSMISSÃO INCORPORÁVEL AQUI");
}

function _shouldLoadStream(cam) {
  if (!cam || cam.wtxSource === "demo" || cam.lite) return false;
  if (cam.stream_hint && (cam.stream_hint.type === "hls" || cam.stream_hint.type === "youtube")) return true;
  const isYoutube = !!cam.page_url && /(?:youtube\.com|youtu\.be)/i.test(cam.page_url);
  const isEarthCam = !!cam.page_url && /earthcam\.com/i.test(cam.page_url);
  const isWebcamera24 = !!cam.page_url && /webcamera24\.com/i.test(cam.page_url) && !cam.page_url_generic;
  if (isYoutube || isEarthCam || isWebcamera24) return true;
  const ft = String(cam.feed_type || "").toLowerCase();
  if (String(cam.id || "").startsWith("ocv-") && ["m3u8", "iframe", "mp4"].includes(ft)) return true;
  // Snapshot puro (image/mjpeg) nao precisa de stream player
  if (cam.image_url && !isYoutube && !isEarthCam && !isWebcamera24) return false;
  return !!(cam.page_url && !cam.page_url_generic);
}

async function renderCameraInfo(p) {
  paintCameraInfo(p);

  // Camera sem imagem propria (ou canal do YouTube / EarthCam / OpenCCTV
  // HLS-YT) : tenta transmissao de verdade no painel. Ver loadCameraStream().
  if (_shouldLoadStream(p)) {
    loadCameraStream(p);
  }

  // OpenCCTV lite: busca nome/cidade/feed no clique.
  if (p && (p.lite || String(p.id || "").startsWith("ocv-")) && window.WTX.resolveCameraDetail) {
    const full = await window.WTX.resolveCameraDetail(p);
    if (full && full._unavailable) {
      // Camera confirmada sem feed tocavel: ja removida do mapa.
      infoBody.innerHTML =
        row("Local", full.location || "—") +
        '<div class="info-video">◉ CÂMERA SEM FEED TOCÁVEL — REMOVIDA DO MAPA</div>';
      setTimeout(() => infoPanel.classList.add("hidden"), 1800);
      return;
    }
    if (full && full !== p) {
      const enriched = { ...full, wtxType: "camera", wtxSource: full.wtxSource || p.wtxSource || "opencctv_live" };
      paintCameraInfo(enriched);
      if (_shouldLoadStream(enriched)) {
        loadCameraStream(enriched);
      }
    }
  }
}


function renderShipInfo(p) {
  infoTitle.textContent = "NAVIO";
  const isLive = p.wtxSource && p.wtxSource !== "demo";
  const flag = isLive
    ? `<div class="info-flag live">AO VIVO · ${String(p.wtxSource).toUpperCase()}</div>`
    : '<div class="info-flag">SIMULAÇÃO / DADOS DEMO</div>';
  infoBody.innerHTML =
    row("Nome", p.name || "—") +
    row("MMSI", p.mmsi || "—") +
    row("Tipo", p.ship_type || "—") +
    row("Velocidade", `${p.speed_kmh ?? "—"} km/h (${p.speed_kn ?? "—"} kn)`) +
    row("Curso", `${Math.round(p.course || p.heading || 0)}°`) +
    row("Destino", p.destination || "—") +
    row("IMO", p.imo || "—") +
    row("Callsign", p.callsign || "—") +
    row("Bandeira", p.flag || "—") +
    row("Região fonte", p.source_region || "—") +
    flag;
}

const FUEL_LABELS_PT = {
  hydro: "Hidrelétrica",
  nuclear: "Nuclear",
  coal: "Carvão",
  solar: "Solar",
  wind: "Eólica",
};

function renderPowerPlantInfo(p) {
  infoTitle.textContent = "USINA DE ENERGIA";
  const isLive = p.wtxSource && p.wtxSource !== "demo";
  const flag = isLive
    ? `<div class="info-flag live">DADOS REAIS · GLOBAL POWER PLANT DATABASE (WRI)</div>`
    : '<div class="info-flag">SIMULAÇÃO / DADOS DEMO</div>';
  infoBody.innerHTML =
    row("Nome", p.name || "—") +
    row("Tipo", FUEL_LABELS_PT[p.fuel] || p.fuel || "—") +
    row("Capacidade", p.capacity_mw != null ? `${p.capacity_mw.toLocaleString("pt-BR")} MW` : "—") +
    row("País", p.country_long || p.country || "—") +
    row("Em operação desde", p.year || "—") +
    row("Proprietário", p.owner || "—") +
    flag;
}

// INFRAFISICA.md, Bloco 1 — cabos submarinos. O GeoJSON público da
// TeleGeography (cable-geo.json) só traz id/nome/cor/rota — dono,
// capacidade e ano de entrada em operação (RFS) existem na API deles,
// mas por cabo individual, não no arquivo agregado; por isso o popup
// aqui é mais magro que o de usinas (ver infra_service.py, Bloco 1).
function renderCableInfo(p) {
  infoTitle.textContent = "CABO SUBMARINO";
  const isLive = p.wtxSource && p.wtxSource !== "demo";
  const flag = isLive
    ? '<div class="info-flag live">DADOS REAIS · TELEGEOGRAPHY SUBMARINE CABLE MAP</div>'
    : '<div class="info-flag">CATÁLOGO DEMO (subconjunto real, TeleGeography)</div>';
  infoBody.innerHTML =
    row("Nome", p.name || "—") +
    row("Fonte", "TeleGeography Submarine Cable Map (uso não comercial)") +
    flag;
}

function renderCableLandingPointInfo(p) {
  infoTitle.textContent = "PONTO DE ATERRISSAGEM";
  const isLive = p.wtxSource && p.wtxSource !== "demo";
  const flag = isLive
    ? '<div class="info-flag live">DADOS REAIS · TELEGEOGRAPHY SUBMARINE CABLE MAP</div>'
    : '<div class="info-flag">CATÁLOGO DEMO (subconjunto real, TeleGeography)</div>';
  infoBody.innerHTML =
    row("Local", p.name || "—") +
    row("Fonte", "TeleGeography Submarine Cable Map (uso não comercial)") +
    flag;
}

// INFRAFISICA.md, Bloco 3 — datacenters. Duas fontes bem diferentes
// pra' distinguir no popup: OSM (ponto real, mapeado por alguém) vs. a
// lista curada de fallback (local aproximado, nível de cidade/região
// — ver `approx` em infra_service.py/_DEMO_CURATED_DATACENTERS).
function renderDatacenterInfo(p) {
  infoTitle.textContent = "DATACENTER";
  const isLive = p.wtxSource === "openstreetmap_overpass";
  let flag;
  if (isLive) {
    flag = '<div class="info-flag live">DADOS REAIS · OPENSTREETMAP (OVERPASS)</div>';
  } else if (p.approx) {
    flag = '<div class="info-flag">LISTA CURADA · LOCAL APROXIMADO (CIDADE/REGIÃO)</div>';
  } else {
    flag = '<div class="info-flag">CATÁLOGO DEMO</div>';
  }
  infoBody.innerHTML =
    row("Nome", p.name || "—") +
    row("Operador", p.operator || "—") +
    row("Fonte", isLive ? "OpenStreetMap (Overpass API)" : "Lista curada (megacampi divulgados publicamente)") +
    flag;
}

const RISK_LABEL = { baixo: "BAIXO", medio: "MÉDIO", alto: "ALTO", critico: "CRÍTICO" };

function renderComputerInfo(p) {
  infoTitle.textContent = "HOST DE REDE";
  const isLive = p.wtxSource === "scan_ativo";
  const flag = isLive
    ? '<div class="info-flag live">SCAN ATIVO · PORTAS + BANNER + GEOIP REAIS</div>'
    : '<div class="info-flag">FALHA NO SCAN — SEM DADOS AO VIVO</div>';

  const ports = p.open_ports || [];
  const portsHtml = ports.length
    ? ports
        .map((pt) => {
          const adv = (pt.advisories || [])
            .map((a) => `<div class="info-cve">⚠ ${a.id} — ${a.description} (${a.severity})</div>`)
            .join("");
          const banner = pt.banner || pt.server_header || pt.software || "";
          return (
            `<div class="info-port-row">` +
            `<span class="info-port-badge risk-${pt.risk}">${pt.emoji || ""} ${pt.port}/${pt.service}</span>` +
            (banner ? `<div class="info-port-banner">${String(banner).slice(0, 140)}</div>` : "") +
            adv +
            `</div>`
          );
        })
        .join("")
    : '<div class="info-row"><span>Portas</span><span>nenhuma porta monitorada aberta</span></div>';

  infoBody.innerHTML =
    row("Host", p.label || p.ip) +
    row("IP", p.ip) +
    row("Local", [p.city, p.country].filter(Boolean).join(", ") || "—") +
    row("Organização/ISP", p.org || p.isp || "—") +
    row("ASN", p.asn || "—") +
    row("Risco geral", `<span class="risk-pill risk-${p.risk_level}">${RISK_LABEL[p.risk_level] || "—"}</span>`) +
    row("Última varredura", p.scanned_at ? new Date(p.scanned_at).toLocaleString("pt-BR") : "—") +
    row("Nº de varreduras (histórico local)", p.scan_count ?? "—") +
    `<div class="info-ports-title">PORTAS ABERTAS</div>` +
    portsHtml +
    flag;
}

function unwrapEntityProps(entity) {
  // Cesium wraps plain objects assigned to entity.properties in a
  // PropertyBag; each field becomes a ConstantProperty. Reading
  // p.wtxType directly yields an object, not the string — so clicks
  // on cameras (and sometimes other layers) never open the panel.
  // Prefer the side-channel we attach as entity.wtxData when present.
  if (entity.wtxData && entity.wtxData.wtxType) {
    return entity.wtxData;
  }
  const p = entity.properties;
  if (!p) return null;
  const raw = {};
  const names = p.propertyNames || [];
  if (names.length) {
    for (const name of names) {
      const v = p[name];
      raw[name] = v && typeof v.getValue === "function" ? v.getValue(Cesium.JulianDate.now()) : v;
    }
  } else {
    // fallback: iterate own keys
    for (const name of Object.keys(p)) {
      if (name === "propertyNames" || name === "definitionChanged") continue;
      const v = p[name];
      raw[name] = v && typeof v.getValue === "function" ? v.getValue(Cesium.JulianDate.now()) : v;
    }
  }
  return raw.wtxType ? raw : null;
}

function showInfoForEntity(entity) {
  const p = unwrapEntityProps(entity);
  if (!p || !p.wtxType) return;

  // Trocou de objeto: derruba qualquer player HLS que estivesse tocando
  // pra' camera anterior (senao o audio/video continua rodando fora de
  // tela e vaza conexao).
  if (p.wtxType !== "camera" && window.WTX._teardownCameraStream) {
    window.WTX._teardownCameraStream();
  }

  if (p.wtxType === "aircraft") renderAircraftInfo(p);
  else if (p.wtxType === "satellite") renderSatelliteInfo(p);
  else if (p.wtxType === "camera") renderCameraInfo(p);
  else if (p.wtxType === "ship") renderShipInfo(p);
  else if (p.wtxType === "power_plant") renderPowerPlantInfo(p);
  else if (p.wtxType === "submarine_cable") renderCableInfo(p);
  else if (p.wtxType === "cable_landing_point") renderCableLandingPointInfo(p);
  else if (p.wtxType === "datacenter") renderDatacenterInfo(p);
  else if (p.wtxType === "computer") renderComputerInfo(p);
  else return;

  infoPanel.classList.remove("hidden");

  const trackBtn = document.getElementById("trackBtn");
  if (trackBtn) {
    trackBtn.addEventListener("click", () => {
      window.WTX.viewer.trackedEntity = entity;
    });
  }

  // Street View: se o objeto tem lat/lon, oferecece botão no painel
  const lat = p.lat != null ? p.lat : p.latitude;
  const lon = p.lon != null ? p.lon : p.longitude;
  if (Number.isFinite(Number(lat)) && Number.isFinite(Number(lon))) {
    const body = document.getElementById("infoBody");
    if (body && !document.getElementById("streetViewBtn")) {
      body.insertAdjacentHTML(
        "beforeend",
        streetViewButtonHtml(lat, lon, p.location || p.name || p.callsign || "Local")
      );
      bindStreetViewButton();
    }
  }
}


// ---------- Google Street View ----------
//
// Abre panorama no lightbox. Sem chave Google: usa o embed clássico
// (output=svembed). Com window.WTX_GOOGLE_MAPS_KEY: Embed API oficial.
let _svPickMode = false;

function _streetViewEmbedUrl(lat, lon) {
  const key = window.WTX_GOOGLE_MAPS_KEY || "";
  if (key) {
    return (
      "https://www.google.com/maps/embed/v1/streetview" +
      `?key=${encodeURIComponent(key)}` +
      `&location=${lat},${lon}` +
      "&heading=0&pitch=0&fov=90"
    );
  }
  // Embed sem chave (Street View clássico). Pode não ter cobertura em
  // todos os pontos — o usuário ainda tem o link "Abrir no Google Maps".
  return (
    "https://maps.google.com/maps?q=&layer=c" +
    `&cbll=${lat},${lon}` +
    "&cbp=11,0,0,0,0&hl=pt-BR&output=svembed"
  );
}

function _streetViewMapsUrl(lat, lon) {
  return `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat},${lon}`;
}

function closeStreetView() {
  const box = document.getElementById("streetViewLightbox");
  const frame = document.getElementById("streetViewFrame");
  if (frame) frame.src = "about:blank";
  if (box) box.classList.add("hidden");
}

window.WTX.openStreetView = openStreetView;
function openStreetView(lat, lon, label) {
  lat = Number(lat);
  lon = Number(lon);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;

  const box = document.getElementById("streetViewLightbox");
  const frame = document.getElementById("streetViewFrame");
  const title = document.getElementById("streetViewTitle");
  const coords = document.getElementById("streetViewCoords");
  const openMaps = document.getElementById("streetViewOpenMaps");
  if (!box || !frame) return;

  if (title) title.textContent = label ? String(label).slice(0, 60) : "STREET VIEW";
  if (coords) coords.textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  if (openMaps) {
    openMaps.href = _streetViewMapsUrl(lat, lon);
  }
  frame.src = _streetViewEmbedUrl(lat, lon);
  box.classList.remove("hidden");
}

function streetViewButtonHtml(lat, lon, label) {
  if (!Number.isFinite(Number(lat)) || !Number.isFinite(Number(lon))) return "";
  const safeLabel = String(label || "Street View").replace(/"/g, "&quot;");
  return (
    `<button type="button" class="info-sv-btn" id="streetViewBtn" ` +
    `data-lat="${lat}" data-lon="${lon}" data-label="${safeLabel}">` +
    `◎ STREET VIEW NESTE PONTO</button>`
  );
}

function bindStreetViewButton() {
  const btn = document.getElementById("streetViewBtn");
  if (!btn) return;
  btn.addEventListener("click", () => {
    openStreetView(btn.dataset.lat, btn.dataset.lon, btn.dataset.label);
  });
}

(function initStreetViewUi() {
  const closeBtn = document.getElementById("streetViewClose");
  const box = document.getElementById("streetViewLightbox");
  const fsBtn = document.getElementById("streetViewFullscreen");
  const pickBtn = document.getElementById("ctrl-streetview");

  if (closeBtn) closeBtn.addEventListener("click", closeStreetView);
  if (box) {
    box.addEventListener("click", (e) => {
      if (e.target === box) closeStreetView();
    });
  }
  if (fsBtn) {
    fsBtn.addEventListener("click", () => {
      const media = document.getElementById("streetViewMedia");
      const frame = document.getElementById("streetViewFrame");
      const target = frame || media || box;
      if (target && target.requestFullscreen) target.requestFullscreen().catch(() => {});
    });
  }
  if (pickBtn) {
    pickBtn.addEventListener("click", () => {
      _svPickMode = !_svPickMode;
      pickBtn.classList.toggle("active", _svPickMode);
      const canvas = window.WTX.viewer && window.WTX.viewer.scene.canvas;
      if (canvas) {
        canvas.style.cursor = _svPickMode ? "crosshair" : "";
      }
      const root = document.querySelector(".cesium-viewer");
      if (root) root.classList.toggle("sv-pick-mode", _svPickMode);
    });
  }
})();

const handler = new Cesium.ScreenSpaceEventHandler(window.WTX.viewer.scene.canvas);
handler.setInputAction((click) => {
  // Modo Street View: clique no terreno abre o panorama naquele ponto
  if (_svPickMode) {
    const scene = window.WTX.viewer.scene;
    let cartesian = scene.pickPosition(click.position);
    if (!Cesium.defined(cartesian)) {
      const ray = window.WTX.viewer.camera.getPickRay(click.position);
      cartesian = scene.globe.pick(ray, scene);
    }
    if (Cesium.defined(cartesian)) {
      const carto = Cesium.Cartographic.fromCartesian(cartesian);
      const lat = Cesium.Math.toDegrees(carto.latitude);
      const lon = Cesium.Math.toDegrees(carto.longitude);
      openStreetView(lat, lon, "Street View");
    }
    return;
  }
  const picked = window.WTX.viewer.scene.pick(click.position);
  if (Cesium.defined(picked) && picked.id && picked.id.properties) {
    showInfoForEntity(picked.id);
  }
}, Cesium.ScreenSpaceEventType.LEFT_CLICK);

// ---------- Busca (cidades, países, aviões, satélites e navios) ----------
//
// BUGFIX: antes so' buscava numa lista fixa de 9 cidades hardcoded, sem
// nenhum pais e sem nenhum objeto ao vivo (aviao/satelite/navio) - por
// mais que o app já tivesse tudo isso no globo, não dava pra' achar
// pelo nome. Agora a busca junta:
//   - países e cidades  -> boundariesLayer.searchPlaces() (nomes do
//     Natural Earth, pré-carregados em segundo plano desde o boot -
//     ver preloadNamesForSearch() em boundaries.js - sem precisar
//     ligar a camada de DIVISÕES);
//   - aviões / satélites / navios -> o .search() de cada camada, sobre
//     o que já foi carregado da API. Se a camada nunca foi ligada,
//     ensureLoaded() dispara a busca dos dados em segundo plano (sem
//     acender nada no globo) na primeira letra digitada, e o resultado
//     aparece assim que a resposta chega.

const searchInput = document.getElementById("searchInput");
const searchResults = document.getElementById("searchResults");

const MAX_RESULTS_PER_KIND = 6;
let _searchBgTriggered = false;
let _searchRetryTimer = null;

const KIND_LABEL = {
  country: "PAÍS",
  city: "CIDADE",
  airport: "AEROPORTO",
  aircraft: "AVIÃO",
  satellite: "SATÉLITE",
  ship: "NAVIO",
};

function runSearch(q) {
  const places = (window.WTX.boundariesLayer && window.WTX.boundariesLayer.searchPlaces(q)) || [];
  const airports = (window.WTX.airportLayer && window.WTX.airportLayer.search(q)) || [];
  const aircraft = (window.WTX.aircraftLayer && window.WTX.aircraftLayer.search(q)) || [];
  const satellites = (window.WTX.satelliteLayer && window.WTX.satelliteLayer.search(q)) || [];
  const ships = (window.WTX.shipLayer && window.WTX.shipLayer.search(q)) || [];

  const results = [];
  places.forEach((p) => results.push({ kind: p.type, label: p.name, lon: p.lon, lat: p.lat }));
  airports.slice(0, MAX_RESULTS_PER_KIND).forEach((a) => {
    const bits = [a.iata, [a.city, a.country].filter(Boolean).join(", ")].filter(Boolean);
    const label = bits.length ? `${a.name} (${bits.join(" · ")})` : a.name;
    results.push({ kind: "airport", label, lon: a.lon, lat: a.lat });
  });
  aircraft
    .slice(0, MAX_RESULTS_PER_KIND)
    .forEach((a) => results.push({ kind: "aircraft", label: a.label, lon: a.lon, lat: a.lat, entity: a.entity }));
  satellites
    .slice(0, MAX_RESULTS_PER_KIND)
    .forEach((s) => results.push({ kind: "satellite", label: s.label, lon: s.lon, lat: s.lat, entity: s.entity }));
  ships
    .slice(0, MAX_RESULTS_PER_KIND)
    .forEach((s) => results.push({ kind: "ship", label: s.label, lon: s.lon, lat: s.lat, entity: s.entity }));
  return results;
}

function renderSearchResults(q) {
  const results = runSearch(q);
  if (!results.length) {
    searchResults.classList.remove("visible");
    searchResults.innerHTML = "";
    return;
  }
  searchResults.innerHTML = results
    .slice(0, 40)
    .map((r, i) => {
      const esc = String(r.label).replace(/&/g, "&amp;").replace(/</g, "&lt;");
      return (
        `<div class="search-result-item" data-idx="${i}" data-lon="${r.lon}" data-lat="${r.lat}">` +
        `<span class="search-result-kind">${KIND_LABEL[r.kind] || ""}</span> ${esc}</div>`
      );
    })
    .join("");
  searchResults.dataset.entities = "1";
  searchResults._wtxResults = results; // referencias de entidade nao cabem em data-*
  searchResults.classList.add("visible");
}

searchInput.addEventListener("input", () => {
  const q = searchInput.value.trim();
  if (!q) {
    searchResults.classList.remove("visible");
    searchResults.innerHTML = "";
    if (_searchRetryTimer) {
      clearTimeout(_searchRetryTimer);
      _searchRetryTimer = null;
    }
    return;
  }

  // Primeira letra digitada: dispara o carregamento em segundo plano
  // das camadas ao vivo (sem acender nada no globo) e tenta de novo
  // em instantes, ja' que a 1a resposta da API pode nao ter chegado.
  if (!_searchBgTriggered) {
    _searchBgTriggered = true;
    if (window.WTX.aircraftLayer) window.WTX.aircraftLayer.ensureLoaded();
    if (window.WTX.satelliteLayer) window.WTX.satelliteLayer.ensureLoaded();
    if (window.WTX.shipLayer) window.WTX.shipLayer.ensureLoaded();
  }

  renderSearchResults(q);

  if (_searchRetryTimer) clearTimeout(_searchRetryTimer);
  _searchRetryTimer = setTimeout(() => {
    if (searchInput.value.trim() === q) renderSearchResults(q);
  }, 1500);
});

searchResults.addEventListener("click", (e) => {
  const item = e.target.closest(".search-result-item");
  if (!item) return;
  const idx = Number(item.dataset.idx);
  const r = (searchResults._wtxResults || [])[idx];
  const lon = parseFloat(item.dataset.lon);
  const lat = parseFloat(item.dataset.lat);
  if (Number.isFinite(lon) && Number.isFinite(lat)) {
    const isPlace = r && (r.kind === "country" || r.kind === "city" || r.kind === "airport");
    window.WTX.flyTo(lon, lat, isPlace && r.kind === "country" ? 4000000 : 900000);
  }
  // Resultado é um objeto ao vivo (avião/satélite/navio): liga a
  // camada dele (se ainda tava desligada) e abre a prévia, como se o
  // usuário tivesse clicado nele no globo.
  if (r && r.entity) {
    const layerByKind = {
      aircraft: window.WTX.aircraftLayer,
      satellite: window.WTX.satelliteLayer,
      ship: window.WTX.shipLayer,
    };
    const layer = layerByKind[r.kind];
    if (layer) layer.setVisible(true);
    const cb = document.getElementById(`layer-${r.kind === "aircraft" ? "aircraft" : r.kind === "satellite" ? "satellites" : "ships"}`);
    if (cb) cb.checked = true;
    setTimeout(() => showInfoForEntity(r.entity), 150);
  }
  searchResults.classList.remove("visible");
  searchInput.value = "";
});

// ---------- Totais no boot (antes de ligar qualquer camada) ----------
// Os contadores mostram o TOTAL GLOBAL desde o início. A plotagem no
// Cesium continua sob demanda (viewport / camada ligada).
async function loadGlobalStats() {
  try {
    const res = await fetch("/api/stats/");
    const data = await res.json();
    if (data.aircraft) {
      const t = data.aircraft.total ?? data.aircraft.count;
      window.WTX.updateCounter("aircraft", t);
      if (typeof data.aircraft.is_live === "boolean") {
        window.WTX.updateBadge("aircraft", data.aircraft.is_live);
      }
    }
    if (data.satellites) {
      window.WTX.updateCounter("satellites", data.satellites.count);
      if (typeof data.satellites.is_live === "boolean") {
        window.WTX.updateBadge("satellites", data.satellites.is_live);
      }
    }
    if (data.ships) {
      const t = data.ships.total ?? data.ships.count;
      window.WTX.updateCounter("ships", t);
      if (typeof data.ships.is_live === "boolean") {
        window.WTX.updateBadge("ships", data.ships.is_live);
      }
    }
    if (data.cameras) {
      // Sempre o catálogo completo — nunca data.cameras.count (amostra).
      const t =
        data.cameras.global_total ||
        data.cameras.opencctv_total ||
        0;
      window.WTX.updateCounter("cameras", t);
      if (typeof data.cameras.is_live === "boolean") {
        window.WTX.updateBadge("cameras", data.cameras.is_live);
      }
    }
    if (data.computers) {
      window.WTX.updateCounter("computers", data.computers.count);
      if (typeof data.computers.is_live === "boolean") {
        window.WTX.updateBadge("computers", data.computers.is_live);
      }
    }
  } catch (err) {
    console.error("Falha ao carregar totais globais", err);
  }
}

// Dispara cedo; se o servidor ainda estiver aquecendo, tenta de novo.
loadGlobalStats();
setTimeout(loadGlobalStats, 8000);
