"""Installed-desktop credentials backed only by recommended OS keyrings.

Source checkouts keep their existing private ``.env`` workflow, and server
deployments keep using platform-injected environment secrets.  This adapter is
for the installed desktop only: it never operates through null, fail, chained,
or third-party backends instead of silently falling back to a plaintext file.

Storage layout: Windows and Linux keep one keyring item per credential name.
macOS keeps every credential inside one JSON record because the Keychain asks
for per-item authorization per app build; a single record means at most one
prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any, MutableMapping

from dotenv import dotenv_values
import keyring

from .ai.providers import provider_specs


SERVICE_NAME = "com.careerdesk.desktop.credentials.v1"
MACOS_SERVICE_NAME = "com.careerdesk.desktop.credentials.v2"
MACOS_ACCOUNT_NAME = "credentials"
_SEARCH_CREDENTIAL_NAMES = (
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "GOOGLE_PSE_API_KEY",
    "GOOGLE_PSE_ENGINE_ID",
    "SEARXNG_BASE_URL",
)
SUPPORTED_CREDENTIAL_NAMES = tuple(dict.fromkeys((
    *(
        credential
        for spec in provider_specs()
        for credential in spec.key_envs
    ),
    *_SEARCH_CREDENTIAL_NAMES,
)))

_BACKEND_MODULES = {
    "darwin": {
        "keyring.backends.macOS": "macOS Keychain",
    },
    "win32": {
        "keyring.backends.Windows": "Windows Credential Locker",
    },
    "linux": {
        "keyring.backends.SecretService": "Linux Secret Service",
        "keyring.backends.kwallet": "KDE KWallet",
    },
}


class CredentialStoreUnavailable(ValueError):
    """The installed desktop cannot safely access a supported OS keyring."""


@dataclass(frozen=True, slots=True)
class CredentialStoreStatus:
    """Non-sensitive status exposed to the Settings UI."""

    kind: str
    available: bool
    label: str
    issue: str | None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "kind": self.kind,
            "available": self.available,
            "label": self.label,
            "issue": self.issue,
        }


def _unavailable_message(platform_name: str) -> str:
    if platform_name == "linux":
        return (
            "系统凭据存储不可用；请确认当前桌面会话已启动并解锁 Secret Service "
            "或 KWallet，然后重启 CareerDesk。"
        )
    return "系统凭据存储不可用或已锁定；请解锁系统登录凭据库后重启 CareerDesk。"


_CORRUPT_RECORD_MESSAGE = (
    "系统凭据存储中的 CareerDesk 记录无法解析；请在“钥匙串访问”中删除服务为 "
    f"{MACOS_SERVICE_NAME} 的记录，然后重新在设置页保存密钥。"
)


def _parse_record(raw: str) -> dict[str, str]:
    """Parse the single macOS record without echoing its content in errors."""
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise CredentialStoreUnavailable(_CORRUPT_RECORD_MESSAGE) from error
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and key and isinstance(value, str) and value
        for key, value in parsed.items()
    ):
        raise CredentialStoreUnavailable(_CORRUPT_RECORD_MESSAGE)
    return parsed


class SystemCredentialStore:
    """Small verified wrapper around one platform-supported keyring backend."""

    def __init__(self, backend: Any | None = None, *, platform_name: str | None = None):
        self._platform = platform_name or sys.platform
        allowed = _BACKEND_MODULES.get(self._platform, {})
        try:
            selected = keyring.get_keyring() if backend is None else backend
            # keyring selects ChainerBackend when more than one viable backend
            # exists.  Never use its cross-backend read/write/delete semantics:
            # choose its highest-priority native child and ignore all others.
            # This keeps Linux desktops with both Secret Service and KWallet
            # usable without admitting a chained or third-party file backend.
            if type(selected).__module__ == "keyring.backends.chainer":
                selected = next(
                    (
                        candidate
                        for candidate in selected.backends
                        if type(candidate).__module__ in allowed
                    ),
                    selected,
                )
            self._backend = selected
            priority = selected.priority
        except Exception as error:  # noqa: BLE001 - third-party backend init is untrusted
            raise CredentialStoreUnavailable(
                _unavailable_message(self._platform)
            ) from error

        module = type(self._backend).__module__
        if module not in allowed or not isinstance(priority, (int, float)) or priority < 1:
            raise CredentialStoreUnavailable(_unavailable_message(self._platform))
        self.label = allowed[module]
        self._single_record = self._platform == "darwin"
        self._record_cache: dict[str, str] | None = None

    def _backend_get(self, service: str, account: str) -> str | None:
        try:
            return self._backend.get_password(service, account)
        except Exception as error:  # noqa: BLE001 - backend errors vary by platform
            raise CredentialStoreUnavailable(
                _unavailable_message(self._platform)
            ) from error

    def _backend_set(self, service: str, account: str, value: str) -> None:
        try:
            self._backend.set_password(service, account, value)
        except Exception as error:  # noqa: BLE001 - backend errors vary by platform
            raise CredentialStoreUnavailable(
                _unavailable_message(self._platform)
            ) from error

    def _backend_delete(self, service: str, account: str) -> None:
        try:
            self._backend.delete_password(service, account)
        except Exception as error:  # noqa: BLE001 - backend errors vary by platform
            raise CredentialStoreUnavailable(
                _unavailable_message(self._platform)
            ) from error

    def _load_record(self) -> dict[str, str]:
        """Return the cached macOS record, reading it at most once per store."""
        if self._record_cache is None:
            raw = self._backend_get(MACOS_SERVICE_NAME, MACOS_ACCOUNT_NAME)
            self._record_cache = {} if raw is None else _parse_record(raw)
        return self._record_cache

    def _store_record(self, record: dict[str, str]) -> None:
        """Persist the macOS record, verify it, and refresh the cache."""
        self._record_cache = None
        if record:
            payload = json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            self._backend_set(MACOS_SERVICE_NAME, MACOS_ACCOUNT_NAME, payload)
            persisted = self._backend_get(MACOS_SERVICE_NAME, MACOS_ACCOUNT_NAME)
            if persisted is None or _parse_record(persisted) != record:
                raise CredentialStoreUnavailable(
                    "系统凭据存储未能确认写入；本次保存未完成，请重试。"
                )
        else:
            if self._backend_get(MACOS_SERVICE_NAME, MACOS_ACCOUNT_NAME) is not None:
                self._backend_delete(MACOS_SERVICE_NAME, MACOS_ACCOUNT_NAME)
                if self._backend_get(MACOS_SERVICE_NAME, MACOS_ACCOUNT_NAME) is not None:
                    raise CredentialStoreUnavailable(
                        "系统凭据存储未能确认删除；本次保存未完成，请重试。"
                    )
        self._record_cache = dict(record)

    def get(self, name: str) -> str | None:
        _validate_name(name)
        if self._single_record:
            return self._load_record().get(name)
        value = self._backend_get(SERVICE_NAME, name)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise CredentialStoreUnavailable(_unavailable_message(self._platform))
        return value

    def set(self, name: str, value: str) -> None:
        _validate_name(name)
        if not isinstance(value, str) or not value:
            raise ValueError("凭据值不能为空")
        if self._single_record:
            record = dict(self._load_record())
            record[name] = value
            self._store_record(record)
            return
        self._backend_set(SERVICE_NAME, name, value)
        persisted = self._backend_get(SERVICE_NAME, name)
        if persisted != value:
            raise CredentialStoreUnavailable(
                "系统凭据存储未能确认写入；本次保存未完成，请重试。"
            )

    def delete(self, name: str) -> None:
        _validate_name(name)
        if self._single_record:
            record = dict(self._load_record())
            if name not in record:
                return
            del record[name]
            self._store_record(record)
            return
        if self.get(name) is None:
            return
        self._backend_delete(SERVICE_NAME, name)
        if self.get(name) is not None:
            raise CredentialStoreUnavailable(
                "系统凭据存储未能确认删除；本次保存未完成，请重试。"
            )


def _validate_name(name: str) -> None:
    if name not in SUPPORTED_CREDENTIAL_NAMES:
        raise ValueError(f"不支持的凭据变量：{name}")


_LAST_STATUS: CredentialStoreStatus | None = None


def _set_status(status: CredentialStoreStatus) -> CredentialStoreStatus:
    global _LAST_STATUS
    _LAST_STATUS = status
    return status


def available_status(store: SystemCredentialStore) -> CredentialStoreStatus:
    return _set_status(CredentialStoreStatus(
        kind="system",
        available=True,
        label=store.label,
        issue=None,
    ))


def unavailable_status(error: CredentialStoreUnavailable) -> CredentialStoreStatus:
    return _set_status(CredentialStoreStatus(
        kind="system",
        available=False,
        label="系统凭据存储",
        issue=str(error),
    ))


def current_system_status() -> CredentialStoreStatus:
    """Return the last startup result, or safely probe backend selection."""
    if _LAST_STATUS is not None:
        return _LAST_STATUS
    try:
        return available_status(SystemCredentialStore())
    except CredentialStoreUnavailable as error:
        return unavailable_status(error)


def reject_plaintext_credentials(config_file: Path) -> None:
    """Reject managed secrets in installed settings without modifying the file."""
    if not config_file.is_file():
        return
    try:
        values = dotenv_values(config_file)
    except Exception as error:  # noqa: BLE001 - malformed local config is untrusted
        raise RuntimeError("无法安全检查安装式配置文件中的凭据变量。") from error
    names = sorted(
        name
        for name in SUPPORTED_CREDENTIAL_NAMES
        if isinstance(values.get(name), str) and values[name].strip()
    )
    if names:
        listed = " / ".join(names)
        raise RuntimeError(
            f"安装式配置文件不能保存明文凭据：{listed}。"
            "文件未被修改；请移除这些行后，通过设置页写入系统凭据存储。"
        )


def inject_system_credentials(
    *,
    config_file: Path,
    environment: MutableMapping[str, str] | None = None,
) -> CredentialStoreStatus:
    """Load stored values before config/provider imports without overriding env."""
    reject_plaintext_credentials(config_file)
    target = os.environ if environment is None else environment
    injected: list[str] = []
    try:
        store = SystemCredentialStore()
        for name in SUPPORTED_CREDENTIAL_NAMES:
            if name in target:
                continue
            value = store.get(name)
            if value is not None:
                target[name] = value
                injected.append(name)
        return available_status(store)
    except CredentialStoreUnavailable as error:
        for name in injected:
            target.pop(name, None)
        return unavailable_status(error)


def configuration_file_status() -> CredentialStoreStatus:
    return CredentialStoreStatus(
        kind="configuration_file",
        available=True,
        label="私有配置文件",
        issue=None,
    )


def server_environment_status() -> CredentialStoreStatus:
    return CredentialStoreStatus(
        kind="server_environment",
        available=True,
        label="服务器环境变量/Secret",
        issue=None,
    )
