from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Advanced RAG Agentic AI"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://raguser:change_me@localhost:5432/ragdb"
    chroma_path: str = "/data/chroma"
    jwt_secret: str = "CHANGE_ME"
    jwt_expire_minutes: int = 720
    cors_origins: str = "http://localhost:8080,http://localhost:5173"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    llm_provider: str = "groq"
    groq_api_key: str = ""
    serpapi_api_key: str = ""
    llm_model: str = "openai/gpt-oss-20b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    max_upload_mb: int = 20
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

settings = Settings()
