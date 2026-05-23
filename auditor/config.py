from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv()


class Settings(BaseSettings):
    BOT_TOKEN: str

    AI_PROVIDER: str = "openrouter"

    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "deepseek/deepseek-r1"
    VISION_MODEL: str = "google/gemini-2.0-flash-001"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1/chat/completions"

    YANDEXGPT_API_KEY: str = ""
    YANDEXGPT_FOLDER_ID: str = ""
    YANDEXGPT_BASE_URL: str = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

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
