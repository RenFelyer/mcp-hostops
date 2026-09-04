"""Настройки сервера: таймауты, пороги, пути к состоянию.

Значения берутся из окружения с префиксом `OPENSSH_MCP_`. Состояние лежит в
трёх местах по сроку жизни: runtime-каталог (сокеты ControlMaster, кэш статусов
хостов, итоги проверки источников llms — до перезагрузки), кэш (скачанное по
`llms.txt`, бывает десятками мегабайт) и данные (пользовательские источники
llms — терять нельзя). Окружение читается при создании настроек; runtime-каталог
берётся из `XDG_RUNTIME_DIR` при обращении. Неизменяемое — в `constants`.
"""

import os
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import APP_NAME


def _xdg(var: str, fallback: Path) -> Path:
    return Path(os.environ.get(var) or fallback) / APP_NAME


def _runtime_dir() -> Path:
    """Runtime-каталог: `XDG_RUNTIME_DIR`, а без него — временный, свой для uid.

    Общий `/tmp` без uid в имени позволил бы другому пользователю подсунуть
    каталог под наши сокеты; проверка владельца — в `store.private_dir`.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / APP_NAME
    return Path(tempfile.gettempdir()) / f"{APP_NAME}-{os.getuid()}"


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
    max_command_timeout: float = 3600.0  # потолок клиентского таймаута `run`; больше — ошибка вызова
    max_wait: float = 3600.0  # потолок клиентского `wait` в `job`; больше — ждём столько
    output_limit: int = 1_000_000  # байт stdout/stderr, дальше — обрезаем
    control_persist: int = 60  # секунды жизни мастер-соединения после последней команды
    job_history: int = 50  # сколько завершённых задач помнить; старше — забываются

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

    # ── пути ─────────────────────────────────────────────────────────────────
    # Кэш — скачанное по llms.txt; данные — пользовательские источники llms.
    # Runtime-каталог не настраивается: см. `state_dir`.
    llms_cache_dir: Path = Field(default_factory=lambda: _xdg("XDG_CACHE_HOME", Path.home() / ".cache") / "llms")
    llms_sources_file: Path = Field(
        default_factory=lambda: _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share") / "llms-sources.json"
    )
    # Каталог с паролями sudo: `<alias>.secret`, права 0600.
    secret_dir: Path = Field(default_factory=lambda: Path.home() / ".ssh")

    # Хосты, где sudo требует терминал: для них ssh идёт с `-tt`.
    # В окружении — JSON-список: OPENSSH_MCP_PTY_HOSTS='["a","b"]'.
    pty_hosts: frozenset[str] = frozenset()

    # Файл отладочного лога; без него лог выключен (см. `logger`).
    debug_log: Path | None = None

    @property
    def state_dir(self) -> Path:
        """Каталог состояния до перезагрузки: `XDG_RUNTIME_DIR`, иначе временный с uid."""
        return _runtime_dir()

    @property
    def cache_file(self) -> Path:
        """Файл кэша статусов доступности."""
        return self.state_dir / "hosts-status.json"

    @property
    def llms_status_file(self) -> Path:
        """Итоги проверки источников llms.txt."""
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
    """Единственный экземпляр настроек."""
    return Settings()
