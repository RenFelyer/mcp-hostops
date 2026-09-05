"""Host availability: TCP port probing, and for jump hosts, probing from inside.

A direct host is checked with a TCP connect to its port, not a full login.
Hosts behind a ProxyJump have no direct route — they're probed from inside the
jump host with a single ssh call. The deep flag adds a real login (`ssh ...
true`): it answers not "is the port open" but "is the key accepted, are we
let in".

The logic is synchronous (socket and subprocess): the server calls it through
a thread so as not to block the event loop.
"""

import logging
import shlex
import socket
import subprocess
from collections import defaultdict
from collections.abc import Callable
from functools import partial
from time import sleep, time

from pydantic import TypeAdapter, ValidationError

from ..config.constants import PROBE_PATH_BUDGET, PROBE_PATH_PAUSE, PROBE_RETRY_ERRNOS
from ..config.environment import Settings
from ..schemas import Availability, Host
from .hosts import pairs, resolve
from .parallel import fan_out
from .ssh import run_sync, ssh_argv

log = logging.getLogger(__name__)

Statuses = dict[str, Availability]
_REPORT = TypeAdapter(Statuses)  # jump script output: "alias status" per line


def _reachable(host: Host, connect_timeout: float) -> bool:
    """Whether the port is open: a TCP connect, not a full authenticated login.

    A routing error is retried up to `PROBE_PATH_BUDGET` — on an overlay it
    only means "the path isn't up yet". A refusal or timeout isn't retried:
    that's the answer.
    """
    deadline = time() + PROBE_PATH_BUDGET
    while True:
        try:
            with socket.create_connection((host.hostname, host.port), connect_timeout):
                return True
        except OSError as err:
            if err.errno not in PROBE_RETRY_ERRNOS or time() >= deadline:
                return False
        sleep(PROBE_PATH_PAUSE)


def _jump_script(group: list[Host]) -> str:
    """Script that checks a group's ports on the jump host (needs bash and `timeout`).

    Prints one "alias status" line per host, using words from `Availability`.
    hostname, port, and alias are escaped with `shlex.quote`: config values
    must not be substituted into the remote bash as code.
    """
    checks = []
    for h in group:
        host_q = shlex.quote(h.hostname)
        port_q = shlex.quote(str(h.port))
        alias_q = shlex.quote(h.alias)
        checks.append(
            f'if timeout 1 bash -c \'exec 3<>/dev/tcp/"$0"/"$1"\' '
            f"{host_q} {port_q} 2>/dev/null; "
            f"then echo {alias_q} available; else echo {alias_q} unavailable; fi"
        )
    return "; ".join(checks)


def _probe_direct(host: Host, s: Settings) -> Statuses:
    return {host.alias: "available" if _reachable(host, s.connect_timeout) else "unavailable"}


def _probe_via(jump: Host, group: list[Host], s: Settings) -> Statuses:
    """A group behind one jump host: no direct route, so probe from inside its network.

    An unreachable jump means everything behind it is unreachable — that's a
    conclusion, not a guess. A TCP probe of the jump itself is possible only
    if it's direct: behind its own jump host, its port isn't visible from
    here, and the ssh call decides instead.
    """
    down: Statuses = dict.fromkeys((h.alias for h in group), "unavailable")
    # ssh to the jump host itself is affected by overlay laziness the same way,
    # but has no retry of its own: a cold path would drag the whole group
    # behind it into unavailable.
    if not jump.proxyjump and not _reachable(jump, s.connect_timeout):
        return down
    try:
        done = run_sync([*ssh_argv(jump, s), _jump_script(group)], s.jump_timeout)
    except (OSError, subprocess.SubprocessError):
        return down
    if done.returncode != 0:
        return down
    seen = pairs(done.stdout)
    try:
        return _REPORT.validate_python({h.alias: seen.get(h.alias) for h in group})
    except ValidationError:
        # The script is ours and prints a line for every alias; nothing else
        # should happen, but we must not guess a status from garbage.
        return dict.fromkeys((h.alias for h in group), "unknown")


def measure(hosts: list[Host], s: Settings) -> Statuses:
    """All probes at once; time is bounded by the probes' own timeouts.

    Returns:
        Status for each alias in `hosts`.
    """
    known = {h.alias: h for h in hosts}
    behind: dict[str, list[Host]] = defaultdict(list)
    for host in hosts:
        if host.proxyjump:
            behind[host.proxyjump].append(host)

    statuses: Statuses = {}
    probes: list[Callable[[], Statuses]] = [partial(_probe_direct, h, s) for h in hosts if not h.proxyjump]
    # The jump is usually described in the config itself — then it's already
    # resolved and a second `ssh -G` isn't needed. An unresolved jump makes
    # the whole group behind it unavailable.
    for alias, group in behind.items():
        via = known.get(alias) or resolve(alias, s.ssh_g_timeout)
        if via is None:
            statuses |= dict.fromkeys((h.alias for h in group), "unavailable")
        else:
            probes.append(partial(_probe_via, via, group, s))

    for result in fan_out(lambda probe: probe(), probes):
        statuses |= result
    log.debug("probes: %s", statuses)
    return statuses


def deep_check(host: Host, s: Settings) -> tuple[bool, str]:
    """A real login: `ssh ... true`.

    Returns:
        A pair of "was access granted" and a short failure reason; empty on success.
    """
    try:
        done = run_sync([*ssh_argv(host, s), "true"], s.deep_timeout)
    except subprocess.TimeoutExpired:
        return False, "login timed out"
    except OSError as err:
        return False, f"ssh failed to start: {err}"
    if done.returncode == 0:
        return True, ""
    reason = done.stderr.strip().splitlines()
    return False, reason[-1] if reason else f"exit code {done.returncode}"
