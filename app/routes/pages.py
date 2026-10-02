from flask import Blueprint, render_template
import os
import time

pages_bp = Blueprint("pages", __name__)

# BUGFIX: o navegador as vezes mantem em cache uma versao antiga dos
# .js/.css estaticos mesmo depois de trocar os arquivos no disco (Flask
# nao versiona a URL por padrao). Isso fazia mudancas no frontend
# (ex.: caixa "FONTES DE IMAGEM") nao aparecerem mesmo com o codigo
# certo no servidor. _BUILD_STAMP muda a cada reinicio do processo,
# entao a URL do script muda junto e forca o navegador a buscar de novo.
_BUILD_STAMP = str(int(time.time()))


@pages_bp.route("/")
def index():
    return render_template(
        "index.html",
        build_v=_BUILD_STAMP,
        cesium_token=os.environ.get("CESIUM_TOKEN") or os.environ.get("WTX_CESIUM_TOKEN") or "",
        google_maps_key=os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("WTX_GOOGLE_MAPS_KEY") or "",
    )
