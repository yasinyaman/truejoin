"""Load `Settings` from a YAML file with `${VAR:default}` and `${file:/path}` interpolation."""

import os
import re
from pathlib import Path
from typing import Any

import yaml

from trio.application.settings import Settings


def interpolate_env_vars(value: Any) -> Any:
    """Recursively interpolate environment variables in configuration values.

    Supports ${VAR_NAME} and ${VAR_NAME:default}, and ${file:/path} for a secret
    mounted as a file (its content without the trailing newline). A missing
    file is an error: there is no plain-text fallback for a secret.
    """
    if isinstance(value, str):
        pattern = r"\$\{([^}:]+)(?::([^}]*))?\}"

        def replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            if var_name == "file":
                return Path(match.group(2) or "").read_text(encoding="utf-8").strip()
            default_value = match.group(2) if match.group(2) is not None else ""
            return os.environ.get(var_name, default_value)

        return re.sub(pattern, replacer, value)

    elif isinstance(value, dict):
        return {k: interpolate_env_vars(v) for k, v in value.items()}

    elif isinstance(value, list):
        return [interpolate_env_vars(item) for item in value]

    return value


def load_config(config_path: str | None = None) -> Settings:
    """Load configuration from YAML file with environment variable interpolation.

    Args:
        config_path: Path to the YAML configuration file.
                    Defaults to config/database.yaml relative to project root.

    Returns:
        Settings object with loaded configuration.
    """
    if config_path is None:
        # Default to config/database.yaml relative to project root
        project_root = Path(__file__).parent.parent.parent.parent
        resolved_path = project_root / "config" / "database.yaml"
    else:
        resolved_path = Path(config_path)

    if not resolved_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {resolved_path}")

    with open(resolved_path, encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    # Interpolate environment variables
    interpolated_config = interpolate_env_vars(raw_config)

    # Convert port values to integers if they came from env vars as strings
    if "databases" in interpolated_config:
        for db in interpolated_config["databases"]:
            if "port" in db and isinstance(db["port"], str):
                db["port"] = int(db["port"])

    return Settings(**interpolated_config)
