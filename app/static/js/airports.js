/*
 * airports.js
 * Fonte de dados de AEROPORTOS pro buscador (main.js).
 *
 * Antes o buscador só cobria países, cidades e os objetos ao vivo
 * (avião/satélite/navio) — não dava pra digitar "GRU", "Guarulhos" ou
 * "aeroporto de Confins" e achar nada. Aqui carregamos, em segundo
 * plano assim que a página abre (mesmo padrão de
 * boundariesLayer.preloadNamesForSearch), uma base pública de
 * aeroportos (mwgg/Airports) e filtramos só os que têm código IATA
 * (3 letras) — são os aeroportos "de verdade" pra passageiro, o que
 * deixa a lista bem menor (~9 mil em vez de ~28 mil pistas/aeródromos
 * pequenos) e as sugestões mais relevantes.
 *
 * Não desenha nada no globo: é só busca por texto (nome, cidade, país,
 * IATA, ICAO), igual searchPlaces() de boundaries.js.
 */

window.WTX = window.WTX || {};

(function airportSearch() {
  const AIRPORTS_URL =
    "https://raw.githubusercontent.com/mwgg/Airports/master/airports.json";

  let airports = []; // { name, city, country, iata, icao, lon, lat }
  let loaded = false;
  let loading = null; // Promise em andamento, evita disparar 2 fetches

  function validLonLat(lon, lat) {
    return (
      Number.isFinite(lon) &&
      Number.isFinite(lat) &&
      Math.abs(lon) <= 180 &&
      Math.abs(lat) <= 90 &&
      !(lon === 0 && lat === 0) // maioria dos registros quebrados cai em 0,0
    );
  }

  async function load() {
    if (loaded) return;
    if (loading) return loading;
    loading = (async () => {
      try {
        const res = await fetch(AIRPORTS_URL);
        const data = await res.json();
        const out = [];
        for (const key in data) {
          const a = data[key];
          if (!a) continue;
          const iata = String(a.iata || "").trim().toUpperCase();
          // So' aeroportos com codigo IATA valido (3 letras) - filtra
          // pistas de fazenda/heliponto/aerodromo minusculo que so'
          // teriam ICAO, e que a maioria dos usuarios nunca vai buscar.
          if (!/^[A-Z]{3}$/.test(iata)) continue;
          const lon = Number(a.lon);
          const lat = Number(a.lat);
          if (!validLonLat(lon, lat)) continue;
          out.push({
            name: String(a.name || "").trim() || iata,
            city: String(a.city || "").trim(),
            country: String(a.country || "").trim(),
            iata,
            icao: String(a.icao || "").trim().toUpperCase(),
            lon,
            lat,
          });
        }
        airports = out;
        loaded = true;
      } catch (err) {
        console.warn("Falha ao carregar base de aeroportos.", err);
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  function score(a, q) {
    // Prioriza match exato de codigo (o jeito mais comum de buscar
    // aeroporto e' digitando a sigla: "GRU", "CGH", "JFK"...).
    if (a.iata.toLowerCase() === q) return 0;
    if (a.icao.toLowerCase() === q) return 1;
    if (a.iata.toLowerCase().startsWith(q)) return 2;
    if (a.city.toLowerCase().startsWith(q)) return 3;
    if (a.name.toLowerCase().startsWith(q)) return 4;
    return 5;
  }

  window.WTX.airportLayer = {
    // Dispara o carregamento em segundo plano (chamado 1x no boot da
    // pagina, como as outras camadas fazem via ensureLoaded()).
    ensureLoaded() {
      load();
    },
    // Sincrono de proposito (igual boundariesLayer.searchPlaces): so'
    // devolve o que ja' foi carregado. Se ainda nao carregou, dispara
    // o load em segundo plano e devolve vazio - o retry de 1.5s do
    // buscador (main.js) tenta de novo e ja' acha resultado.
    search(query) {
      const q = String(query || "").trim().toLowerCase();
      if (!q) return [];
      if (!loaded) {
        load();
        return [];
      }
      const out = [];
      for (const a of airports) {
        const haystack =
          a.name.toLowerCase() +
          " " +
          a.city.toLowerCase() +
          " " +
          a.country.toLowerCase() +
          " " +
          a.iata.toLowerCase() +
          " " +
          a.icao.toLowerCase();
        if (haystack.includes(q)) out.push(a);
      }
      out.sort((x, y) => score(x, q) - score(y, q));
      return out;
    },
  };

  // Pre-carrega em segundo plano assim que o script sobe, igual as
  // outras fontes de busca - assim o buscador ja' acha aeroporto desde
  // a primeira letra digitada, sem precisar ligar camada nenhuma.
  load();
})();
