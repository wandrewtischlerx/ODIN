from app import create_app

app = create_app()

if __name__ == "__main__":
    # threaded=True e essencial aqui: sem isso, o servidor de
    # desenvolvimento do Flask atende um pedido por vez. Como a busca de
    # aeronaves faz varias chamadas a APIs externas (que podem demorar
    # ou dar timeout se a rede estiver lenta), sem threading a pagina
    # inteira travaria esperando essa unica requisicao terminar.
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
