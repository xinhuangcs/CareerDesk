"""CareerDesk deployment values and secrets loaded from code or platform configuration."""

from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
import os
from pathlib import Path
import stat
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import (
    DEFAULT_DATA_DIR,
    DEFAULT_FRONTEND_DIST_DIR,
    DEFAULT_LOG_DIR,
    ENV_FILE,
    STARTUP_ENV_KEYS,
    canonical_data_dir,
    canonical_log_dir,
)

_ENV_FILE = ENV_FILE

# Record only names present before dotenv loading, never secret values. Settings uses this to
# avoid presenting startup-managed configuration as persistently editable.
_DOTENV_PRELOAD_ENV_KEYS = STARTUP_ENV_KEYS

OPENAI_COMPATIBLE_ENDPOINT_ENVS = ("OPENAI_BASE_URL", "LLM_BASE_URL")


def externally_managed_environment_variable(name: str) -> bool:
    """Return whether the startup environment supplied a variable before dotenv loading."""
    return name in _DOTENV_PRELOAD_ENV_KEYS


@dataclass(frozen=True, slots=True)
class OpenAICompatibleEndpoint:
    """Non-sensitive endpoint snapshot for the generic OpenAI-compatible channel."""

    status: Literal["configured", "missing", "invalid"]
    url: str | None
    source: Literal["OPENAI_BASE_URL", "LLM_BASE_URL"] | None
    externally_managed: bool
    issue: str | None = None


def _normalize_openai_compatible_endpoint(value: str) -> str:
    """Validate and normalize a displayable HTTP(S) endpoint suitable for the SDK.

    Credentials, queries, and fragments fail closed because they can leak through logs or
    errors. Preserve paths such as the common OpenAI-compatible ``/v1`` contract.
    """
    if not value or value != value.strip() or len(value) > 2048:
        raise ValueError("endpoint 为空、过长或首尾含空白")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("endpoint 不能包含控制字符")
    if any(character.isspace() for character in value) or "\\" in value:
        raise ValueError("endpoint 不能包含空白或反斜杠")
    # urlsplit cannot distinguish no query from an empty query delimiter. Reject the delimiter
    # itself before parsing because both forms are outside the contract.
    if "?" in value or "#" in value:
        raise ValueError("endpoint 不能包含 query 或 fragment")
    try:
        parsed = urlsplit(value)
        port = parsed.port  # Access raises ValueError for malformed or out-of-range ports.
    except ValueError as error:
        raise ValueError("endpoint URL 或端口格式非法") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("endpoint 只允许 http 或 https")
    if not parsed.netloc or parsed.hostname is None:
        raise ValueError("endpoint 必须包含 host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("endpoint 不能携带 userinfo/凭据")
    if parsed.query or parsed.fragment:
        raise ValueError("endpoint 不能包含 query 或 fragment")
    if parsed.netloc.endswith(":"):
        raise ValueError("endpoint 端口格式非法")

    hostname = parsed.hostname
    if "%" in hostname:
        raise ValueError("endpoint host 格式非法")
    try:
        rendered_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError("endpoint host 格式非法") from error
    if not rendered_host or len(rendered_host) > 253:
        raise ValueError("endpoint host 格式非法")
    if ":" in rendered_host:
        rendered_host = f"[{rendered_host}]"
    netloc = f"{rendered_host}:{port}" if port is not None else rendered_host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def resolve_openai_compatible_endpoint() -> OpenAICompatibleEndpoint:
    """Mirror Agentmaker's ``OPENAI_BASE_URL -> LLM_BASE_URL`` priority.

    Never return an invalid raw value; expose only non-sensitive state and a safe canonical URL.
    """
    source = next(
        (name for name in OPENAI_COMPATIBLE_ENDPOINT_ENVS if os.environ.get(name)),
        None,
    )
    if source is None:
        return OpenAICompatibleEndpoint("missing", None, None, False)
    try:
        endpoint = _normalize_openai_compatible_endpoint(os.environ[source])
    except ValueError as error:
        return OpenAICompatibleEndpoint(
            "invalid",
            None,
            source,
            externally_managed_environment_variable(source),
            str(error),
        )
    return OpenAICompatibleEndpoint(
        "configured",
        endpoint,
        source,
        externally_managed_environment_variable(source),
    )


def _harden_env_before_load(path: Path) -> None:
    """Validate the final file and remove POSIX group/other permissions before reading secrets."""
    if not os.path.lexists(path):
        return
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("配置文件必须是普通文件，不能是符号链或特殊文件")
    if info.st_nlink != 1:
        raise RuntimeError("配置文件不能有额外硬链接，否则密钥可能被暴露或写入其他路径")
    if os.name == "posix":
        tightened = stat.S_IMODE(info.st_mode) & 0o700
        if tightened != stat.S_IMODE(info.st_mode):
            os.chmod(path, tightened)


# Agentmaker reads provider credentials from the environment; real environment values override dotenv.
_harden_env_before_load(_ENV_FILE)
load_dotenv(_ENV_FILE)


def env_file_path() -> Path:
    """Return the fixed configuration path, reading the module variable lazily for test redirects."""
    return _ENV_FILE


class Settings(BaseSettings):
    """Validated deployment configuration loaded once during startup."""

    # APP_ prevents collisions with host variables; APP_NAME is the one
    # explicit unprefixed deployment alias.
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_prefix="APP_", populate_by_name=True, extra="ignore")

    app_name: str = Field("careerdesk", validation_alias="APP_NAME")
    # Runtime mode is the security boundary. Debug controls documentation exposure only and
    # never bypasses authentication or HTTP protections. Desktop is the local default; Docker
    # must explicitly select server, while conftest selects test.
    runtime_mode: Literal["development", "desktop", "server", "test"] = "desktop"
    debug: bool = True
    timezone: str = "Asia/Shanghai"

    # Server mode requires explicit values; local modes are code-fixed to loopback and reject "*".
    allowed_hosts: str = ""
    allowed_origins: str = ""

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        """Reject misspelled IANA zones at startup before scheduling and business dates diverge."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"未知时区：{value}") from error
        return value
    # Single local-data root for the business database and uploads. Never put an active SQLite
    # directory on a sync drive; sync only complete Online Backup packages.
    data_dir: str = str(DEFAULT_DATA_DIR)

    @field_validator("data_dir")
    @classmethod
    def _canonical_data_dir(cls, value: str) -> str:
        """Canonicalize home, relative, and symlinked data roots to one absolute path.

        Process locks and the database must share the same canonical root so aliases cannot
        bypass single-instance enforcement.
        """
        return str(canonical_data_dir(value))
    # Agent traces are bounded metadata-only logs excluded from business backups. A custom data
    # root without a log root uses a sibling logs directory, keeping portable/test instances out
    # of global paths while maintaining physical separation.
    log_dir: str | None = None

    @model_validator(mode="after")
    def _resolve_and_separate_log_dir(self):
        data_root = Path(self.data_dir)
        requested = self.log_dir
        if requested is None:
            requested = str(
                DEFAULT_LOG_DIR
                if data_root == Path(DEFAULT_DATA_DIR).resolve()
                else data_root.parent / "logs"
            )
        log_root = canonical_log_dir(requested)
        if (
            log_root == data_root
            or log_root.is_relative_to(data_root)
            or data_root.is_relative_to(log_root)
        ):
            raise ValueError("APP_LOG_DIR 必须与 APP_DATA_DIR 分离，二者不能相同或互相嵌套")
        self.log_dir = str(log_root)
        return self
    frontend_dist_dir: str = str(DEFAULT_FRONTEND_DIST_DIR)

    # Model IDs use provider:model; provider credentials are managed separately.
    llm_model: str | None = None

    # Agentmaker only knows capacities for a provider's exact default model.
    # A switched/local/OpenAI-compatible model must declare both values instead
    # of inheriting a guessed 8K window or another model's provider defaults.
    llm_context_window: int | None = Field(default=None, ge=1_024, le=2_147_483_647)
    llm_max_output_tokens: int | None = Field(default=None, ge=256, le=2_147_483_647)

    @field_validator("llm_context_window", "llm_max_output_tokens", mode="before")
    @classmethod
    def _blank_model_capacity_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("llm_model", mode="before")
    @classmethod
    def _blank_model_is_none(cls, value):
        """Normalize a blank model to an unconfigured value."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_explicit_llm_capabilities(self):
        context = self.llm_context_window
        output = self.llm_max_output_tokens
        if (context is None) != (output is None):
            raise ValueError(
                "APP_LLM_CONTEXT_WINDOW 与 APP_LLM_MAX_OUTPUT_TOKENS 必须同时配置或同时留空"
            )
        if context is not None and output is not None and output > context:
            raise ValueError("APP_LLM_MAX_OUTPUT_TOKENS 不能大于 APP_LLM_CONTEXT_WINDOW")
        if self.llm_model is None and context is not None:
            raise ValueError("未配置 APP_LLM_MODEL 时不能单独配置模型容量")
        return self

    # Outbound consent and provider credentials are separate boundaries. An API key indicates
    # capability, not consent to send conversation text for embeddings or to search the web.
    # strict_offline overrides while preserving model, key, and child-consent configuration.
    strict_offline: bool = False
    allow_conversation_embedding: bool = False
    allow_web_research: bool = False
    # Deep research broadcasts every query to every outlet (roughly 3x quota). The unofficial
    # community DDG fallback can be disabled independently.
    allow_deep_research: bool = False
    allow_ddg_fallback: bool = True

    @property
    def conversation_embedding_enabled(self) -> bool:
        """Return whether this instance effectively permits remote conversation embeddings."""
        return self.allow_conversation_embedding and not self.strict_offline

    @property
    def web_research_enabled(self) -> bool:
        """Return whether this instance effectively permits web search for company research."""
        return self.allow_web_research and not self.strict_offline

    @property
    def deep_research_enabled(self) -> bool:
        """Return whether this instance permits deep cross-query, cross-outlet research."""
        return self.allow_deep_research and self.web_research_enabled

    # Server mode requires the gateway secret and forbids the local fake user.
    gateway_auth_secret: SecretStr | None = None
    dev_fake_user: str | None = "me"

    @field_validator("gateway_auth_secret", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value):
        """Reject blank secrets through the normal missing-secret path."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("dev_fake_user", mode="before")
    @classmethod
    def _blank_user_is_none(cls, value):
        """Treat a blank fake user as unconfigured."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def db_path(self) -> str:
        """Return the business database path under the data root."""
        return str(Path(self.data_dir) / "careerdesk.db")

    @property
    def trace_path(self) -> str:
        """Bounded metadata trace path outside the business-data backup root."""
        if self.log_dir is None:  # pragma: no cover - post validation always resolves it
            raise RuntimeError("APP_LOG_DIR 尚未解析")
        return str(Path(self.log_dir) / "traces.jsonl")

    @property
    def allowed_host_list(self) -> list[str]:
        """Return the TrustedHost allowlist; local and test modes never expand it from env."""
        if self.runtime_mode == "server":
            return [item.strip().lower() for item in self.allowed_hosts.split(",") if item.strip()]
        hosts = ["localhost", "127.0.0.1", "[::1]"]
        if self.runtime_mode == "test":
            hosts.append("testserver")
        return hosts

    @property
    def allowed_origin_list(self) -> list[str]:
        """Return the exact server-mode Origin allowlist; middleware validates local loopback."""
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """
    Return the process-wide Settings instance, instantiated once through ``lru_cache``.

    Returns:
        The configuration object used through ``Depends(get_settings)`` and replaceable in tests.
    """
    return Settings()


def local_today(timezone: str | None = None) -> date:
    """Return today in the configured IANA zone used by every business date."""
    name = timezone or get_settings().timezone
    return datetime.now(ZoneInfo(name)).date()
