from fastapi import FastAPI

app = FastAPI(title="Container Normal")

@app.get("/")
def root():
    return {
        "service": "normal",
        "message": "Funcionando normalmente"
    }

@app.get("/health")
def health():
    return {
        "status": "healthy"
    }