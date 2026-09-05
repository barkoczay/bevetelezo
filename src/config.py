from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # A Railway a DATABASE_URL-t automatikusan beinjektálja a Postgres addonból.
    # A "postgres://" prefixet a SQLAlchemy nem ismeri, ezért normalizáljuk.
    database_url: str = "postgresql+psycopg://localhost/bevetelezo"

    jwt_secret: str = "valtsd-meg-productionben"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 12

    # Naturasoft export alapértelmezései
    default_warehouse: str = "Szüret utca"

    # A PWA origin(ek), vesszővel elválasztva. Fejlesztéskor '*' is lehet,
    # élesben a saját domaint add meg.
    cors_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
