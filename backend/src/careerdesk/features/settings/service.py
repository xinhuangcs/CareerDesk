"""Atomic model, credential, and egress settings for the web settings page.

Provider availability comes from the platform AI adapters instead of a manual
list, so framework upgrades flow through automatically. Writes are allowed only
in local mode without a gateway secret; debug mode is not a security input.
Server deployments manage these values through environment secrets and reject
all UI writes. Source layouts retain credentials in a private ``.env`` file,
while installed desktop builds use the operating-system credential store.
Secret values are never returned. UI-managed fields take effect without restart
because settings are uncached and LLM clients are created per request. Values
owned by the shell/container environment are read-only, and compatibility
endpoints are read only at process startup.
"""

import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import set_key, unset_key

from ...core.config import (env_file_path, externally_managed_environment_variable,
                            get_settings, resolve_openai_compatible_endpoint)
from ...core.paths import SOURCE_LAYOUT
from ...platform import credentials
from ...platform.ai.providers import (
    resolve_model_capabilities,
    provider_spec,
    provider_specs,
    validate_model_reference,
)
from ...platform.storage.private import ensure_private_directory, prepare_private_file

# Display names fall back to the provider identifier for new framework entries.
_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic Claude",
    "gemini": "Google Gemini",
    "deepseek": "DeepSeek 深度求索",
    "dashscope": "阿里云百炼 通义千问",
    "moonshot": "月之暗面 Kimi",
    "zhipu": "智谱 GLM",
    "gemini_openai": "Gemini（OpenAI 兼容通道）",
    "modelscope": "魔搭 ModelScope",
    "openai_compatible": "通用 OpenAI 兼容接口",
    "ollama": "Ollama",
    "vllm": "vLLM",
    "sglang": "SGLang",
}

# Server-side allowlist prevents arbitrary environment mutation. The installed
# launcher uses the same list to load the OS keyring before config imports.
ALLOWED_KEY_VARS = credentials.SUPPORTED_CREDENTIAL_NAMES

_MODEL_ENV = "APP_LLM_MODEL"
_TIMEZONE_ENV = "APP_TIMEZONE"
_CAPABILITY_ENVS = {
    "context_window": "APP_LLM_CONTEXT_WINDOW",
    "max_output_tokens": "APP_LLM_MAX_OUTPUT_TOKENS",
}
_OUTBOUND_POLICY_ENVS = {
    "strict_offline": "APP_STRICT_OFFLINE",
    "allow_conversation_embedding": "APP_ALLOW_CONVERSATION_EMBEDDING",
    "allow_web_research": "APP_ALLOW_WEB_RESEARCH",
    "allow_deep_research": "APP_ALLOW_DEEP_RESEARCH",
    "allow_ddg_fallback": "APP_ALLOW_DDG_FALLBACK",
}
_MAX_VALUE_LENGTH = 512  # Real provider keys are much shorter; excess implies bad paste.
_SETTINGS_LOCK = threading.RLock()
_REVISION_PATH: Path | None = None
_REVISION_VALUE: str | None = None
_PERSISTENCE_WARNING: tuple[Path, str] | None = None
_STORAGE_DISCLOSURE_META_KEY = "ui.storage_disclosure_shown.v1"


class SettingsRevisionConflict(RuntimeError):
    """The submitted settings snapshot is stale and requires reconfirmation."""


def claim_storage_disclosure() -> bool:
    """Return true exactly once for a data-root, while durably recording the claim."""
    from ...platform.database import transaction

    with transaction(get_settings().db_path) as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, 'shown')",
            (_STORAGE_DISCLOSURE_META_KEY,),
        )
        return cursor.rowcount == 1


def _revision_locked(env_file: Path) -> str:
    """Return this process's opaque CAS token for the settings file."""
    global _PERSISTENCE_WARNING, _REVISION_PATH, _REVISION_VALUE
    if _REVISION_PATH != env_file or _REVISION_VALUE is None:
        _REVISION_PATH = env_file
        _REVISION_VALUE = secrets.token_urlsafe(24)
        _PERSISTENCE_WARNING = None
    return _REVISION_VALUE


def _advance_revision_locked(env_file: Path) -> str:
    """Rotate the token after commit so every stale tab fails closed."""
    global _REVISION_PATH, _REVISION_VALUE
    _REVISION_PATH = env_file
    _REVISION_VALUE = secrets.token_urlsafe(24)
    return _REVISION_VALUE


def _persistence_warning_locked(env_file: Path) -> str | None:
    """Return the latest persistence warning for this settings file."""
    if _PERSISTENCE_WARNING is None or _PERSISTENCE_WARNING[0] != env_file:
        return None
    return _PERSISTENCE_WARNING[1]


# Hide the OpenAI-compatible Gemini alias because the native protocol is the
# recommended entry. Advanced users can still enter ``gemini_openai:model``.
_HIDDEN_PROVIDERS = ("gemini_openai",)


def _provider_catalog() -> list[dict]:
    """Return framework-supported providers for the frontend selector.

    ``default_model=None`` requires an explicit model; empty ``key_vars`` means
    no credential is needed. ``local`` only states that the model base URL is a
    loopback address; other outbound capabilities remain independently gated.
    """
    catalog = []
    for spec in provider_specs():
        if spec.name in _HIDDEN_PROVIDERS:
            continue
        catalog.append({
            "name": spec.name,
            "label": _PROVIDER_LABELS.get(spec.name, spec.name),
            "default_model": spec.default_model,
            "key_vars": list(spec.key_envs),
            "local": spec.is_local,
            "context_window": spec.context_window,
            "max_output_tokens": spec.max_output_tokens,
        })
    return catalog


def ui_editable() -> bool:
    """Allow UI writes only in local/test mode without a gateway secret."""
    settings = get_settings()
    return (
        settings.runtime_mode in {"desktop", "development", "test"}
        and settings.gateway_auth_secret is None
    )


def _uses_system_credential_store(settings=None) -> bool:
    current = settings or get_settings()
    return not SOURCE_LAYOUT and current.runtime_mode == "desktop"


def _open_system_credential_store() -> credentials.SystemCredentialStore:
    """Narrow seam for hermetic settings transaction tests."""
    return credentials.SystemCredentialStore()


def _credential_storage_state(settings) -> dict:
    if settings.runtime_mode == "server" or settings.gateway_auth_secret is not None:
        return credentials.server_environment_status().to_dict()
    if _uses_system_credential_store(settings):
        return credentials.current_system_status().to_dict()
    return credentials.configuration_file_status().to_dict()


def _read_state_locked() -> dict:
    """Return one revision-consistent snapshot under ``_SETTINGS_LOCK``."""
    editable = ui_editable()
    settings = get_settings()
    model_provider = (
        provider_spec(settings.llm_model.partition(":")[0].strip())
        if settings.llm_model
        else None
    )
    env_file = prepare_private_file(env_file_path(), private_parent=False, create=False)
    compatible_endpoint = resolve_openai_compatible_endpoint()
    capabilities = _effective_model_capabilities(
        settings.llm_model,
        context_window=settings.llm_context_window,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    return {
        "editable": editable,
        "llm_model": settings.llm_model,
        "llm_model_local": bool(model_provider and model_provider.is_local) if settings.llm_model else None,
        "llm_capabilities": capabilities,
        "keys": {name: bool(os.environ.get(name, "").strip()) for name in ALLOWED_KEY_VARS} if editable else {},
        "credential_storage": _credential_storage_state(settings),
        "providers": _provider_catalog() if editable else [],
        "outbound_policy": {
            field: getattr(settings, field)
            for field in _OUTBOUND_POLICY_ENVS
        },
        "environment_managed": {
            "llm_model": externally_managed_environment_variable(_MODEL_ENV),
            "llm_capabilities": {
                field: externally_managed_environment_variable(env_name)
                for field, env_name in _CAPABILITY_ENVS.items()
            },
            "keys": {
                name: externally_managed_environment_variable(name)
                for name in ALLOWED_KEY_VARS
            } if editable else {},
            "outbound_policy": {
                field: externally_managed_environment_variable(env_name)
                for field, env_name in _OUTBOUND_POLICY_ENVS.items()
            },
        },
        "openai_compatible_endpoint": {
            "status": compatible_endpoint.status,
            "url": compatible_endpoint.url,
            "source": compatible_endpoint.source,
            "externally_managed": compatible_endpoint.externally_managed,
            "issue": compatible_endpoint.issue,
        },
        "revision": _revision_locked(env_file),
        "persistence_warning": _persistence_warning_locked(env_file),
    }


def _effective_model_capabilities(
    model_string: str | None,
    *,
    context_window: int | None,
    max_output_tokens: int | None,
) -> dict:
    """Resolve explicit model-bound values before exact provider-default metadata."""
    if model_string is None:
        return {
            "context_window": None,
            "max_output_tokens": None,
            "source": None,
        }
    if context_window is not None and max_output_tokens is not None:
        return {
            "context_window": context_window,
            "max_output_tokens": max_output_tokens,
            "source": "configured",
        }
    provider_context, provider_output = resolve_model_capabilities(
        model_string,
        context_window=None,
        max_output_tokens=None,
    )
    if provider_context is not None and provider_output is not None:
        return {
            "context_window": provider_context,
            "max_output_tokens": provider_output,
            "source": "provider",
        }
    return {
        "context_window": None,
        "max_output_tokens": None,
        "source": "missing",
    }


def read_state() -> dict:
    """Return a revision-consistent snapshot without credential values."""
    with _SETTINGS_LOCK:
        return _read_state_locked()


def _normalize_model_string(model_string: str) -> str:
    """Normalize a model reference for persistence and runtime use."""
    provider, separator, name = model_string.partition(":")
    provider = provider.strip()
    name = name.strip()
    if not provider:
        raise ValueError("模型串格式是「厂商:型号」（如 deepseek:deepseek-chat）或裸厂商名（如 anthropic）。")
    normalized = f"{provider}:{name}" if separator and name else provider
    try:
        validate_model_reference(normalized)
    except Exception as e:
        raise ValueError(f"模型串不可用：{e}") from e
    return normalized


def _required_key_vars(model_string: str | None) -> tuple[str, ...]:
    """Return acceptable credential variables; any one value is sufficient."""
    if not model_string:
        return ()
    provider = model_string.partition(":")[0].strip()
    spec = provider_spec(provider)
    if spec is None:
        return ()  # Invalid providers receive a better error during normalization.
    if spec.is_local or not spec.key_envs:
        return ()
    return spec.key_envs


def _reject_control_characters(value: str, *, field: str) -> None:
    """Reject NUL and invisible C0/DEL characters unsafe for environment values."""
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{field} 不能包含控制字符。")


def _reject_environment_managed_changes(names: list[str]) -> None:
    """Reject values that the shell/container will override at next startup."""
    if not names:
        return
    listed = " / ".join(names)
    raise ValueError(
        f"{listed} 由 CareerDesk 启动前的 shell/容器环境托管，本次未保存。"
        "请完全停止 CareerDesk，在启动环境中修改或移除该变量，再重新启动。"
    )


def _read_env_bytes(env_file: Path) -> bytes:
    """Read an existing env file without following its final symlink."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(env_file, flags)
    except FileNotFoundError:
        return b""
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(env_file)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not os.path.samestat(opened, current)
        ):
            raise ValueError(f"敏感文件必须是稳定的单链接普通文件：{env_file}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _stage_env_update(
    env_file: Path,
    *,
    model_given: bool,
    model_value: str | None,
    capability_updates: dict[str, int | None],
    keys: dict[str, str | None],
    outbound_policy: dict[str, bool] | None,
    timezone_value: str | None = None,
) -> Path:
    """Stage the complete change in a private sibling without touching the target."""
    original = _read_env_bytes(env_file)
    # A packaged build keeps this file in a managed config directory that nothing else
    # creates. Create it only when absent: in a source layout the parent is the
    # repository root, which must never be created or hardened from here.
    if not env_file.parent.exists():
        ensure_private_directory(env_file.parent)
    descriptor, staged_name = tempfile.mkstemp(
        dir=env_file.parent,
        prefix=f".{env_file.name}.staged-",
    )
    staged = Path(staged_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(original)
            destination.flush()
            os.fsync(destination.fileno())
        if model_given:
            set_key(staged, _MODEL_ENV, model_value or "")
        if timezone_value is not None:
            set_key(staged, _TIMEZONE_ENV, timezone_value)
        for field, value in capability_updates.items():
            env_name = _CAPABILITY_ENVS[field]
            if value is None:
                unset_key(staged, env_name)
            else:
                set_key(staged, env_name, str(value))
        for name, value in keys.items():
            if value is None:
                unset_key(staged, name)
            else:
                set_key(staged, name, value)
        if outbound_policy is not None:
            for field, value in outbound_policy.items():
                set_key(staged, _OUTBOUND_POLICY_ENVS[field], "true" if value else "false")
        # Windows FlushFileBuffers requires a writable handle; POSIX allows fsync
        # on any descriptor. O_RDWR is the portable choice.
        staged_descriptor = os.open(staged, os.O_RDWR)
        try:
            if os.name == "posix":
                os.fchmod(staged_descriptor, 0o600)
            os.fsync(staged_descriptor)
        finally:
            os.close(staged_descriptor)
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def sync_system_timezone(timezone_name: str) -> dict[str, str]:
    """Persist a browser-detected IANA timezone for one local app instance.

    Installed desktop instances are single-user and keep this setting in their
    private ``settings.env``.  An explicit process/container environment value
    remains authoritative and is never shadowed by a browser request.
    """
    global _PERSISTENCE_WARNING

    normalized = timezone_name.strip()
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("系统返回了无法识别的时区。") from error

    with _SETTINGS_LOCK:
        current_settings = get_settings()
        if externally_managed_environment_variable(_TIMEZONE_ENV):
            return {
                "status": "external_override",
                "timezone": current_settings.timezone,
            }
        if normalized == current_settings.timezone:
            return {"status": "unchanged", "timezone": normalized}

        env_file = prepare_private_file(env_file_path(), private_parent=False, create=False)
        staged = _stage_env_update(
            env_file,
            model_given=False,
            model_value=None,
            capability_updates={},
            keys={},
            outbound_policy=None,
            timezone_value=normalized,
        )
        try:
            os.replace(staged, env_file)
        finally:
            staged.unlink(missing_ok=True)

        persistence_warning = None
        try:
            _fsync_directory(env_file.parent)
        except OSError:
            persistence_warning = (
                "系统时区已在当前进程生效，但操作系统未能确认配置目录已持久化；"
                "请检查磁盘或文件系统状态。"
            )

        os.environ[_TIMEZONE_ENV] = normalized
        _advance_revision_locked(env_file)
        _PERSISTENCE_WARNING = (
            (env_file, persistence_warning)
            if persistence_warning is not None
            else None
        )
        get_settings.cache_clear()
        return {"status": "updated", "timezone": normalized}


def _fsync_directory(directory: Path) -> None:
    """Persist a replaced directory entry on POSIX systems that support fsync."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_system_credentials(
    store: credentials.SystemCredentialStore,
    snapshot: dict[str, str | None],
) -> None:
    """Best-effort compensation for a settings commit that did not complete."""
    try:
        for name, value in snapshot.items():
            if value is None:
                store.delete(name)
            else:
                store.set(name, value)
    except credentials.CredentialStoreUnavailable as error:
        uncertain = credentials.CredentialStoreUnavailable(
            "系统凭据更新状态无法确认；请重启 CareerDesk 并在设置页核对后再操作。"
        )
        credentials.unavailable_status(uncertain)
        raise uncertain from error


def _apply_system_credentials(
    store: credentials.SystemCredentialStore,
    updates: dict[str, str | None],
) -> dict[str, str | None]:
    """Apply verified keyring mutations and compensate any in-process failure."""
    snapshot: dict[str, str | None] = {}
    try:
        snapshot = {name: store.get(name) for name in updates}
        for name, value in updates.items():
            if value is None:
                store.delete(name)
            else:
                store.set(name, value)
    except credentials.CredentialStoreUnavailable as error:
        # No mutation begins until every previous value has been captured.
        if len(snapshot) == len(updates):
            _restore_system_credentials(store, snapshot)
        credentials.unavailable_status(error)
        raise
    return snapshot


def save(
    *,
    expected_revision: str,
    model_given: bool,
    llm_model: str | None,
    keys: dict[str, str | None],
    outbound_policy: dict[str, bool] | None = None,
    capabilities_given: bool = False,
    llm_capabilities: dict[str, int | None] | None = None,
) -> dict:
    """Atomically commit disk, process config, and revision under CAS and lock."""
    global _PERSISTENCE_WARNING
    with _SETTINGS_LOCK:
        env_file = prepare_private_file(env_file_path(), private_parent=False, create=False)
        if expected_revision != _revision_locked(env_file):
            raise SettingsRevisionConflict("设置已被另一个窗口修改，本次保存未生效。")

        current_settings = get_settings()
        cleaned: dict[str, str | None] = {}
        for name, value in keys.items():
            if name not in ALLOWED_KEY_VARS:
                raise ValueError(f"不支持经设置页写入的变量：{name}")
            raw_value = value or ""
            _reject_control_characters(raw_value, field=f"{name} 的值")
            value = raw_value.strip()
            if len(value) > _MAX_VALUE_LENGTH:
                raise ValueError(f"{name} 的值过长（超过 {_MAX_VALUE_LENGTH} 字符），像是贴错了内容。")
            cleaned[name] = value or None

        model_value: str | None = None
        if model_given:
            raw_model = llm_model or ""
            _reject_control_characters(raw_model, field="模型串")
            raw_model = raw_model.strip()
            model_value = _normalize_model_string(raw_model) if raw_model else None

        current_capabilities = {
            "context_window": current_settings.llm_context_window,
            "max_output_tokens": current_settings.llm_max_output_tokens,
        }
        capability_values = dict(current_capabilities)
        if capabilities_given:
            if llm_capabilities is None or set(llm_capabilities) != set(_CAPABILITY_ENVS):
                raise ValueError("llm_capabilities 必须完整包含 context_window 与 max_output_tokens。")
            capability_values = dict(llm_capabilities)
        elif model_given and model_value != current_settings.llm_model:
            # Capacity values describe exactly one model.  A model switch never
            # inherits stale numbers unless the same request supplies a fresh pair.
            capability_values = {field: None for field in _CAPABILITY_ENVS}

        context_window = capability_values["context_window"]
        max_output_tokens = capability_values["max_output_tokens"]
        if (context_window is None) != (max_output_tokens is None):
            raise ValueError("context window 与 max output tokens 必须同时填写或同时清除。")
        if context_window is not None:
            if (
                type(context_window) is not int
                or type(max_output_tokens) is not int
                or context_window < 1_024
                or max_output_tokens < 256
                or max_output_tokens > context_window
            ):
                raise ValueError(
                    "模型容量必须是有效整数，context window 至少 1024、max output tokens 至少 256，"
                    "且最大输出不能大于上下文窗口。"
                )

        cleaned_policy: dict[str, bool] | None = None
        if outbound_policy is not None:
            expected = set(_OUTBOUND_POLICY_ENVS)
            if set(outbound_policy) != expected:
                raise ValueError("outbound_policy 必须完整包含全部受支持的布尔字段。")
            if any(type(value) is not bool for value in outbound_policy.values()):
                raise ValueError("outbound_policy 的值必须是布尔值。")
            cleaned_policy = dict(outbound_policy)

        # Treat only effective value changes as updates. This avoids touching the
        # env file on no-op commits or shadowing environment-managed values.
        model_changed = model_given and model_value != current_settings.llm_model
        capability_updates = {
            field: value
            for field, value in capability_values.items()
            if value != current_capabilities[field]
        }
        key_updates = {
            name: value
            for name, value in cleaned.items()
            if value != (os.environ.get(name, "").strip() or None)
        }
        policy_changes = (
            {
                field: value
                for field, value in cleaned_policy.items()
                if value != getattr(current_settings, field)
            }
            if cleaned_policy is not None
            else None
        )

        managed_changes: list[str] = []
        if model_changed and externally_managed_environment_variable(_MODEL_ENV):
            managed_changes.append(_MODEL_ENV)
        capability_management_fields = (
            _CAPABILITY_ENVS
            if model_changed
            else capability_updates
        )
        managed_changes.extend(
            _CAPABILITY_ENVS[field]
            for field in capability_management_fields
            if externally_managed_environment_variable(_CAPABILITY_ENVS[field])
        )
        managed_changes.extend(
            name for name in key_updates
            if externally_managed_environment_variable(name)
        )
        managed_changes.extend(
            _OUTBOUND_POLICY_ENVS[field]
            for field in (policy_changes or {})
            if externally_managed_environment_variable(_OUTBOUND_POLICY_ENVS[field])
        )
        _reject_environment_managed_changes(managed_changes)
        # When policy changes, persist a complete unmanaged snapshot per the API
        # contract, skipping equal environment-managed values to avoid shadows.
        policy_updates = (
            {
                field: value
                for field, value in cleaned_policy.items()
                if not externally_managed_environment_variable(_OUTBOUND_POLICY_ENVS[field])
            }
            if policy_changes
            else None
        )

        effective_model = model_value if model_given else current_settings.llm_model
        effective_capabilities = _effective_model_capabilities(
            effective_model,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        )
        if effective_model is None and context_window is not None:
            raise ValueError("未配置模型时不能单独保存模型容量。")
        if (model_changed or capabilities_given) and effective_model is not None:
            if effective_capabilities["source"] == "missing":
                raise ValueError(
                    "该型号没有可信的容量元数据；请同时填写 context window 与 max output tokens，"
                    "数值以该型号服务端或官方配置为准。"
                )
        strict_offline = (
            cleaned_policy["strict_offline"]
            if cleaned_policy is not None
            else current_settings.strict_offline
        )
        required_keys = _required_key_vars(effective_model)
        if required_keys and not strict_offline:
            has_key = any(
                bool((cleaned.get(name, os.environ.get(name)) or "").strip())
                for name in required_keys
            )
            if not has_key:
                provider = (effective_model or "").partition(":")[0].strip()
                label = _PROVIDER_LABELS.get(provider, provider)
                choices = " / ".join(required_keys)
                raise ValueError(f"{label} 需要配置 {choices} 中的任意一项；请和模型一起保存。")

        if (
            effective_model
            and effective_model.partition(":")[0].strip() == "openai_compatible"
            and not strict_offline
        ):
            endpoint = resolve_openai_compatible_endpoint()
            if endpoint.status == "missing":
                raise ValueError(
                    "通用 OpenAI 兼容接口需要配置 OPENAI_BASE_URL 或 LLM_BASE_URL；"
                    "Agentmaker 按此顺序取第一个非空值。"
                )
            if endpoint.status == "invalid":
                raise ValueError(
                    f"通用 OpenAI 兼容接口的 {endpoint.source} 无效：{endpoint.issue}。"
                    "只允许带 host 和可选路径的 http(s) URL，不允许 userinfo、query 或 fragment。"
                )

        if not model_changed and not capability_updates and not key_updates and not policy_updates:
            return _read_state_locked()

        use_system_credentials = _uses_system_credential_store(current_settings)
        config_changed = bool(
            model_changed
            or capability_updates
            or policy_updates
            or (key_updates and not use_system_credentials)
        )
        staged = (
            _stage_env_update(
                env_file,
                model_given=model_changed,
                model_value=model_value,
                capability_updates=capability_updates,
                keys={} if use_system_credentials else key_updates,
                outbound_policy=policy_updates,
            )
            if config_changed
            else None
        )
        store = None
        credential_snapshot: dict[str, str | None] | None = None
        try:
            if use_system_credentials and key_updates:
                store = _open_system_credential_store()
                credential_snapshot = _apply_system_credentials(store, key_updates)
                credentials.available_status(store)
            if staged is not None:
                os.replace(staged, env_file)
        except credentials.CredentialStoreUnavailable as error:
            if store is not None and credential_snapshot is not None:
                _restore_system_credentials(store, credential_snapshot)
            credentials.unavailable_status(error)
            raise
        except BaseException:
            if store is not None and credential_snapshot is not None:
                _restore_system_credentials(store, credential_snapshot)
            raise
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

        persistence_warning = None
        try:
            if config_changed:
                _fsync_directory(env_file.parent)
        except OSError:
            # Rename already committed atomically, so the save cannot honestly be
            # reported as absent. Keep process state aligned with disk and expose
            # uncertain crash durability until a later directory fsync succeeds.
            persistence_warning = (
                "设置已在当前进程生效，但操作系统未能确认目录项已持久化；"
                "请检查磁盘或文件系统状态，确认后再重启 CareerDesk。"
            )

        if model_changed:
            os.environ[_MODEL_ENV] = model_value or ""
        for field, value in capability_updates.items():
            env_name = _CAPABILITY_ENVS[field]
            if value is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = str(value)
        for name, value in key_updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if policy_updates is not None:
            for field, value in policy_updates.items():
                os.environ[_OUTBOUND_POLICY_ENVS[field]] = "true" if value else "false"

        _advance_revision_locked(env_file)
        _PERSISTENCE_WARNING = (
            (env_file, persistence_warning)
            if persistence_warning is not None
            else None
        )
        get_settings.cache_clear()
        return _read_state_locked()
