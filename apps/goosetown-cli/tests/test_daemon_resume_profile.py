"""A scheduled wake keeps the requested profile's credentials and home together."""
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

from goosetown import core


def setup_profiles(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.delenv('AGENT_DIR', raising=False)
    homes = {}
    for name in ('alpha', 'beta'):
        folder = home / '.goosetown/agents' / name
        folder.mkdir(parents=True)
        (folder / 'GOOSETOWN.md').write_text(
            f'agent: {name}\ntoken: {name}-credential\nworkspace_path: {folder}\napi_url: https://api.example/api/v1\n')
        homes[name] = folder
    (home / '.goosetown/active-agent').write_text('alpha\n')
    state = tmp_path / 'beta-state'
    state.mkdir()
    alarm = state / 'alarm.json'
    alarm.write_text(json.dumps({'wake_time': '00:00', 'timezone': 'UTC'}))
    monkeypatch.setattr(core, 'state_dir', lambda _: state)
    @contextmanager
    def managed(workspace, api_url):
        yield Mock()
    monkeypatch.setattr('goosetown.member_binding.ManagedMember', managed)
    monkeypatch.setattr('goosetown.runtime_join.enroll_if_requested', lambda *_: {
        'runtime_session_token': 'gtrs_fresh', 'actor_id': 'beta-actor', 'expires_at': 9999999999,
        'api_url': 'https://api.example/api/v1'})
    start = Mock(return_value={'connected': True})
    monkeypatch.setattr(core, '_start_daemon', start)
    return home, homes, alarm, start


def test_resume_requested_profile_when_another_profile_is_selected(monkeypatch, tmp_path):
    home, homes, alarm, start = setup_profiles(monkeypatch, tmp_path)
    # Even an unrelated explicit home cannot replace the requested profile.
    monkeypatch.setenv('AGENT_DIR', str(homes['alpha']))
    assert core.cmd_daemon_resume('beta') == 0
    config, workspace, session = start.call_args.args
    assert session["runtime_session_token"] == "gtrs_fresh"
    assert session["managed"] is True
    assert config['agent'] == 'beta'
    assert config['token'] == 'beta-credential'
    assert config['workspace_path'] == str(homes['beta'])
    assert workspace == homes['beta']
    assert (home / '.goosetown/active-agent').read_text() == 'alpha\n'
    assert not alarm.exists()


def test_missing_requested_profile_never_uses_selected_credentials(monkeypatch, tmp_path):
    _, homes, alarm, start = setup_profiles(monkeypatch, tmp_path)
    (homes['beta'] / 'GOOSETOWN.md').unlink()
    assert core.cmd_daemon_resume('beta') == 1
    start.assert_not_called()
    assert alarm.exists()


def test_mismatched_requested_profile_is_rejected(monkeypatch, tmp_path):
    _, homes, alarm, start = setup_profiles(monkeypatch, tmp_path)
    (homes['beta'] / 'GOOSETOWN.md').write_text('agent: alpha\ntoken: alpha-credential\n')
    assert core.cmd_daemon_resume('beta') == 1
    start.assert_not_called()
    assert alarm.exists()


def test_resume_denied_proof_keeps_alarm_and_does_not_start(monkeypatch, tmp_path, capsys):
    from goosetown.runtime_join import EnrollmentError
    _, _, alarm, start = setup_profiles(monkeypatch, tmp_path)
    def denied(*_):
        raise EnrollmentError("Binding revoked or inactive")
    monkeypatch.setattr('goosetown.runtime_join.enroll_if_requested', denied)
    assert core.cmd_daemon_resume('beta') == 1
    start.assert_not_called()
    assert alarm.exists()
    assert 'Owner action required' in capsys.readouterr().out
