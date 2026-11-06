"""Fresh-host setup and macOS Keychain lookup must precede browser consent."""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock
import sys

import pytest

from goosetown import core, member_binding
from goosetown.cli import _build_parser


def test_dedicated_binary_is_preferred_without_changing_path(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    private = tmp_path / ".goosetown/bin/lightbulb"
    private.parent.mkdir(parents=True)
    private.write_text("fixture executable")
    private.chmod(0o700)
    which = Mock(return_value="/system/lightbulb")
    monkeypatch.setattr("shutil.which", which)
    assert member_binding.member_binary() == str(private)
    which.assert_not_called()


def test_missing_standalone_fails_before_device_code(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.delenv("GOOSETOWN_MEMBER_FD", raising=False)
    authorize = Mock()
    monkeypatch.setattr("goosetown.device_join.authorize", authorize)
    with pytest.raises(SystemExit):
        core.cmd_join(_build_parser().parse_args(["join", "--name", "fresh"]))
    authorize.assert_not_called()
    assert (
        "standalone binary is required before browser confirmation"
        in capsys.readouterr().out
    )


def test_incompatible_signer_fails_before_device_code(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("GOOSETOWN_MEMBER_FD", raising=False)

    @contextmanager
    def incompatible(*_):
        member = Mock()
        member.check_support.side_effect = member_binding.MemberError(
            "incompatible GooseTown signing"
        )
        yield member

    monkeypatch.setattr(member_binding, "ManagedMember", incompatible)
    authorize = Mock()
    monkeypatch.setattr("goosetown.device_join.authorize", authorize)
    with pytest.raises(SystemExit):
        core.cmd_join(_build_parser().parse_args(["join", "--name", "fresh"]))
    authorize.assert_not_called()


def test_mac_preserves_os_home_but_keeps_explicit_isolated_member_state(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", "/real-os-user")
    monkeypatch.setenv("PRIVATE_CONTEXT", "must-not-be-inherited")
    state = tmp_path / "goose/.goosetown/member"
    env = member_binding.member_environment(state)
    assert env["HOME"] == "/real-os-user"
    assert "PRIVATE_CONTEXT" not in env
    member = member_binding.ManagedMember(tmp_path / "goose")
    assert member.home == state


def test_linux_keeps_home_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", "/real-os-user")
    assert member_binding.member_environment(tmp_path)["HOME"] == str(tmp_path)
