from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    app_name: str = "Agent Platform"
    debug: bool = False

    class Config:
        env_file = ".env"


settings = Settings()
