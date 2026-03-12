from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    github_token: str = ""
    openai_api_key: str = ""
    codebase_path: str = "/Users/Sidreddy/VoyageCode/SerpApiTestTool"
    app_name: str = "Agent Platform"
    debug: bool = False

    class Config:
        env_file = ".env"


settings = Settings()
