#!/usr/bin/env python3
"""Self-test for the v2 tunnel logic - no SSH server, no private key needed.

Verifies the three things that were wrong or missing before:
  1. failed connections back off exponentially (1s, 2s, 4s ...),
  2. a *dead transport* is noticed even while idle and leads to a reconnect
     (the old code only printed "SSH transport is inactive" and never
     reconnected),
  3. stop() returns promptly even in the middle of a long backoff.

Run with an interpreter that has paramiko, from the tunnel project root:
    python3 selftest_v2.py
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time

import paramiko

from project_code import stunnel

os.environ.setdefault("PYTHONUNBUFFERED", "1")


class FakeTransport:
    def __init__(self, active=True):
        self.active = active
        self.keepalives = []

    def set_keepalive(self, seconds):
        self.keepalives.append(seconds)

    def is_active(self):
        return self.active


class FakeSSH:
    def __init__(self, transport):
        self._transport = transport
        self.close_calls = 0

    def close(self):
        self.close_calls += 1

    def get_transport(self):
        return self._transport


class CapturedLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def make_tunnel(port):
    return stunnel.SSHTunnel(
        host_config={
            "hostname": "fake.example",
            "username": "nobody",
            "remote_port": 1080,
            "local_port": port,
            "key_filename": "/dev/null",
        }
    )


def run_case_failed_connects():
    """1) exponential backoff while every connect fails."""
    port = 18000 + os.getpid() % 500
    tunnel = make_tunnel(port)
    tunnel.connect_fn = lambda: (_ for _ in ()).throw(
        paramiko.SSHException("simulated auth failure")
    )
    log = CapturedLog()
    tunnel.log.addHandler(log)
    tunnel.log.setLevel(logging.INFO)

    thread = threading.Thread(target=tunnel.start_tunn, daemon=True)
    thread.start()
    time.sleep(3.2)
    tunnel.stop()
    thread.join(timeout=3)
    delays = [msg for msg in log.records if msg.startswith("retrying in")]
    print(f"  connect failures -> attempts logged: {len(delays)}  {delays[:5]}")
    ok = len(delays) >= 3 and "1.0s" in delays[0] and "2.0s" in delays[1] and "4.0s" in delays[2]
    print(f"  退避递增 1s->2s->4s: {'OK' if ok else 'FAIL'}")
    return ok and not thread.is_alive()


def run_case_dead_transport():
    """2) an idle tunnel with a dead transport must reconnect."""
    port = 18500 + os.getpid() % 500
    tunnel = make_tunnel(port)
    transport = FakeTransport(active=False)
    attempts = []

    def fake_connect():
        attempts.append(time.monotonic())
        tunnel.ssh = FakeSSH(transport)
        return transport

    tunnel._connect = fake_connect
    log = CapturedLog()
    tunnel.log.addHandler(log)
    tunnel.log.setLevel(logging.INFO)

    thread = threading.Thread(target=tunnel.start_tunn, daemon=True)
    thread.start()
    time.sleep(2.5)
    tunnel.stop()
    thread.join(timeout=3)
    drops = [msg for msg in log.records if "dropped" in msg]
    print(f"  dead transport -> reconnects: {len(attempts)}, drops detected: {len(drops)}")
    print(f"  检测到失效并重连: {'OK' if len(attempts) >= 2 and len(drops) >= 1 else 'FAIL'}")
    return len(attempts) >= 2 and len(drops) >= 1 and not thread.is_alive()


def run_case_stop_during_backoff():
    """3) stop() must not wait for a long backoff to finish."""
    port = 19000 + os.getpid() % 500
    tunnel = make_tunnel(port)
    tunnel.connect_fn = lambda: (_ for _ in ()).throw(paramiko.SSHException("nope"))
    thread = threading.Thread(target=tunnel.start_tunn, daemon=True)
    thread.start()
    time.sleep(0.3)
    # force a long pending backoff
    tunnel._backoff = 30.0
    started = time.monotonic()
    tunnel.stop()
    thread.join(timeout=3)
    elapsed = time.monotonic() - started
    print(f"  stop() during a 30s backoff took {elapsed:.2f}s")
    print(f"  立即退出: {'OK' if elapsed < 1.5 and not thread.is_alive() else 'FAIL'}")
    return elapsed < 1.5 and not thread.is_alive()


def run_case_forward_stream():
    """4) forward_stream copies bytes and closes both ends."""
    left, right = socket.socketpair()
    other_left, other_right = socket.socketpair()
    worker = threading.Thread(target=stunnel.forward_stream, args=(left, other_left), daemon=True)
    worker.start()
    right.sendall(b"hello tunnel")
    other_right.settimeout(2)
    got = other_right.recv(64)
    right.close()
    worker.join(timeout=2)
    print(f"  forward_stream 转发: {got!r}")
    other_right.close()
    return got == b"hello tunnel"


def main():
    logging.basicConfig(level=logging.WARNING)
    # speed the test up
    stunnel.LIVENESS_INTERVAL = 0.2
    stunnel.ACCEPT_TIMEOUT = 0.1
    stunnel.BACKOFF_INITIAL = 1.0          # keep 1/2/4 to check the ladder
    stunnel.BACKOFF_RESET_AFTER = 0.5

    print("== 1) 连接失败 -> 指数退避 ==")
    one = run_case_failed_connects()
    print("== 2) transport 静默失效 -> 主动重连（本次修复的重点）==")
    two = run_case_dead_transport()
    print("== 3) 退避期间 stop() 立即生效 ==")
    three = run_case_stop_during_backoff()
    print("== 4) forward_stream 数据转发 ==")
    four = run_case_forward_stream()
    print()
    results = {"backoff": one, "dead transport": two, "stop": three, "forward": four}
    for name, ok in results.items():
        print(f"  {name:<16} {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
