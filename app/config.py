from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://talentscope:talentscope@localhost:5432/talentscope"
    redis_url: str = "redis://localhost:6379/0"
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    groq_api_key: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
