from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://autoscreen:autoscreen@postgres:5432/autoscreen"
    redis_url: str = "redis://redis:6379/0"

    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 24

    default_openai_api_key: str = ""

    static_root: str = "/app/static"
    screenshots_root: str = "/app/static/screenshots"

    app_name: str = "auto_screen API"
    debug: bool = False

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
