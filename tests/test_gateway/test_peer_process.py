"""The gateway can tell when the other end of a local connection is its own child."""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap

import pytest

from agentos.gateway import peer_process

pytestmark = pytest.mark.skipif(
    not peer_process.supported(), reason="needs Linux /proc to map sockets to processes"
)

_CHILD = textwrap.dedent(
    """
    import socket, sys, time
    sock = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
    sys.stdout.write("connected\\n"); sys.stdout.flush()
    sys.stdin.readline()
    """
)


def _listener() -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    return server


def test_a_child_holding_the_client_end_is_a_descendant() -> None:
    with _listener() as server:
        port = server.getsockname()[1]
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(port)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None and child.stdout.readline().strip() == "connected"
            conn, peer = server.accept()
            with conn:
                assert peer_process.peer_is_descendant(peer, conn.getsockname())
                assert child.pid in peer_process.descendants()
        finally:
            if child.stdin is not None:
                child.stdin.write("\n")
                child.stdin.flush()
            child.wait(timeout=10)


def test_a_grandchild_is_a_descendant_too() -> None:
    """An agent shell's own children (``sh -c 'python …'``) count."""
    with _listener() as server:
        port = server.getsockname()[1]
        shell = subprocess.Popen(
            ["/bin/sh", "-c", f'{sys.executable} -c "$0" {port}', _CHILD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert shell.stdout is not None and shell.stdout.readline().strip() == "connected"
            conn, peer = server.accept()
            with conn:
                assert peer_process.peer_is_descendant(peer, conn.getsockname())
        finally:
            if shell.stdin is not None:
                shell.stdin.write("\n")
                shell.stdin.flush()
            shell.wait(timeout=10)


def test_our_own_connection_is_not_a_descendant() -> None:
    with _listener() as server:
        client = socket.create_connection(server.getsockname())
        conn, peer = server.accept()
        with client, conn:
            assert not peer_process.peer_is_descendant(peer, conn.getsockname())


@pytest.mark.parametrize(
    "peer", [None, ("testclient", 50000), ("127.0.0.1", 0), ("not-an-ip", 1), ("127.0.0.1",)]
)
def test_unknown_peers_are_not_descendants(peer: object) -> None:
    assert peer_process.peer_is_descendant(peer, ("127.0.0.1", 18791)) is False  # type: ignore[arg-type]


def test_a_walk_that_hits_its_bound_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def too_many(root: int | None = None) -> set[int]:
        raise peer_process._TruncatedError

    monkeypatch.setattr(peer_process, "descendants", too_many)
    assert peer_process.peer_is_descendant(("127.0.0.1", 40000), ("127.0.0.1", 18791))
