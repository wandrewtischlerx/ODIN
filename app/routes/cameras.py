from flask import Blueprint, jsonify, Response, request

from ..services.camera_service import (
    _apply_status,
    get_cameras,
    get_camera_image,
    get_opencctv_detail,
    get_opencctv_stats,
    get_camera_stream_info,
    get_hls_playlist,
    get_hls_segment,
)

cameras_bp = Blueprint("cameras", __name__)


def _parse_bbox():
    """bbox=minLon,minLat,maxLon,maxLat (opcional)."""
    raw = request.args.get("bbox")
    if not raw:
        return None
    try:
        parts = [float(x.strip()) for x in raw.split(",")]
        if len(parts) != 4:
            return None
        min_lon, min_lat, max_lon, max_lat = parts
        if min_lon > max_lon:
            min_lon, max_lon = max_lon, min_lon
        if min_lat > max_lat:
            min_lat, max_lat = max_lat, min_lat
        return (min_lon, min_lat, max_lon, max_lat)
    except (TypeError, ValueError):
        return None


@cameras_bp.route("/")
def list_cameras():
    bbox = _parse_bbox()
    limit = request.args.get("limit", type=int)
    cameras, source, global_total = get_cameras(bbox=bbox, opencctv_limit=limit)
    ocv_stats = get_opencctv_stats()

    # Quantas cameras plotadas em cada faixa do semaforo de atualizacao.
    # O front usa isso pra mostrar o numero ao lado de cada filtro de cor.
    status_counts = {"verde": 0, "amarelo": 0, "vermelho": 0, "cinza": 0}
    for cam in cameras:
        cor = cam.get("status_color")
        if cor in status_counts:
            status_counts[cor] += 1

    ocv_total = int(ocv_stats.get("total_markers", 0) or 0)
    # Garante que o HUD nunca mostre a amostra no lugar do catálogo.
    if ocv_total and (not global_total or global_total < ocv_total):
        global_total = max(int(global_total or 0), ocv_total)
    return jsonify({
        "source": source,
        "is_live": source != "demo",
        "count": len(cameras),                 # plotado agora (amostra/viewport)
        "plotted": len(cameras),
        "global_total": global_total,          # TOTAL DA TERRA (HUD)
        "opencctv_total": ocv_total,           # índice completo OpenCCTV
        "bbox": list(bbox) if bbox else None,
        "status_counts": status_counts,
        "cameras": cameras,
    })


@cameras_bp.route("/detail/<path:camera_id>")
def camera_detail(camera_id):
    """
    Detalhe sob demanda (principalmente OpenCCTV lite): nome, cidade,
    feed. Outras fontes devolvem o que ja esta na listagem se houver.
    """
    if str(camera_id).startswith("ocv-"):
        detail = get_opencctv_detail(camera_id)
        if not detail:
            return jsonify({"error": "camera nao encontrada"}), 404
        # Recalcula o semaforo agora que a camera tem (ou nao) URL de
        # imagem - antes da resolucao ela era so um marker "lite".
        _apply_status(detail)
        return jsonify({"camera": detail, "source": "opencctv_live"})
    return jsonify({"error": "detalhe sob demanda so para OpenCCTV (ocv-*)"}), 400


@cameras_bp.route("/image/<path:camera_id>")
def camera_image(camera_id):
    """
    Proxy da imagem da camera: o navegador do usuario nunca busca a
    imagem direto no dominio da NYC (que bloqueia hotlinking por
    Referer/Origin com 403) - busca aqui, e o backend repassa os bytes
    com o Referer correto, como um navegador faria ao visitar o site
    deles diretamente.
    """
    content, content_type = get_camera_image(camera_id)
    if content is None:
        return jsonify({"error": "imagem indisponivel"}), 502

    return Response(
        content,
        mimetype=content_type,
        headers={"Cache-Control": "no-store"},
    )


@cameras_bp.route("/opencctv/stats")
def opencctv_stats():
    return jsonify(get_opencctv_stats())


@cameras_bp.route("/stream/<path:camera_id>")
def camera_stream(camera_id):
    """
    Descobre como tocar esta camera AO VIVO dentro do app (nao so' um
    link pro site de origem): YouTube (embed oficial) ou HLS (extraido
    da pagina - ver comentario em camera_service.get_camera_stream_info,
    inclusive o limite honesto de quando isso NAO funciona). O front
    chama isto ao abrir uma camera "somente_pagina" antes de decidir
    entre <iframe> do YouTube, <video> com hls.js, ou o aviso de "sem
    transmissao incorporavel".
    """
    page_url = request.args.get("page_url")
    return jsonify(get_camera_stream_info(camera_id, page_url))


@cameras_bp.route("/hls/<path:camera_id>/playlist.m3u8")
def camera_hls_playlist(camera_id):
    """Proxy do manifesto HLS: busca o .m3u8 real (com o Referer que a
    fonte exige) e reescreve toda URL de dentro pra passar por aqui -
    o navegador nunca fala direto com o CDN de origem."""
    override = request.args.get("u")  # ja' vem decodificado pelo Werkzeug
    text, ctype = get_hls_playlist(camera_id, override_url=override)
    if text is None:
        return jsonify({"error": "playlist indisponivel"}), 502
    return Response(text, mimetype=ctype, headers={"Cache-Control": "no-store"})


@cameras_bp.route("/hls/<path:camera_id>/segment")
def camera_hls_segment(camera_id):
    """Proxy de 1 segmento (.ts/.m4s) ou de uma sub-URL referenciada
    pelo manifesto. So' aceita URL cujo host bate com o host ja'
    descoberto pra esta camera (get_hls_segment valida isso) - protege
    contra usar esta rota como proxy aberto pra qualquer host."""
    segment_url = request.args.get("u")
    if not segment_url:
        return jsonify({"error": "parametro u ausente"}), 400
    content, ctype = get_hls_segment(camera_id, segment_url)
    if content is None:
        return jsonify({"error": "segmento indisponivel"}), 502
    return Response(content, mimetype=ctype, headers={"Cache-Control": "no-store"})
