import os

FIREWORKS_API_KEY: str | None = os.getenv("FIREWORKS_API_KEY")
FIREWORKS_MODEL: str = os.getenv(
	"FIREWORKS_MODEL", "accounts/fireworks/models/glm-5p3-flash"
)
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./ledgerlens.db")