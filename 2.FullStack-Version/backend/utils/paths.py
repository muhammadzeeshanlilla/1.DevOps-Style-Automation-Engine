"""Stable project paths, independent of the terminal's working directory."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "settings.json"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "engine.log"
ENGINE_LOCK_PATH = PROJECT_ROOT / "engine.lock"
ENGINE_PID_PATH = PROJECT_ROOT / "engine.pid"
ENGINE_STATUS_PATH = PROJECT_ROOT / "engine.status.json"
STOP_REQUEST_DIR = PROJECT_ROOT
STOP_REQUEST_PREFIX = "engine.stop."


def resolve_project_path(path):
    """Resolve a relative path from the project root; keep absolute paths."""
    configured_path = Path(path)
    if configured_path.is_absolute():
        return configured_path
    return (PROJECT_ROOT / configured_path).resolve()
