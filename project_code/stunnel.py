# project_code/stunnel.py
"""Local port forwarding over one or more SSH servers.

``SSHTunnel(host_config).start_tunn()`` listens on ``127.0.0.1:<local_port>``
and forwards every accepted connection to ``<target_host>:<remote_port>`` as a
``direct-tcpip`` channel through ``<hostname>:<sshd_port>``.

This is the v2 implementation.  Compared with the first version it:

* **reconnects with exponential backoff** - 1s, 2s, 4s ... capped at 60s,
  instead of retrying on a fixed schedule.  A connection that stayed up for
  ``BACKOFF_RESET_AFTER`` seconds resets the delay, so a long lived tunnel that
  drops once recovers quickly;
* **never swallows a failure that needs a reconnect**: the previous version
  caught "SSH transport is inactive" in the per-connection handler and merely
  printed it, so a dead transport left the port listening with every request
  failing and no reconnect ever happening;
* **probes the transport while idle** (every ``LIVENESS_INTERVAL`` seconds
  during the accept timeout), so a silently dropped connection is noticed even
  when no new request arrives;
* keeps ``transport.set_keepalive(30)`` so NAT/idle timeouts do not kill an
  otherwise healthy connection;
* uses ``logging`` instead of ``print`` and stops cleanly on :meth:`stop` /
  SIGTERM (``start_service.py`` installs the signal handlers);
* announces an unknown SSH host key instead of accepting it silently.

The public interface is unchanged: ``SSHTunnel(host_config)``, ``start_tunn()``,
``stop()``, ``cleanup()`` and the module level ``forward_stream()``.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

import paramiko

__all__ = ["SSHTunnel", "forward_stream"]

logger = logging.getLogger(__name__)

#: First retry delay after a failed connection.
BACKOFF_INITIAL = 1.0
#: Upper bound for the retry delay.
BACKOFF_MAX = 60.0
#: Multiply the delay by this after every failure.
BACKOFF_FACTOR = 2.0
#: A connection that survived this long counts as healthy, so the next
#: reconnect starts from BACKOFF_INITIAL again.
BACKOFF_RESET_AFTER = 60.0
#: With no traffic, check the SSH transport at most this often.
LIVENESS_INTERVAL = 5.0
#: accept() timeout - also how fast stop() takes effect.
ACCEPT_TIMEOUT = 1.0
#: SSH keepalive interval in seconds (paramiko sends a global request).
SSH_KEEPALIVE = 30
#: TCP connect timeout for the SSH server.
CONNECT_TIMEOUT = 30
#: Granularity of the retry sleep, so stop() is not delayed by a long backoff.
STOP_POLL = 0.5


def forward_stream(src, dst):
    """Copy one direction until either side closes, then close both ends."""
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except (socket.error, paramiko.SSHException, OSError):
        pass
    finally:
        for endpoint in (src, dst):
            try:
                endpoint.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass


class _LoggingAutoAddPolicy(paramiko.AutoAddPolicy):
    """``AutoAddPolicy`` that says out loud when it trusts a new host key."""

    def missing_host_key(self, client, hostname, key):
        logger.warning(
            "accepting unknown SSH host key for %s (%s %s) - add it to "
            "known_hosts to pin it",
            hostname,
            key.get_name(),
            key.get_fingerprint().hex(),
        )
        return super().missing_host_key(client, hostname, key)


class SSHTunnel:
    def __init__(self, host_config):
        self.hostname = host_config["hostname"]
        self.username = host_config["username"]
        self.remote_port = host_config["remote_port"]
        self.local_port = host_config["local_port"]
        self.sshd_port = host_config.get("sshd_port", 22)
        self.target_host = host_config.get("target_host", "127.0.0.1")

        if "pkey" in host_config:
            pkey = host_config["pkey"]
            self.connect_fn = lambda: self.ssh.connect(
                hostname=self.hostname,
                username=self.username,
                port=self.sshd_port,
                timeout=CONNECT_TIMEOUT,
                pkey=pkey,
            )
        elif "key_filename" in host_config:
            key_filename = host_config["key_filename"]
            self.connect_fn = lambda: self.ssh.connect(
                hostname=self.hostname,
                username=self.username,
                port=self.sshd_port,
                timeout=CONNECT_TIMEOUT,
                key_filename=key_filename,
            )
        else:
            password = host_config.get("password")
            self.connect_fn = lambda: self.ssh.connect(
                hostname=self.hostname,
                username=self.username,
                port=self.sshd_port,
                timeout=CONNECT_TIMEOUT,
                password=password,
            )

        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(_LoggingAutoAddPolicy())

        self.running = False
        self.local_server = None
        self._backoff = BACKOFF_INITIAL
        self._connected_at = 0.0
        self.log = logging.getLogger(f"{__name__}.{self.local_port}")

    def __repr__(self):
        return (
            f"SSHTunnel(127.0.0.1:{self.local_port} -> "
            f"{self.target_host}:{self.remote_port} via {self.hostname})"
        )

    # ------------------------------------------------------------------ #
    # public API (unchanged)
    # ------------------------------------------------------------------ #
    def start_tunn(self):
        """Listen and forward until :meth:`stop` is called.  Blocking."""
        self.running = True
        self.local_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.local_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.local_server.bind(("127.0.0.1", self.local_port))
        self.local_server.listen(10)
        self.local_server.settimeout(ACCEPT_TIMEOUT)
        self.log.info(
            "listening on 127.0.0.1:%s -> %s:%s (ssh %s:%s)",
            self.local_port,
            self.target_host,
            self.remote_port,
            self.hostname,
            self.sshd_port,
        )

        while self.running:
            try:
                transport = self._connect()
            except (paramiko.SSHException, socket.error, OSError) as exc:
                if not self.running:
                    break
                self.log.warning("cannot connect to %s: %s", self.hostname, exc)
                self._sleep_backoff()
                continue

            self._connected_at = time.monotonic()
            self.log.info(
                "tunnel up: 127.0.0.1:%s -> %s:%s",
                self.local_port,
                self.target_host,
                self.remote_port,
            )

            try:
                self._serve(transport)
            except (paramiko.SSHException, socket.error, OSError) as exc:
                if not self.running:
                    break
                self.log.warning("tunnel on port %s dropped: %s", self.local_port, exc)
                self._sleep_backoff()

        self.cleanup()

    def stop(self):
        self.log.info("stopping tunnel on port %s", self.local_port)
        self._shutdown()

    def cleanup(self):
        self._shutdown()
        self.log.info("tunnel on port %s stopped", self.local_port)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _shutdown(self):
        self.running = False
        server, self.local_server = self.local_server, None
        if server is not None:
            try:
                server.close()
            except Exception:  # noqa: BLE001
                pass
        # Closing the client tears down every channel, which unblocks the
        # forwarding threads of the connections that are still open.
        try:
            self.ssh.close()
        except Exception:  # noqa: BLE001
            pass

    def _connect(self):
        self.ssh.close()
        self.log.info("connecting to %s:%s ...", self.hostname, self.sshd_port)
        self.connect_fn()
        transport = self.ssh.get_transport()
        if transport is None:
            raise paramiko.SSHException("no transport after connect()")
        transport.set_keepalive(SSH_KEEPALIVE)
        return transport

    def _serve(self, transport):
        """Accept connections until the tunnel is stopped or the SSH link dies."""
        listener = self.local_server
        last_probe = time.monotonic()
        while self.running:
            try:
                client_socket, addr = listener.accept()
            except socket.timeout:
                if time.monotonic() - last_probe >= LIVENESS_INTERVAL:
                    last_probe = time.monotonic()
                    if not transport.is_active():
                        raise paramiko.SSHException("SSH transport is no longer active")
                continue
            except OSError:
                if self.running:
                    raise
                return
            self._open_channel(transport, client_socket, addr)

    def _open_channel(self, transport, client_socket, addr):
        """Serve one connection.

        Raising here is deliberate: the caller turns it into a reconnect, which
        is exactly what the previous version failed to do.
        """
        try:
            if not transport.is_active():
                raise paramiko.SSHException("SSH transport is no longer active")
            channel = transport.open_channel(
                kind="direct-tcpip",
                dest_addr=(self.target_host, self.remote_port),
                src_addr=addr,
            )
            if channel is None:
                raise paramiko.SSHException("open_channel() returned None")
        except Exception as exc:  # noqa: BLE001 - reported, then re-raised if fatal
            self.log.warning(
                "channel to %s:%s failed: %s", self.target_host, self.remote_port, exc
            )
            try:
                client_socket.close()
            except Exception:  # noqa: BLE001
                pass
            if not transport.is_active():
                raise paramiko.SSHException(f"SSH transport died: {exc}") from exc
            return

        for src, dst in ((client_socket, channel), (channel, client_socket)):
            threading.Thread(target=forward_stream, args=(src, dst), daemon=True).start()

    def _sleep_backoff(self):
        # A connection that held for a while was healthy: start over.
        if self._connected_at and time.monotonic() - self._connected_at >= BACKOFF_RESET_AFTER:
            self._backoff = BACKOFF_INITIAL
            self._connected_at = 0.0
        delay = self._backoff
        self._backoff = min(self._backoff * BACKOFF_FACTOR, BACKOFF_MAX)
        self.log.info("retrying in %.1fs", delay)
        deadline = time.monotonic() + delay
        while self.running and time.monotonic() < deadline:
            time.sleep(STOP_POLL)
