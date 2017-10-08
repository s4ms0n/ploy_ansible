from mock import MagicMock
import pytest
import sys


class MockPlayContext:
    executable = u'/bin/sh'
    shell = None


@pytest.fixture
def play_context():
    return MockPlayContext()


@pytest.fixture
def conn(ctrl, ployconf, play_context):
    from ploy_ansible.connection_plugins.execnet_connection import Connection
    ctrl.configfile = ployconf.path
    play_context._ploy_ctrl = ctrl
    play_context.remote_addr = 'foo'
    play_context.port = 87
    play_context.remote_user = 'blubber'
    connection = Connection(play_context, sys.stdin)
    return connection


@pytest.fixture
def rpc():
    class RPC:
        def exec_command(self, cmd):
            return (0, cmd, '')
    return RPC()


class ChannelMock(object):
    def send(self, data):
        self.sent.append(data)

    def receive(self):
        return self.data.pop(0)


class GatewayMock(object):
    def remote_exec(self, *args):
        return self.channel


class MakeGatewayMock(object):
    def __call__(self, *args):
        return self.gw


@pytest.mark.parametrize("ssh_info, expected", [
    (dict(host='foo'), ['foo']),
    (dict(host='foo', port=22), ['-p', '22', 'foo']),
    (dict(host='foo', port=22, ProxyCommand='ssh master -W 10.0.0.1'),
     ['-o', 'ProxyCommand=ssh master -W 10.0.0.1', '-p', '22', 'foo'])])
def test_execnet_ssh_spec(conn, ctrl, ployconf, play_context, monkeypatch, ssh_info, expected):
    init_ssh_key_mock = MagicMock()
    init_ssh_key_mock.return_value = ssh_info
    monkeypatch.setattr("ploy_ansible.connection_plugins.execnet_connection.RPC_CACHE", {})
    monkeypatch.setattr(
        "ploy.tests.dummy_plugin.Instance.init_ssh_key", init_ssh_key_mock)
    makegateway_mock = MagicMock()
    monkeypatch.setattr("execnet.makegateway", makegateway_mock)
    conn._connect()
    call, = makegateway_mock.call_args_list
    spec = call[0][0]
    assert spec.ssh.split() == expected


class ExecCommandBase:
    def test_exec_command(self, conn, rpc, play_context):
        conn.rpc = rpc
        assert conn.exec_command('cmd', 'tmp', None, sudoable=False, executable=None) == (
            0, '', 'cmd', '')
        assert conn.exec_command('cmd', 'tmp', None, sudoable=True, executable=None) == (
            0, '', 'cmd', '')
        assert conn.exec_command('cmd', 'tmp', 'user', sudoable=False, executable=None) == (
            0, '', 'cmd', '')
        assert conn.exec_command('cmd', 'tmp', 'user', sudoable=True, executable=None) == (
            0, '', 'cmd', '')

    def test_exec_command_executable(self, conn, rpc, play_context):
        conn.rpc = rpc
        assert conn.exec_command('cmd', 'tmp', None, sudoable=False, executable='/bin/sh') == (
            0, '', '/bin/sh -c cmd', '')
        assert conn.exec_command('cmd', 'tmp', None, sudoable=True, executable='/bin/sh') == (
            0, '', '/bin/sh -c cmd', '')
        assert conn.exec_command('cmd', 'tmp', 'user', sudoable=False, executable='/bin/sh') == (
            0, '', '/bin/sh -c cmd', '')
        assert conn.exec_command('cmd', 'tmp', 'user', sudoable=True, executable='/bin/sh') == (
            0, '', '/bin/sh -c cmd', '')

    def test_exec_command_sudo(self, conn, rpc, play_context):
        conn.rpc = rpc
        play_context.sudo = True
        assert conn.exec_command('cmd', 'tmp', None, sudoable=False, executable=None) == (
            0, '', 'cmd', '')
        assert conn.exec_command('cmd', 'tmp', None, sudoable=True, executable=None) == (
            0, '', 'sudo None None cmd', '')
        assert conn.exec_command('cmd', 'tmp', 'user', sudoable=False, executable=None) == (
            0, '', 'cmd', '')
        assert conn.exec_command('cmd', 'tmp', 'user', sudoable=True, executable=None) == (
            0, '', 'sudo None user cmd', '')

    def test_exec_command_sudo_executable(self, conn, rpc, play_context):
        conn.rpc = rpc
        play_context.sudo = True
        assert conn.exec_command('cmd', 'tmp', None, sudoable=False, executable='/bin/sh') == (
            0, '', '/bin/sh -c cmd', '')
        assert conn.exec_command('cmd', 'tmp', None, sudoable=True, executable='/bin/sh') == (
            0, '', 'sudo /bin/sh None cmd', '')
        assert conn.exec_command('cmd', 'tmp', 'user', sudoable=False, executable='/bin/sh') == (
            0, '', '/bin/sh -c cmd', '')
        assert conn.exec_command('cmd', 'tmp', 'user', sudoable=True, executable='/bin/sh') == (
            0, '', 'sudo /bin/sh user cmd', '')
