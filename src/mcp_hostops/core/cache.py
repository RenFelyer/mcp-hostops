"""Cache of host availability statuses: a timestamped JSON file in the runtime directory.

The cache is an optimization: on any I/O error or garbage in the file, the server
simply measures again.
"""

import contextlib
from collections.abc import Mapping

from pydantic import TypeAdapter, ValidationError

from . import store
from .config.environment import Settings
from .schemas import Availability

_HOSTS = TypeAdapter(dict[str, Availability])


def read(s: Settings) -> tuple[float, dict[str, Availability]]:
    """Read the cache.

    Returns:
        The record's age in seconds and statuses by alias. Age `inf` and empty statuses
        mean there's no cache, it's corrupt, or it holds an unrecognized status value.
    """
    age, data = store.load_stamped(s.cache_file)
    try:
        return age, _HOSTS.validate_python(data.get("hosts"))
    except ValidationError:
        return float("inf"), {}


def write(statuses: Mapping[str, Availability], s: Settings) -> None:
    """Write statuses; an I/O error is silently swallowed."""
    with contextlib.suppress(OSError):
        store.save_stamped(s.cache_file, {"hosts": dict(statuses)})
