from fastapi import FastAPI, HTTPException
import time

app = FastAPI(title="Container Instável")

inicio = time.time()

@app.get("/")
def root():
    return {
        "service": "instavel",
        "message": "API continua executando"
    }

@app.get("/health")
def health():
    tempo_ativo = time.time() - inicio

    if tempo_ativo > 30:
        raise HTTPException(
            status_code=500,
            detail="Falha simulada"
        )

    return {
        "status": "healthy",
        "tempo_ativo": tempo_ativo
    }