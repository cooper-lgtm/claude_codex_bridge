from __future__ import annotations

import errno

import pytest

from ccbd.socket_client import CcbdClient, CcbdClientError
from ccbd.socket_client_runtime.transport import connect_socket


def test_ccbd_client_uses_stable_default_timeout(tmp_path) -> None:
    client = CcbdClient(tmp_path / "ccbd.sock")
    assert client._timeout_s == 3.0


def test_ccbd_client_reads_timeout_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CCB_CCBD_CLIENT_TIMEOUT_S", "4.5")
    client = CcbdClient(tmp_path / "ccbd.sock")
    assert client._timeout_s == 4.5


def test_ccbd_client_explicit_timeout_overrides_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CCB_CCBD_CLIENT_TIMEOUT_S", "4.5")
    client = CcbdClient(tmp_path / "ccbd.sock", timeout_s=0.2)
    assert client._timeout_s == 0.2


def test_ccbd_client_with_timeout_preserves_socket_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CCB_CCBD_CLIENT_TIMEOUT_S", "4.5")
    socket_path = tmp_path / "ccbd.sock"
    client = CcbdClient(socket_path)

    cloned = client.with_timeout(12.0)

    assert cloned is not client
    assert cloned._socket_path == socket_path
    assert cloned._timeout_s == 12.0
    assert client._timeout_s == 4.5


def test_ccbd_client_dynamic_submit_endpoint_uses_request(monkeypatch, tmp_path) -> None:
    client = CcbdClient(tmp_path / "ccbd.sock")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(client, 'request', lambda op, payload=None: calls.append((op, payload)) or {'ok': True})

    class Envelope:
        def to_record(self) -> dict:
            return {'to_agent': 'agent1', 'body': 'hello'}

    envelope = Envelope()
    payload = client.submit(envelope)

    assert payload == {'ok': True}
    assert calls and calls[0][0] == 'submit'
    assert calls[0][1]['to_agent'] == 'agent1'


def test_ccbd_client_dynamic_attach_endpoint_builds_payload(monkeypatch, tmp_path) -> None:
    client = CcbdClient(tmp_path / "ccbd.sock")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(client, 'request', lambda op, payload=None: calls.append((op, payload)) or {'ok': True})

    payload = client.attach(
        agent_name='agent3',
        workspace_path='/tmp/work',
        backend_type='pane-backed',
        pane_id='%9',
        binding_source='external-attach',
    )

    assert payload == {'ok': True}
    assert calls == [
        (
            'attach',
            {
                'agent_name': 'agent3',
                'workspace_path': '/tmp/work',
                'backend_type': 'pane-backed',
                'pid': None,
                'runtime_ref': None,
                'session_ref': None,
                'health': None,
                'provider': None,
                'runtime_root': None,
                'runtime_pid': None,
                'terminal_backend': None,
                'pane_id': '%9',
                'active_pane_id': None,
                'pane_title_marker': None,
                'pane_state': None,
                'tmux_socket_name': None,
                'tmux_window_name': None,
                'tmux_window_id': None,
                'session_file': None,
                'session_id': None,
                'lifecycle_state': None,
                'managed_by': None,
                'binding_source': 'external-attach',
            },
        )
    ]


def test_ccbd_client_dynamic_shutdown_endpoint_uses_empty_payload(monkeypatch, tmp_path) -> None:
    client = CcbdClient(tmp_path / "ccbd.sock")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(client, 'request', lambda op, payload=None: calls.append((op, payload)) or {'ok': True})

    client.shutdown()

    assert calls == [('shutdown', {})]


def test_ccbd_client_project_restart_panes_endpoint_uses_empty_payload(monkeypatch, tmp_path) -> None:
    client = CcbdClient(tmp_path / "ccbd.sock")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(client, 'request', lambda op, payload=None: calls.append((op, payload)) or {'ok': True})

    client.project_restart_panes()

    assert calls == [('project_restart_panes', {})]


def test_ccbd_client_request_wraps_socket_connect_errors(monkeypatch, tmp_path) -> None:
    client = CcbdClient(tmp_path / "ccbd.sock")

    monkeypatch.setattr(
        'ccbd.socket_client.connect_socket',
        lambda socket_path, *, timeout_s: (_ for _ in ()).throw(ConnectionRefusedError('[Errno 111] Connection refused')),
    )

    with pytest.raises(CcbdClientError, match='Connection refused'):
        client.request('ping', {})


def test_connect_socket_retries_transient_connect_errors_within_timeout(monkeypatch, tmp_path) -> None:
    current = {'t': 0.0}
    attempts: list[object] = []
    sleeps: list[float] = []

    class _FakeSocket:
        def __init__(self) -> None:
            self.closed = False
            self.timeout = None

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, path: str) -> None:
            attempts.append((path, self.timeout))
            if len(attempts) == 1:
                raise OSError(errno.EAGAIN, 'Resource temporarily unavailable')

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr('ccbd.socket_client_runtime.transport.time.monotonic', lambda: current['t'])

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        current['t'] += float(seconds)

    monkeypatch.setattr('ccbd.socket_client_runtime.transport.time.sleep', _sleep)
    monkeypatch.setattr('ccbd.socket_client_runtime.transport.socket.socket', lambda *args, **kwargs: _FakeSocket())

    sock = connect_socket(tmp_path / 'ccbd.sock', timeout_s=0.5)

    assert isinstance(sock, _FakeSocket)
    assert len(attempts) == 2
    assert sleeps == [0.05]


def test_connect_socket_does_not_retry_non_transient_errors(monkeypatch, tmp_path) -> None:
    attempts = 0

    class _FakeSocket:
        def settimeout(self, timeout):
            del timeout

        def connect(self, path: str) -> None:
            nonlocal attempts
            del path
            attempts += 1
            raise OSError(errno.EACCES, 'Permission denied')

        def close(self) -> None:
            pass

    monkeypatch.setattr('ccbd.socket_client_runtime.transport.socket.socket', lambda *args, **kwargs: _FakeSocket())

    with pytest.raises(CcbdClientError, match='Permission denied'):
        connect_socket(tmp_path / 'ccbd.sock', timeout_s=0.5)

    assert attempts == 1


def test_connect_socket_caps_transient_connect_retries(monkeypatch, tmp_path) -> None:
    current = {'t': 0.0}
    attempts = 0

    class _FakeSocket:
        def settimeout(self, timeout):
            del timeout

        def connect(self, path: str) -> None:
            nonlocal attempts
            del path
            attempts += 1
            raise OSError(errno.ENOENT, 'No such file or directory')

        def close(self) -> None:
            pass

    monkeypatch.setattr('ccbd.socket_client_runtime.transport.time.monotonic', lambda: current['t'])

    def _sleep(seconds: float) -> None:
        current['t'] += float(seconds)

    monkeypatch.setattr('ccbd.socket_client_runtime.transport.time.sleep', _sleep)
    monkeypatch.setattr('ccbd.socket_client_runtime.transport.socket.socket', lambda *args, **kwargs: _FakeSocket())

    with pytest.raises(CcbdClientError, match='No such file or directory'):
        connect_socket(tmp_path / 'ccbd.sock', timeout_s=0.5)

    assert attempts == 3
