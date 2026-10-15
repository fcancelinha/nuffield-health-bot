import os

os.environ.setdefault("NUFFIELD_EMAIL", "test@example.com")
os.environ.setdefault("NUFFIELD_PASSWORD", "test-password")

# Tests assert against the shipped defaults, so never read the operator's .env.
from nuffield_bot.config import Settings  # noqa: E402

Settings.model_config["env_file"] = None
