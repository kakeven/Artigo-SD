from fastapi import FastAPI
import threading
import time

app = FastAPI(title="Container de Carga")

@app.get("/")
def root():
    return {
        "service": "carga",
        "message": "Use /carga para gerar uso de CPU"
    }


def gerar_carga():
    fim = time.time() + 20

    while time.time() < fim:
        _ = 999999 ** 999


@app.post("/carga")
def carga():
    thread = threading.Thread(target=gerar_carga)
    thread.start()

    return {
        "message": "Carga de CPU iniciada por 20 segundos"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }