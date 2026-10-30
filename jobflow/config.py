"""Paths, settings and the candidate profile.

Real config lives in config/settings.yaml and config/profile.yaml (gitignored). If
either is missing the .example file is used, so a fresh checkout still imports
and the tests run without any personal data.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SCREENS_DIR = DATA_DIR / "screens"
DB_PATH = DATA_DIR / "jobflow.db"
SCREENS_DIR.mkdir(parents=True, exist_ok=True)


def _load(name: str) -> dict:
    real = CONFIG_DIR / f"{name}.yaml"
    path = real if real.exists() else CONFIG_DIR / f"{name}.example.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


SECRETS = ROOT / "secrets.txt"


def read_secrets() -> dict:
    out = {}
    if SECRETS.is_file():
        for line in SECRETS.read_text(encoding="utf-8-sig").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip("\"'")
            if key and val:
                out[key] = val
    return out


def _load_secrets(override: bool = False) -> None:
    """KEY=value lines from secrets.txt become env vars (unless already set).
    Keeps keys out of config files that might get shared."""
    for key, val in read_secrets().items():
        if override:
            os.environ[key] = val
        else:
            os.environ.setdefault(key, val)


def write_secret(key: str, value: str) -> None:
    secrets = read_secrets()
    secrets[key] = value.strip()
    SECRETS.write_text("".join(f"{k}={v}\n" for k, v in secrets.items()), encoding="utf-8")
    os.environ[key] = value.strip()


_load_secrets()
SETTINGS: dict = _load("settings")
PROFILE: dict = _load("profile")


def profile_text() -> str:
    return yaml.safe_dump(PROFILE, sort_keys=False, allow_unicode=True)


def reload() -> None:
    """Re-read config files in place, so code holding SETTINGS/PROFILE sees the
    change (used after an upload in the dashboard)."""
    _load_secrets(override=True)
    SETTINGS.clear()
    SETTINGS.update(_load("settings"))
    PROFILE.clear()
    PROFILE.update(_load("profile"))


def ensure_settings_file() -> Path:
    real = CONFIG_DIR / "settings.yaml"
    if not real.exists():
        real.write_text((CONFIG_DIR / "settings.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return real


def set_resume_path(path: Path) -> None:
    """Point resume_path at a file, editing settings.yaml without losing its comments."""
    import re

    real = ensure_settings_file()
    try:
        value = str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        value = str(path)
    text = real.read_text(encoding="utf-8")
    line = f'resume_path: "{value}"'
    text, n = re.subn(r"(?m)^resume_path:.*$", line.replace("\\", "\\\\"), text)
    if not n:
        text = line + "\n" + text
    real.write_text(text, encoding="utf-8")


def resume_path() -> Path:
    p = Path(SETTINGS.get("resume_path") or "resume.pdf")
    return p if p.is_absolute() else ROOT / p


def section(name: str) -> dict:
    return SETTINGS.get(name) or {}
