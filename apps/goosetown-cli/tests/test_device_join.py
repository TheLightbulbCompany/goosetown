import argparse
from contextlib import contextmanager
from pathlib import Path

import pytest

from goosetown import core, device_join
from goosetown.cli import _build_parser
from goosetown.runtime_join import EnrollmentError


def test_browser_flow_prints_only_public_code(monkeypatch, capsys):
    args = argparse.Namespace(name="lucky", appearance="goose", personality="kind")
    responses = iter([
        {"verification_uri": "https://goosetown.thelightbulb.company/connect?code=ABCDE-12345", "user_code": "ABCDE-12345", "device_secret": "secret", "expires_at": 9999999999},
        {"state": "pending"}, {"state": "approved", "join_token": "gtj_private"},
    ])
    calls = []
    def post(url, token, body):
        calls.append(body)
        return next(responses)
    monkeypatch.setattr(device_join, '_post', post)
    monkeypatch.setattr(device_join.webbrowser, 'open', lambda _: None)
    monkeypatch.setattr(device_join.time, 'sleep', lambda _: None)
    assert device_join.authorize('https://api.goosetown.thelightbulb.company/api/v1', args) == 'gtj_private'
    output = capsys.readouterr()
    assert 'ABCDE-12345' in output.err
    assert 'secret' not in output.err + output.out
    assert 'gtj_private' not in output.err + output.out
    assert calls[1] == {"device_secret": "secret"}


def test_tokenless_join_uses_home_and_selected_config(monkeypatch, tmp_path):
    project, home = tmp_path / 'project', tmp_path / 'home'
    project.mkdir(); home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.delenv('AGENT_DIR', raising=False)
    monkeypatch.delenv('GOOSETOWN_MEMBER_FD', raising=False)
    @contextmanager
    def managed(workspace, api_url):
        assert workspace == home / '.goosetown/agents/lucky'
        class Member:
            def check_support(self, _): pass
        yield Member()
    monkeypatch.setattr('goosetown.member_binding.ManagedMember', managed)
    monkeypatch.setattr(device_join, 'authorize', lambda *_: 'gtj_private')
    def joined(args, member):
        assert args.token == 'gtj_private'
        path = Path(core.os.environ['AGENT_DIR'])
        (path / 'GOOSETOWN.md').write_text('agent: lucky\ntoken: private\n')
    monkeypatch.setattr(core, '_cmd_join', joined)
    core.cmd_join(_build_parser().parse_args(['join', '--name', 'lucky']))
    assert list(project.iterdir()) == []
    monkeypatch.delenv('AGENT_DIR')
    from goosetown.config import load_config
    assert load_config()['agent'] == 'lucky'


def test_cached_credential_is_checked_with_server_before_reuse(monkeypatch, tmp_path):
    import base64
    import json
    import time
    workspace = tmp_path / "town"
    (workspace / ".goosetown/member/state").mkdir(parents=True)
    claims = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).decode().rstrip("=")
    token = claims + ".signature"
    (workspace / "GOOSETOWN.md").write_text("token: " + token)
    monkeypatch.setattr(device_join, "_post", lambda *_: {"valid": True})
    assert device_join.cached_credential("https://api.example", workspace) == token
    def revoked(*_):
        raise EnrollmentError("revoked")
    monkeypatch.setattr(device_join, "_post", revoked)
    assert device_join.cached_credential("https://api.example", workspace) is None
