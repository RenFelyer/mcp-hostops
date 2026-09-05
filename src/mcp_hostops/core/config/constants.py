"""Immutable system parameters.

Everything that isn't configurable via the environment and doesn't change at runtime:
directory and file names, probe thresholds, command-parsing vocabularies, the built-in
`llms.txt` registry. Configurable values live in `environment`. Values are immutable by
type too: tuples, `frozenset`, `MappingProxyType`, frozen models.
"""

import errno
from pathlib import Path
from types import MappingProxyType
from typing import Final

from ..schemas import KnownSource

# ── common ──────────────────────────────────────────────────────────────────────
APP_NAME: Final = "mcp-hostops"
PRIVATE_DIR_MODE: Final = 0o700  # a directory closed to group and others
MAX_WORKERS: Final = 16  # threads at once: more doesn't speed things up, just spawns more ssh processes

# ── ssh ─────────────────────────────────────────────────────────────────────────
SSH_DIR: Final = Path.home() / ".ssh"
SSH_CONFIG: Final = SSH_DIR / "config"  # ssh reads exactly this file regardless
TERM_GRACE: Final = 2.0  # seconds between SIGTERM and SIGKILL on a run timeout

# ── managing ~/.ssh/config ──────────────────────────────────────────────────────
SECRET_FILE_MODE: Final = 0o600  # permissions of the secret file and the managed config
# Header of the managed file: the server owns it entirely and may overwrite it.
MANAGED_HEADER: Final = "# Managed by mcp-hostops: add_host/remove_host.\n"
# Key order in the canonical Host block; other options follow in input order.
MANAGED_KEY_ORDER: Final = ("HostName", "User", "Port", "IdentityFile", "ProxyJump")
SSH_DEFAULT_PORT: Final = 22  # the default ssh port: known_hosts doesn't write `[host]:port` for it

# ── availability probes ─────────────────────────────────────────────────────────
# ZeroTier brings up the path to a peer lazily: while rendezvous through the root
# servers is in progress, the kernel returns EHOSTUNREACH instantly. A single probe
# mistakes this for "the node is dead", even though the next one already succeeds. A
# longer timeout doesn't help — the error arrives immediately, not after one.
PROBE_RETRY_ERRNOS: Final = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH})
PROBE_PATH_BUDGET: Final = 2.5  # seconds total for retries while the overlay brings up the path
PROBE_PATH_PAUSE: Final = 0.25  # seconds between attempts

# ── sudo ────────────────────────────────────────────────────────────────────────
SUDO_MASK: Final = "***"
# Priming script: the shell reads the first stdin line and pipes it to a single `sudo -v`
# call. This way sudo gets exactly one line and, on a wrong password, doesn't consume the
# following stdin lines as new attempts, and the password never hits argv. `-k` resets
# the ticket: with a live ticket `sudo -v` wouldn't read a password.
SUDO_PRIME: Final = "IFS= read -r __sudo_pw && printf '%s\\n' \"$__sudo_pw\" | sudo -k -S -p '' -v && unset __sudo_pw"
# Wrappers behind which the real command hides: name → options that take a separate
# value, and the number of positional arguments before the command (`timeout 5 …`).
# sudo/doas themselves aren't included here — they're what we need to recognize.
SUDO_WRAPPERS: Final = MappingProxyType(
    {
        "env": (frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}), 0),
        "nohup": (frozenset[str](), 0),
        "time": (frozenset({"-f", "-o", "--format", "--output"}), 0),
        "command": (frozenset[str](), 0),
        "builtin": (frozenset[str](), 0),
        "exec": (frozenset({"-a"}), 0),
        "nice": (frozenset({"-n", "--adjustment"}), 0),
        "ionice": (frozenset({"-c", "-n", "--class", "--classdata"}), 0),
        "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
        "setsid": (frozenset[str](), 0),
        "timeout": (frozenset({"-s", "-k", "--signal", "--kill-after"}), 1),
    }
)
# Shells where `-c '<code>'` runs a nested command: we need to look inside it, otherwise
# sudo inside `sh -c '…'` would go unnoticed.
SHELLS: Final = frozenset({"sh", "bash", "dash", "zsh", "ash", "ksh", "mksh", "fish"})

# ── llms.txt ────────────────────────────────────────────────────────────────────
LLMS_REDIRECTS: Final = 5  # consecutive redirects; each one is checked as a new address
LLMS_JUNK_NAME: Final = "zzz-nope-12345.txt"  # a stable name so the probe gets cached
LLMS_INDEX_NAME: Final = "llms.txt"
LLMS_FULL_NAME: Final = "llms-full.txt"
# File names that domains publish alongside the index. The set isn't limited to these,
# but these are the ones seen in practice: full text, shortened, and the "context"
# variants of the llms-ctx format.
LLMS_VARIANTS: Final = (
    "llms.txt",
    "llms-full.txt",
    "llms-small.txt",
    "llms-medium.txt",
    "llms-ctx.txt",
    "llms-ctx-full.txt",
)
# Built-in registry: vetted entries we're confident about; cannot be removed.
LLMS_DEFAULT_SOURCES: Final[tuple[KnownSource, ...]] = (
    KnownSource(
        domain="code.claude.com",
        index="https://code.claude.com/llms.txt",
        covers=(
            "Claude Code: the CLI and its configuration points — hooks, slash commands, "
            "skills, subagents, MCP, plugins, permissions; plus the Agent SDK"
        ),
        default=True,
    ),
    KnownSource(
        domain="docs.claude.com",
        index="https://docs.claude.com/llms.txt",
        covers=(
            "Anthropic API: the Messages API, models, pricing, tool use, prompt "
            "caching, limits, error codes, release notes"
        ),
        full_size=40_000_000,
        default=True,
    ),
    KnownSource(
        domain="docs.astral.sh/uv",
        index="https://docs.astral.sh/uv/llms.txt",
        covers=(
            "uv: dependencies, projects and workspaces, running scripts, installing "
            "Python itself, the pip-compatible interface"
        ),
        default=True,
    ),
    KnownSource(
        domain="docs.astral.sh/ruff",
        index="https://docs.astral.sh/ruff/llms.txt",
        covers="ruff: linter rules and their codes, the formatter, configuration, editor integration",
        default=True,
    ),
    KnownSource(
        domain="docs.astral.sh/ty",
        index="https://docs.astral.sh/ty/llms.txt",
        covers="ty: type checking, configuration, the language server, migrating from mypy and pyright",
        default=True,
    ),
    KnownSource(
        domain="pydantic.dev",
        index="https://pydantic.dev/llms.txt",
        covers="root of the Pydantic family — Validation, AI, Logfire; links from here to each product's index",
        default=True,
    ),
    KnownSource(
        domain="docs.pydantic.dev/latest",
        index="https://docs.pydantic.dev/latest/llms.txt",
        covers="pydantic: models, validators, serialization, API reference, parsing error text",
        full_size=2_000_000,
        default=True,
    ),
    KnownSource(
        domain="ai.pydantic.dev",
        index="https://ai.pydantic.dev/llms.txt",
        covers=(
            "Pydantic AI: building agents, tools and toolsets, models and providers, "
            "structured output, dependencies, MCP, testing and evals"
        ),
        full_size=5_300_000,
        default=True,
    ),
    KnownSource(
        domain="gofastmcp.com",
        index="https://gofastmcp.com/llms.txt",
        covers=(
            "FastMCP: MCP servers and clients in Python — tools, resources, "
            "prompts, mounting, transports, authentication, tests"
        ),
        full_size=3_000_000,
        default=True,
    ),
    KnownSource(
        domain="developers.telnyx.com",
        index="https://developers.telnyx.com/llms.txt",
        covers=("Telnyx: voice, messaging, phone numbers and SIP, the APIs and webhooks for real-time communications"),
        full_size=46_000,
        default=True,
    ),
    KnownSource(
        domain="docs.docker.com",
        index="https://docs.docker.com/llms.txt",
        covers=("Docker: images, containers, Compose, build, networking, volumes, the CLI and Dockerfile reference"),
        full_size=318_000,
        default=True,
    ),
    # Nix ecosystem.
    KnownSource(
        domain="determinate.systems",
        index="https://determinate.systems/llms.txt",
        covers="Determinate Systems: the Determinate Nix installer and the secure software supply chain tooling",
        full_size=935_000,
        default=True,
    ),
    KnownSource(
        domain="docs.determinate.systems",
        index="https://docs.determinate.systems/llms.txt",
        covers="Determinate documentation: installing and using Determinate Nix, FlakeHub, enterprise setup",
        full_size=310_000,
        default=True,
    ),
    KnownSource(
        domain="devenv.sh",
        index="https://devenv.sh/llms.txt",
        covers=(
            "devenv: declarative reproducible developer environments on Nix — languages, services, processes, tasks"
        ),
        full_size=710_000,
        default=True,
    ),
    KnownSource(
        domain="zero-to-nix.com",
        index="https://zero-to-nix.com/llms.txt",
        covers="Zero to Nix: an introduction to Nix and flakes — concepts, packages, dev shells, first steps",
        full_size=121_000,
        default=True,
    ),
    KnownSource(
        domain="flakehub.com",
        index="https://flakehub.com/llms.txt",
        covers="FlakeHub: publishing, caching and privately sharing Nix flakes",
        default=True,
    ),
    # Frontend stack.
    KnownSource(
        domain="react.dev",
        index="https://react.dev/llms.txt",
        covers="React: components and hooks, state and effects, the API reference, React DOM",
        default=True,
    ),
    KnownSource(
        domain="vite.dev",
        index="https://vite.dev/llms.txt",
        covers="Vite: the build tool — dev server, config, plugins, build and library modes, SSR",
        full_size=425_000,
        default=True,
    ),
    KnownSource(
        domain="vitest.dev",
        index="https://vitest.dev/llms.txt",
        covers=("Vitest: the test framework — config, matchers, mocking, coverage, browser mode, migration from Jest"),
        full_size=1_365_000,
        default=True,
    ),
    KnownSource(
        domain="lucide.dev",
        index="https://lucide.dev/llms.txt",
        covers="Lucide: the icon library — usage across frameworks, the React/Vue/Svelte packages, customization",
        full_size=321_000,
        default=True,
    ),
    KnownSource(
        domain="zod.dev",
        index="https://zod.dev/llms.txt",
        covers=("Zod: TypeScript-first schema validation — schemas, parsing, refinements, inference, error handling"),
        full_size=285_000,
        default=True,
    ),
)
