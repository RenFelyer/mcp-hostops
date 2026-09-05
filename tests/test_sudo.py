"""Detecting sudo, reading the secret, and masking — pure logic with no network."""

from pathlib import Path

import pytest

from mcp_openssh_connector.core.config.environment import Settings
from mcp_openssh_connector.core.utils.sudo import (
    SudoError,
    decide_prime,
    mask,
    read_secret,
    uses_sudo,
)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("whoami", False),
        ("sudo whoami", True),
        ("ls && sudo apt update", True),
        ("echo sudo", False),  # sudo as an argument, not a verb
        ("grep -r sudo /etc", False),
        ("env FOO=bar sudo systemctl restart x", True),  # env wrapper
        ("env -u HOME sudo true", True),  # env option with a value
        ("VAR=1 sudo -n true", True),  # assignment before sudo
        ("nohup sudo backup &", True),
        ("sleep 1 & sudo reboot", True),  # background command is also a separator
        ("ls 2>&1 | grep sudo", False),  # `>&` is a redirection, not a separator
        ("timeout 5 sudo systemctl stop x", True),  # wrapper with a positional argument
        ("timeout -s KILL 5 sudo true", True),
        ("time -p sudo true", True),  # time's `-p` is a flag, not a valued option
        ("nice -n 10 sudo make", True),
        ("bash -c 'sudo reboot'", True),  # nested sudo inside sh -c
        ("bash -lc 'sudo reboot'", True),  # -c flag fused with another
        ("bash -o pipefail -c 'sudo reboot'", True),  # `-o` takes a value
        ("sh -c 'ls; sudo tail -f /var/log/x'", True),
        ("bash -c 'echo hi'", False),
        ("doas pkg upgrade", True),
        ("/usr/bin/sudo -u www ls", True),
    ],
)
def test_uses_sudo(command: str, expected: bool) -> None:
    assert uses_sudo(command) is expected


def test_decide_prime_modes() -> None:
    assert decide_prime("whoami", "true") is True
    assert decide_prime("sudo whoami", "false") is False
    assert decide_prime("sudo whoami", "auto") is True
    assert decide_prime("whoami", "auto") is False


def test_mask_masks_whole_line_password() -> None:
    # pty echo — the password on its own line: masked in full.
    assert mask("s3cret\r\nreal output", "s3cret") == "***\nreal output"
    assert mask("s3cret", "s3cret") == "***"


def test_mask_does_not_corrupt_substrings() -> None:
    # A short password-as-substring must not corrupt normal output.
    assert mask("chroot to /root", "root") == "chroot to /root"
    assert mask("banana bread", "an") == "banana bread"


def test_mask_noop() -> None:
    assert mask("text", None) == "text"
    assert mask("text", "") == "text"


def test_read_secret_rejects_traversal(tmp_path: Path) -> None:
    s = Settings(secret_dir=tmp_path)
    with pytest.raises(SudoError, match="outside"):
        read_secret("../evil", s)


def test_read_secret_ok(tmp_path: Path) -> None:
    secret = tmp_path / "host1.secret"
    secret.write_text("password\r\n", encoding="utf-8")
    secret.chmod(0o600)
    s = Settings(secret_dir=tmp_path)
    assert read_secret("host1", s) == "password"


def test_read_secret_missing(tmp_path: Path) -> None:
    s = Settings(secret_dir=tmp_path)
    with pytest.raises(SudoError, match="not found"):
        read_secret("nope", s)


def test_read_secret_bad_mode(tmp_path: Path) -> None:
    secret = tmp_path / "host2.secret"
    secret.write_text("x", encoding="utf-8")
    secret.chmod(0o644)
    s = Settings(secret_dir=tmp_path)
    with pytest.raises(SudoError, match="unsafe permissions"):
        read_secret("host2", s)


def test_read_secret_not_regular(tmp_path: Path) -> None:
    (tmp_path / "dir.secret").mkdir(mode=0o700)
    s = Settings(secret_dir=tmp_path)
    with pytest.raises(SudoError, match="regular file"):
        read_secret("dir", s)


def test_read_secret_follows_own_symlink(tmp_path: Path) -> None:
    # Our own symlink to a file elsewhere is fine: the actual file is checked.
    store = tmp_path / "store"
    store.mkdir()
    real = store / "pw"
    real.write_text("password", encoding="utf-8")
    real.chmod(0o600)
    (tmp_path / "linked.secret").symlink_to(real)
    assert read_secret("linked", Settings(secret_dir=tmp_path)) == "password"
