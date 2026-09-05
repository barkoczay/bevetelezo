from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import get_settings
from fastapi.responses import RedirectResponse

from src.routes import core, orders, receipts, setup

settings = get_settings()

app = FastAPI(
    title="Raktári bevételező",
    description="Naturasoft-kompatibilis bevételezés kezelés",
    version="0.1.0",
)

# A PWA külön origin-ről érkezik. Bearer tokennel dolgozunk, ezért
# allow_credentials nem kell — sütit nem használunk.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(core.router)
app.include_router(orders.router)
app.include_router(receipts.router)
app.include_router(setup.router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health", tags=["rendszer"])
def health() -> dict:
    return {"status": "ok"}
