"""Схемы роутера управления ~/.ssh/config."""

from pydantic import BaseModel, Field

from ...core.schemas import Host


class ManagedHost(BaseModel):
    """Желаемый Host-блок для managed-файла; из него собирается канонический текст."""

    alias: str
    hostname: str
    user: str = ""
    port: int = 22
    identity_file: str = ""
    proxy_jump: str = Field(default="", description="алиас jump-хоста")
    extra: dict[str, str] = Field(default_factory=dict, description="прочие опции ssh, «Ключ Значение»")


class AddHostResult(BaseModel):
    """Ответ add_host: что записано и как хост теперь виден ssh."""

    alias: str
    config_file: str = Field(description="managed-файл, куда записан Host-блок")
    include_added: bool = Field(description="в основной конфиг добавлена строка Include на managed-файл")
    host: Host | None = Field(description="хост глазами ssh -G после записи; null — ssh -G не разобрал")


class RemoveHostResult(BaseModel):
    """Ответ remove_host: что вычищено вместе с Host-блоком."""

    alias: str
    known_hosts_removed: int = Field(description="удалённых записей known_hosts")
    secret_removed: bool = Field(description="удалён ли файл ~/.ssh/<alias>.secret")


class ForgetHostResult(BaseModel):
    """Ответ forget_host: очистка known_hosts без правки config."""

    target: str = Field(description="имя хоста, по которому чистили known_hosts")
    known_hosts_file: str
    removed: int = Field(description="удалённых записей")


class CopyIdResult(BaseModel):
    """Ответ copy_id: установка публичного ключа на хост."""

    alias: str
    ok: bool = Field(description="ключ установлен (код возврата 0)")
    detail: str = Field(description="последняя строка вывода ssh-copy-id или причина отказа")
