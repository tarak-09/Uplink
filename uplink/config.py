"""Settings, read from the environment and an optional .env file (real environment wins)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

LANGUAGES = {"english": "english", "en": "english", "telugu": "telugu", "te": "telugu", "hindi": "hindi", "hi": "hindi"}
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
PROVIDERS = ("cloud", "twilio")
MIN_POLL_SECONDS = 60
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(ValueError):
    """A setting is missing or malformed. The message names the variable to fix."""


@dataclass(frozen=True)
class Settings:
    # The network being watched
    feed_url: str = "https://map.ffmuc.net/data/meshviewer.json"
    network_name: str = "Freifunk München"
    poll_interval: int = 60
    stale_after: int = 180
    language: str = "english"
    anonymize: bool = False
    watch_nodes: tuple[str, ...] = ()
    contact: str = ""

    # When to send a message
    alert_min_nodes: int = 2
    alert_min_users: int = 15
    confirm_seconds: int = 60
    alerts_per_hour: int = 6

    # Meta Model API
    model_api_key: str = field(default="", repr=False)
    model_base_url: str = "https://api.meta.ai/v1"
    model_name: str = "muse-spark-1.3"
    reasoning_effort: str = "low"
    asr_model: str = "muse-voice-transcribe-1.0"

    # WhatsApp: the Cloud API ("cloud") or Twilio's sandbox ("twilio")
    whatsapp_provider: str = "cloud"

    # WhatsApp Cloud API
    whatsapp_token: str = field(default="", repr=False)
    whatsapp_phone_number_id: str = ""
    whatsapp_to: str = ""
    whatsapp_app_secret: str = field(default="", repr=False)
    whatsapp_verify_token: str = field(default="", repr=False)
    whatsapp_api_version: str = "v26.0"
    whatsapp_alert_template: str = ""
    whatsapp_template_language: str = "en"
    graph_base_url: str = "https://graph.facebook.com"

    # Twilio Sandbox for WhatsApp
    twilio_account_sid: str = ""
    twilio_auth_token: str = field(default="", repr=False)
    twilio_whatsapp_from: str = "14155238886"
    twilio_webhook_url: str = ""
    twilio_base_url: str = "https://api.twilio.com"

    # Running
    data_dir: Path = Path("data")
    record_snapshots: bool = True
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    ffmpeg: str = "ffmpeg"

    @property
    def has_model(self) -> bool:
        return bool(self.model_api_key)

    @property
    def uses_twilio(self) -> bool:
        return self.whatsapp_provider == "twilio"

    @property
    def has_whatsapp(self) -> bool:
        return not self.missing_for_alerts()

    def missing_for_alerts(self) -> list[str]:
        """The variables still needed before alerts can reach WHATSAPP_TO."""
        if self.uses_twilio:
            needed = {"TWILIO_ACCOUNT_SID": self.twilio_account_sid, "TWILIO_AUTH_TOKEN": self.twilio_auth_token}
        else:
            needed = {"WHATSAPP_TOKEN": self.whatsapp_token, "WHATSAPP_PHONE_NUMBER_ID": self.whatsapp_phone_number_id}
        needed["WHATSAPP_TO"] = self.whatsapp_to
        return [name for name, value in needed.items() if not value]

    def missing_for_webhook(self) -> list[str]:
        """The variables still needed before Uplink can answer messages."""
        missing = self.missing_for_alerts()
        if not self.uses_twilio:
            extra = {
                "WHATSAPP_APP_SECRET": self.whatsapp_app_secret,
                "WHATSAPP_VERIFY_TOKEN": self.whatsapp_verify_token,
            }
            missing += [name for name, value in extra.items() if not value]
        return missing

    @property
    def feed_is_local(self) -> bool:
        parsed = urlparse(self.feed_url)
        return parsed.scheme == "file" or (parsed.hostname or "") in _LOCAL_HOSTS

    def validate(self) -> None:
        if self.poll_interval < MIN_POLL_SECONDS and not self.feed_is_local:
            raise ConfigError(
                f"POLL_INTERVAL_SECONDS must be at least {MIN_POLL_SECONDS}: the map updates once a "
                "minute, and polling faster only adds load on a volunteer-run server."
            )
        if self.stale_after < 90:
            raise ConfigError("STALE_AFTER_SECONDS must be at least 90; nodes answer about once a minute.")
        if self.whatsapp_provider not in PROVIDERS:
            raise ConfigError(
                "WHATSAPP_PROVIDER must be cloud (Meta's WhatsApp Cloud API) or twilio (Twilio's sandbox)"
            )
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise ConfigError(f"MODEL_REASONING_EFFORT must be one of: {', '.join(REASONING_EFFORTS)}")
        if not re.fullmatch(r"v\d+\.\d+", self.whatsapp_api_version):
            raise ConfigError("WHATSAPP_API_VERSION looks like v26.0")
        if not 0 < self.webhook_port < 65536:
            raise ConfigError("WEBHOOK_PORT must be between 1 and 65535")
        for name in ("alert_min_nodes", "alerts_per_hour"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name.upper()} must be at least 1")


def load_settings(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = Path(".env"),
    **overrides: Any,
) -> Settings:
    """Build Settings from `environ` (default: os.environ) on top of `env_file`, then validate."""
    merged = dict(read_env_file(env_file)) if env_file and env_file.is_file() else {}
    merged.update(os.environ if environ is None else environ)
    values: dict[str, Any] = {}
    for variable, (attribute, convert) in _VARIABLES.items():
        raw = merged.get(variable, "").strip()
        if not raw:
            continue
        try:
            values[attribute] = convert(raw)
        except ValueError as exc:
            raise ConfigError(f"{variable}: {exc}") from None
    known = {f.name for f in fields(Settings)}
    values.update({k: v for k, v in overrides.items() if k in known and v is not None})
    settings = Settings(**values)
    settings.validate()
    return settings


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Supports comments, `export`, and single or double quotes."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"(?:^|\s)#", value, maxsplit=1)[0].rstrip()  # "KEY=   # comment" is empty
        values[key] = value
    return values


def _boolean(raw: str) -> bool:
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected true or false, got {raw!r}")


def _integer(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"expected a whole number, got {raw!r}") from None


def _language(raw: str) -> str:
    try:
        return LANGUAGES[raw.lower()]
    except KeyError:
        raise ValueError("choose english, telugu or hindi") from None


def _url(raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https", "file"} or not (parsed.netloc or parsed.path):
        raise ValueError(f"not a URL: {raw!r}")
    return raw.rstrip("/") if parsed.scheme != "file" else raw


def _phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        raise ValueError("use the full number with country code, for example 14155550123")
    return digits


def _names(raw: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


_VARIABLES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "FEED_URL": ("feed_url", _url),
    "NETWORK_NAME": ("network_name", str),
    "POLL_INTERVAL_SECONDS": ("poll_interval", _integer),
    "STALE_AFTER_SECONDS": ("stale_after", _integer),
    "LANGUAGE": ("language", _language),
    "ANONYMIZE": ("anonymize", _boolean),
    "WATCH_NODES": ("watch_nodes", _names),
    "CONTACT": ("contact", str),
    "ALERT_MIN_NODES": ("alert_min_nodes", _integer),
    "ALERT_MIN_USERS": ("alert_min_users", _integer),
    "CONFIRM_SECONDS": ("confirm_seconds", _integer),
    "ALERTS_PER_HOUR": ("alerts_per_hour", _integer),
    "MODEL_API_KEY": ("model_api_key", str),
    "MODEL_BASE_URL": ("model_base_url", _url),
    "MODEL_NAME": ("model_name", str),
    "MODEL_REASONING_EFFORT": ("reasoning_effort", str.lower),
    "ASR_MODEL": ("asr_model", str),
    "WHATSAPP_PROVIDER": ("whatsapp_provider", str.lower),
    "WHATSAPP_TOKEN": ("whatsapp_token", str),
    "WHATSAPP_PHONE_NUMBER_ID": ("whatsapp_phone_number_id", str),
    "WHATSAPP_TO": ("whatsapp_to", _phone),
    "WHATSAPP_APP_SECRET": ("whatsapp_app_secret", str),
    "WHATSAPP_VERIFY_TOKEN": ("whatsapp_verify_token", str),
    "WHATSAPP_API_VERSION": ("whatsapp_api_version", str),
    "WHATSAPP_ALERT_TEMPLATE": ("whatsapp_alert_template", str),
    "WHATSAPP_TEMPLATE_LANGUAGE": ("whatsapp_template_language", str),
    "GRAPH_BASE_URL": ("graph_base_url", _url),
    "TWILIO_ACCOUNT_SID": ("twilio_account_sid", str),
    "TWILIO_AUTH_TOKEN": ("twilio_auth_token", str),
    "TWILIO_WHATSAPP_FROM": ("twilio_whatsapp_from", _phone),
    "TWILIO_WEBHOOK_URL": ("twilio_webhook_url", _url),
    "TWILIO_BASE_URL": ("twilio_base_url", _url),
    "DATA_DIR": ("data_dir", Path),
    "RECORD_SNAPSHOTS": ("record_snapshots", _boolean),
    "WEBHOOK_HOST": ("webhook_host", str),
    "WEBHOOK_PORT": ("webhook_port", _integer),
    "FFMPEG_PATH": ("ffmpeg", str),
}
