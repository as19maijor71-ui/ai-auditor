from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    BOT_TOKEN: str

    AI_PROVIDER: str = "openrouter"

    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "deepseek/deepseek-r1"
    VISION_MODEL: str = "google/gemini-2.5-flash"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1/chat/completions"

    YANDEXGPT_API_KEY: str = ""
    YANDEXGPT_FOLDER_ID: str = ""
    YANDEXGPT_BASE_URL: str = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    GEMINI_API_KEY: str = ""
    GEMINI_TEXT_MODEL: str = "gemini-2.5-flash"
    GEMINI_VISION_MODEL: str = "gemini-2.5-flash-lite"
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/models"

    OPENAI_API_KEY: str = ""
    OPENAI_TEXT_MODEL: str = "gpt-5-mini"
    OPENAI_BASE_URL: str = "https://api.openai.com/v1/chat/completions"

    QUICK_AUDIT_MAX_TOKENS: int = 4096
    QUICK_AUDIT_CLEANED_TEXT_LIMIT: int = 12000
    MEDIA_MAX_PHOTOS: int = 8
    MEDIA_MAX_VIDEOS: int = 3
    MEDIA_MAX_IMAGE_BYTES: int = 8 * 1024 * 1024

    PROXY_URL: str = ""

    MAX_INPUT_LENGTH: int = 2000
    COMPETITOR_MAX_LENGTH: int = 3000
    MAX_RETRIES: int = 1
    DEFAULT_MAX_TOKENS: int = 4096
    REQUEST_TIMEOUT: int = 120

    SQLITE_PATH: str = "auditor/data/bot.db"
    FSM_STATE_TTL: int = 86400

    COMPETITOR_FETCH_TIMEOUT: int = 5

    RATE_LIMIT_MAX: int = 3
    RATE_LIMIT_WINDOW: int = 60

    ADMIN_USER_ID: int = 0

    SUPPORT_CHANNEL: str = ""
    PRIVACY_URL: str = ""

    FREE_AUDIT_LIMIT: int = 3
    SUBSCRIPTION_PRICE: int = 990


settings = Settings()
