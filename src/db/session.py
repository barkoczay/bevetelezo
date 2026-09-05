from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import get_settings

settings = get_settings()

_url = settings.sqlalchemy_url
# A pool paramétereket csak a Postgres érti; SQLite-on (teszt) hibát dobnak.
_engine_kwargs: dict = {"pool_pre_ping": True}
if _url.startswith("postgresql"):
    _engine_kwargs.update(pool_size=5, max_overflow=10)

engine = create_engine(_url, **_engine_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
