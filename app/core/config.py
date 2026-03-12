from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    github_token: str = ""
    openai_api_key: str = ""
    codebase_path: str = "/Users/Sidreddy/DevCode/SerpApiTestTool"
    # AWS — leave blank to use local credentials (~/.aws/credentials / env vars)
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    app_name: str = "Agent Platform"
    debug: bool = False

    class Config:
        env_file = ".env"


settings = Settings()
