import os
import tempfile
from pathlib import Path

_TEST_DIR = Path(tempfile.mkdtemp(prefix="amoregymbro-pytest-"))
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DATABASE_PATH"] = str(_TEST_DIR / "default.db")
os.environ["SYNC_ENABLED"] = "false"
os.environ["GARMIN_API_KEY"] = ""
os.environ["GARMIN_SYNC_ENABLED"] = "false"
os.environ["HEVY_API_KEY"] = ""
os.environ["HEVY_WEBHOOK_AUTHORIZATION"] = ""
os.environ["WEBHOOK_WORKER_ENABLED"] = "false"
os.environ["APP_PASSWORD"] = "test-password-123"
os.environ["APP_SECRET"] = "test-secret-value-32chars"
os.environ["HEVY_PAGE_DELAY_SECONDS"] = "0"
os.environ["SECURE_COOKIES"] = "false"
os.environ["OPENAI_API_KEY"] = ""
os.environ["OPENAI_MODEL"] = ""
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
