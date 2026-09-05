from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.routes import core, orders, receipts

app = FastAPI(
    title="Raktári bevételező",
    description="Naturasoft-kompatibilis bevételezés kezelés",
    version="0.1.0",
)

# A PWA külön origin-ről érkezik (Railway static / saját domain)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(core.router)
app.include_router(orders.router)
app.include_router(receipts.router)


@app.get("/health", tags=["rendszer"])
def health() -> dict:
    return {"status": "ok"}
