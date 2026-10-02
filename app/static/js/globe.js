/*
 * globe.js
 * Inicializacao do globo 3D (CesiumJS) e utilitarios de camera.
 * Todo o restante da aplicacao (aircraft.js, satellites.js,
 * cameras.js, main.js) consome o objeto global WTX.viewer criado aqui.
 */

window.WTX = window.WTX || {};

(function initGlobe() {
  const hasToken = !!window.WTX_CESIUM_TOKEN;

  if (hasToken) {
    Cesium.Ion.defaultAccessToken = window.WTX_CESIUM_TOKEN;
  }

  // Cria o globo SEM camada de imagem ainda. A imagem e' carregada logo
  // em seguida, de forma assincrona, com uma cadeia de fallback (ver
  // loadBestImagery abaixo). Isso evita que uma falha de rede deixe o
  // globo transparente: enquanto nada carrega, ele fica solido nesta
  // cor escura.
  const viewer = new Cesium.Viewer("cesiumContainer", {
    baseLayerPicker: false,
    geocoder: false,
    homeButton: false,
    sceneModePicker: false,
    navigationHelpButton: false,
    animation: false,
    timeline: false,
    fullscreenButton: false,
    infoBox: false,
    selectionIndicator: false,
    creditContainer: document.createElement("div"),
    baseLayer: false,
    terrainProvider: hasToken ? undefined : new Cesium.EllipsoidTerrainProvider(),
  });

  viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString("#0b0c0d");
  viewer.scene.globe.show = true;
  viewer.scene.globe.showWaterEffect = false;
  viewer.scene.globe.enableLighting = true;
  viewer.scene.globe.showGroundAtmosphere = true;
  viewer.scene.skyAtmosphere.show = true;
  viewer.scene.skyBox.show = true;
  viewer.scene.fog.enabled = true;
  viewer.scene.moon.show = true;
  viewer.scene.sun.show = true;
  viewer.scene.backgroundColor = Cesium.Color.fromCssColorString("#050505");
  viewer.scene.globe.depthTestAgainstTerrain = false;

  // ---------- Carregamento da imagem do globo (satelite real, com fallback) ----------
  //
  // Ordem de tentativa:
  //   1) Se houver token do Cesium Ion: imagem de satelite Bing Aerial
  //      via Ion (altissima resolucao).
  //   2) Sem token: Esri World Imagery (fotos de satelite/aerofoto
  //      reais, publicas, sem necessidade de chave).
  //   3) Se Esri falhar (rede/CORS): tiles do OpenStreetMap (mapa
  //      vetorial, nao e foto de satelite, mas sempre carrega).
  //   4) Se tudo falhar (sem internet): textura embutida no proprio
  //      pacote do CesiumJS, nao depende de nenhum servidor.
  async function loadBestImagery() {
    const layers = viewer.imageryLayers;

    if (hasToken) {
      try {
        const provider = await Cesium.createWorldImageryAsync({
          style: Cesium.IonWorldImageryStyle.AERIAL,
        });
        layers.addImageryProvider(provider);
        return;
      } catch (err) {
        console.warn("Falha ao carregar imagery do Cesium Ion, tentando Esri.", err);
      }
    }

    try {
      const provider = await Cesium.ArcGisMapServerImageryProvider.fromUrl(
        "https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer",
        { enablePickFeatures: false }
      );
      layers.addImageryProvider(provider);
      return;
    } catch (err) {
      console.warn("Falha ao carregar Esri World Imagery, tentando OpenStreetMap.", err);
    }

    try {
      const provider = new Cesium.OpenStreetMapImageryProvider({
        url: "https://a.tile.openstreetmap.org/",
      });
      layers.addImageryProvider(provider);
      return;
    } catch (err) {
      console.warn("Falha ao carregar OpenStreetMap, usando textura embutida.", err);
    }

    try {
      const provider = await Cesium.TileMapServiceImageryProvider.fromUrl(
        "https://cesium.com/downloads/cesiumjs/releases/1.118/Build/Cesium/Assets/Textures/NaturalEarthII"
      );
      layers.addImageryProvider(provider);
    } catch (err) {
      console.error("Nenhuma fonte de imagem do globo pode ser carregada.", err);
    }
  }

  loadBestImagery();

  // camera inicial: visao ampla da Terra a partir do espaco
  viewer.camera.setView({
    destination: Cesium.Cartesian3.fromDegrees(-40, 10, 22000000),
  });

  window.WTX.viewer = viewer;
  window.WTX.INITIAL_VIEW = Cesium.Cartesian3.fromDegrees(-40, 10, 22000000);

  // O Cesium Viewer registra por padrao um duplo-clique -> "rastrear
  // objeto" (a camera trava na entidade e nunca mostra nome/detalhes -
  // e exatamente essa acao "muda", sem info nenhuma, que os usuarios
  // relataram ao clicar rapido demais num satelite). Removemos esse
  // atalho padrao: rastrear so acontece pelo botao "RASTREAR OBJETO"
  // do nosso proprio painel, depois que o nome e os detalhes ja
  // apareceram no clique simples (ver main.js).
  viewer.screenSpaceEventHandler.removeInputAction(Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

  window.WTX.resetCamera = function () {
    viewer.camera.flyTo({
      destination: window.WTX.INITIAL_VIEW,
      duration: 1.4,
    });
  };

  window.WTX.zoomBy = function (factor) {
    const height = viewer.camera.positionCartographic.height;
    viewer.camera.zoomIn(height * factor);
  };

  window.WTX.flyTo = function (lon, lat, height) {
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(lon, lat, height || 1500000),
      duration: 1.6,
    });
  };

  window.WTX.locateUser = function () {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition((pos) => {
      window.WTX.flyTo(pos.coords.longitude, pos.coords.latitude, 900000);
    });
  };
})();
