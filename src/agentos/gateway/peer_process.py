"""Whether the process on the other end of a local connection is one we spawned.

Everything an agent runs on this host -- an ``exec_command`` shell, a
background process, a cron script, ``execute_code``, an ACP harness and the
shells *it* runs, an MCP server -- is a descendant of the gateway process.
The operator's terminal, the desktop app and a browser are not. On Linux the
kernel can say which process holds the client end of a TCP connection, so the
gateway can tell an agent's connection from the operator's without trusting
anything the client sends: no environment variable to unset, no secret file
to read past a text scan.

The answer is advisory in one direction only. ``True`` makes a connection an
agent's (``AgentBinding(via="process")``); ``False`` -- including "cannot
tell" on other platforms, a peer that is not an IP, or ``/proc`` being
unreadable -- leaves the other rules of ``gateway.agent_surface`` to decide.
A process that detached and was re-parented away from the gateway (``setsid``
with a double fork) is no longer a descendant; in the official container
image the gateway is PID 1, so even that one is re-parented back to it.

Cost: walking the descendants through ``/proc/<pid>/task/<tid>/children``
(falling back to a full ``/proc`` scan where that file is absent), reading
their fd tables, and parsing ``/proc/net/tcp{,6}`` only when a descendant
holds a socket at all. A gateway with no children pays for one directory
read.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
import sys
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_PROC = Path("/proc")

#: Bounds on the walk. Exceeding one fails closed (the peer counts as ours):
#: an agent that forks thousands of processes must not buy itself a pass.
_MAX_DESCENDANTS = 4096
_MAX_FDS = 200_000

IpPort = tuple[str, int]


class _TruncatedError(Exception):
    """The walk hit a bound before it could answer."""


def supported() -> bool:
    return sys.platform.startswith("linux") and (_PROC / "self" / "net" / "tcp").exists()


def _read_children(pid: int) -> list[int] | None:
    """Direct children of *pid*, or ``None`` when the kernel lacks the interface."""
    task_dir = _PROC / str(pid) / "task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return []
    children: list[int] = []
    for tid in tids:
        try:
            text = (task_dir / tid / "children").read_text()
        except FileNotFoundError:
            if tid == str(pid):
                return None  # CONFIG_PROC_CHILDREN is off: no such file anywhere
            continue
        except OSError:
            continue
        children.extend(int(part) for part in text.split() if part.isdigit())
    return children


def _parent_map() -> dict[int, int]:
    parents: dict[int, int] = {}
    for name in os.listdir(_PROC):
        if not name.isdigit():
            continue
        try:
            stat = (_PROC / name / "stat").read_text()
        except OSError:
            continue
        # ``pid (comm) state ppid ...``; comm may hold spaces and parentheses.
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) >= 2 and fields[1].isdigit():
            parents[int(name)] = int(fields[1])
    return parents


def descendants(root: int | None = None) -> set[int]:
    """Every live descendant of *root* (default: this process), not *root* itself."""
    root = os.getpid() if root is None else root
    found: set[int] = set()
    first = _read_children(root)
    if first is None:
        parents = _parent_map()
        frontier = [root]
        while frontier:
            current = frontier.pop()
            for pid, parent in parents.items():
                if parent == current and pid not in found:
                    found.add(pid)
                    frontier.append(pid)
                    if len(found) > _MAX_DESCENDANTS:
                        raise _TruncatedError
        return found
    frontier = list(first)
    while frontier:
        pid = frontier.pop()
        if pid in found or pid == root:
            continue
        found.add(pid)
        if len(found) > _MAX_DESCENDANTS:
            raise _TruncatedError
        frontier.extend(_read_children(pid) or [])
    return found


def _socket_inodes(pids: set[int]) -> set[int]:
    inodes: set[int] = set()
    seen = 0
    for pid in pids:
        fd_dir = _PROC / str(pid) / "fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            seen += 1
            if seen > _MAX_FDS:
                raise _TruncatedError
            try:
                target = os.readlink(fd_dir / fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(int(target[8:-1]))
    return inodes


def _decode(hex_addr: str) -> IpPort | None:
    try:
        host_hex, port_hex = hex_addr.split(":")
        port = int(port_hex, 16)
        if len(host_hex) == 8:
            packed = struct.pack("=I", int(host_hex, 16))
            return socket.inet_ntop(socket.AF_INET, packed), port
        if len(host_hex) == 32:
            words = [int(host_hex[i : i + 8], 16) for i in range(0, 32, 8)]
            packed = struct.pack("=4I", *words)
            return socket.inet_ntop(socket.AF_INET6, packed), port
    except (ValueError, OSError, struct.error):
        return None
    return None


def _normalize(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        addr = ipaddress.ip_address(host.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _connection_inodes(peer: IpPort, server_port: int | None) -> set[int]:
    """Inodes of the sockets whose local end is *peer* (the client side)."""
    want_addr = _normalize(peer[0])
    if want_addr is None:
        return set()
    want_port = int(peer[1])
    found: set[int] = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = (_PROC / "self" / "net" / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10:
                continue
            local = _decode(fields[1])
            if local is None or local[1] != want_port or _normalize(local[0]) != want_addr:
                continue
            if server_port is not None:
                remote = _decode(fields[2])
                if remote is None or remote[1] != server_port:
                    continue
            if fields[9].isdigit():
                found.add(int(fields[9]))
    return found


def peer_is_descendant(peer: IpPort | None, server: IpPort | None = None) -> bool:
    """Whether a descendant of this process holds the client end of *peer*.

    *peer* is the connection's remote ``(host, port)`` as the socket saw it,
    *server* the local end (only its port is used, to tell two connections
    from the same client port apart). Never raises.
    """
    if peer is None or not supported():
        return False
    try:
        host, port = str(peer[0]), int(peer[1])
    except (TypeError, ValueError, IndexError):
        return False
    if _normalize(host) is None or port <= 0:
        return False
    try:
        server_port = int(server[1]) if server is not None else None
    except (TypeError, ValueError, IndexError):
        server_port = None
    try:
        pids = descendants()
        if not pids:
            return False
        inodes = _socket_inodes(pids)
        if not inodes:
            return False
        return bool(_connection_inodes((host, port), server_port) & inodes)
    except _TruncatedError:
        log.warning("peer_process.walk_truncated", peer=f"{host}:{port}")
        return True
    except Exception:  # noqa: BLE001 - an advisory signal must never break admission
        log.debug("peer_process.lookup_failed", exc_info=True)
        return False


__all__ = ["descendants", "peer_is_descendant", "supported"]
