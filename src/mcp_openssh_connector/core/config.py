"""Настройки сервера: таймауты, пути, пороги кэша.

Значения берутся из окружения с префиксом `OPENSSH_MCP_`. Пути к состоянию
живут в `XDG_RUNTIME_DIR` (tmpfs, права 0700), а без него — во временном
каталоге: состояние эфемерно и переживать перезагрузку не обязано.
"""

import os
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Поля настроек с дефолтами; смысл каждого — в комментарии рядом."""

    model_config = SettingsConfigDict(env_prefix="OPENSSH_MCP_")

    # ── таймауты проб (секунды) ──────────────────────────────────────────────
    connect_timeout: float = 1.0  # прямой TCP-коннект и ConnectTimeout у ssh
    ssh_g_timeout: float = 2.0  # разбор конфига одного хоста через `ssh -G`
    jump_timeout: float = 4.0  # ssh к jump-хосту и пробы за ним
    deep_timeout: float = 6.0  # реальный вход `ssh ... true`

    # ── выполнение команд ────────────────────────────────────────────────────
    run_timeout: float = 60.0  # дефолтный таймаут `run`, если вызов не задал свой
    max_command_timeout: float = 3600.0  # потолок клиентского таймаута `run`
    max_wait: float = 3600.0  # потолок клиентского `wait` в `job`
    output_limit: int = 1_000_000  # байт stdout/stderr, дальше — обрезаем
    control_persist: int = 60  # секунды жизни мастер-соединения после последней команды

    # ── кэш статусов ─────────────────────────────────────────────────────────
    cache_ttl: float = 900.0  # секунды; старше — list_hosts перемеряет сам

    # ── llms.txt ─────────────────────────────────────────────────────────────
    llms_timeout: float = 20.0  # секунды на один HTTP-запрос
    llms_cache_ttl: float = 86400.0  # секунды жизни скачанного в кэше на диске
    llms_max_bytes: int = 64_000_000  # потолок одного скачивания (full бывает 40 МБ)
    llms_page_chars: int = 20_000  # символов страницы за один llms_fetch
    llms_hit_chars: int = 2_000  # символов одного совпадения в llms_search
    llms_max_hits: int = 20  # совпадений за один llms_search
    llms_status_ttl: float = 86400.0  # секунды, пока проверка источников считается свежей
    # Кэш скачанного — не в runtime-каталоге (tmpfs), а в XDG_CACHE_HOME.
    llms_cache_dir: Path = Field(
        default_factory=lambda: (
            Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "mcp-openssh-connector" / "llms"
        )
    )

    # Пользовательские источники llms.txt — переживают перезапуск: XDG_DATA_HOME.
    llms_sources_file: Path = Field(
        default_factory=lambda: (
            Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
            / "mcp-openssh-connector"
            / "llms-sources.json"
        )
    )

    # Каталог с паролями sudo: `<alias>.secret`, права 0600.
    secret_dir: Path = Field(default_factory=lambda: Path.home() / ".ssh")

    # Хосты, где sudo требует терминал: для них ssh идёт с `-tt`.
    # В окружении — JSON-список: OPENSSH_MCP_PTY_HOSTS='["a","b"]'.
    pty_hosts: frozenset[str] = frozenset()

    @property
    def state_dir(self) -> Path:
        """Каталог состояния: в runtime-каталоге, а без него — во временном."""
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        return runtime / "mcp-openssh-connector"

    @property
    def cache_file(self) -> Path:
        """Файл кэша статусов доступности."""
        return self.state_dir / "hosts-status.json"

    @property
    def llms_status_file(self) -> Path:
        """Итоги проверки источников llms.txt: живут до перезагрузки машины."""
        return self.state_dir / "llms-sources-status.json"

    @property
    def control_dir(self) -> Path:
        """Каталог сокетов ControlMaster."""
        return self.state_dir / "control"

    def secret_file(self, alias: str) -> Path:
        """Файл с паролем sudo для алиаса."""
        return self.secret_dir / f"{alias}.secret"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Единственный экземпляр настроек; окружение читается один раз."""
    return Settings()
