import socket
import threading
from unittest.mock import MagicMock, patch

from ezhpcy.tunnel.relay import DirectTCPIPRelay, relay_streams


def _start_relay(left: socket.socket, right: socket.socket) -> threading.Thread:
    thread = threading.Thread(target=relay_streams, args=(left, right))
    thread.start()
    return thread


def test_relay_streams_copies_bytes_in_both_directions() -> None:
    left_client, left_relay = socket.socketpair()
    right_relay, right_client = socket.socketpair()
    thread = _start_relay(left_relay, right_relay)

    left_client.sendall(b"worker request")
    assert right_client.recv(1024) == b"worker request"

    right_client.sendall(b"worker response")
    assert left_client.recv(1024) == b"worker response"

    left_client.close()
    right_client.close()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_relay_streams_preserves_response_after_request_eof() -> None:
    left_client, left_relay = socket.socketpair()
    right_relay, right_client = socket.socketpair()
    thread = _start_relay(left_relay, right_relay)

    left_client.sendall(b"request")
    left_client.shutdown(socket.SHUT_WR)
    assert right_client.recv(1024) == b"request"
    assert right_client.recv(1024) == b""

    right_client.sendall(b"response after eof")
    right_client.shutdown(socket.SHUT_WR)
    assert left_client.recv(1024) == b"response after eof"
    assert left_client.recv(1024) == b""

    thread.join(timeout=1)
    assert not thread.is_alive()
    left_client.close()
    right_client.close()


def test_direct_tcpip_channel_uses_worker_destination_and_client_origin() -> None:
    transport = MagicMock()
    channel = MagicMock()
    transport.open_channel.return_value = channel
    client = MagicMock()

    with (
        DirectTCPIPRelay(transport, ("worker.internal", 3333)) as server,
        patch("ezhpcy.tunnel.relay.relay_streams") as relay,
    ):
        server._serve_client(client, ("127.0.0.1", 50123))

    transport.open_channel.assert_called_once_with(
        "direct-tcpip",
        dest_addr=("worker.internal", 3333),
        src_addr=("127.0.0.1", 50123),
    )
    relay.assert_called_once_with(client, channel)
    client.close.assert_called()
    channel.close.assert_called()


def test_direct_tcpip_open_failure_closes_client() -> None:
    transport = MagicMock()
    transport.open_channel.side_effect = OSError("channel rejected")
    client = MagicMock()

    with DirectTCPIPRelay(transport, ("worker.internal", 3333)) as server:
        try:
            server._serve_client(client, ("127.0.0.1", 50123))
        except OSError:
            pass

    client.close.assert_called_once_with()


def test_connection_reset_is_reported_without_escaping_worker_thread() -> None:
    transport = MagicMock()
    channel = MagicMock()
    transport.open_channel.return_value = channel
    client = MagicMock()
    error_handler = MagicMock()
    reset = ConnectionResetError(10054, "connection reset")

    with (
        DirectTCPIPRelay(
            transport,
            ("worker.internal", 3333),
            client_error_handler=error_handler,
        ) as server,
        patch("ezhpcy.tunnel.relay.relay_streams", side_effect=reset),
    ):
        server._serve_client(client, ("127.0.0.1", 50123))

    error_handler.assert_called_once_with(("127.0.0.1", 50123), reset)
    client.close.assert_called()
    channel.close.assert_called()


def test_relay_uses_threading_tcp_server_lifecycle() -> None:
    transport = MagicMock()

    with DirectTCPIPRelay(transport, ("worker.internal", 3333)) as server:
        host, port = server.address
        assert host == "127.0.0.1"
        assert port > 0
        assert server.daemon_threads is True
        assert server.block_on_close is False

    assert server.socket.fileno() == -1


def test_threading_tcp_server_dispatches_accepted_client() -> None:
    transport = MagicMock()
    channel = MagicMock()
    transport.open_channel.return_value = channel
    dispatched = threading.Event()

    with (
        DirectTCPIPRelay(transport, ("worker.internal", 3333)) as server,
        patch(
            "ezhpcy.tunnel.relay.relay_streams",
            side_effect=lambda _client, _channel: dispatched.set(),
        ),
    ):
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
        )
        server_thread.start()
        with socket.create_connection(server.address):
            assert dispatched.wait(timeout=1)
        server.shutdown()
        server_thread.join(timeout=1)

    assert not server_thread.is_alive()
    transport.open_channel.assert_called_once()
    assert transport.open_channel.call_args.kwargs["dest_addr"] == (
        "worker.internal",
        3333,
    )
