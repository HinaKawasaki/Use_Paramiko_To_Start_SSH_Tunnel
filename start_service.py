# start_service.py
"""Start every tunnel from ``conf.py`` and keep them alive.

Run by ``ssh-tunnel.service``::

    ExecStart=/home/hello/venvs/.para/bin/python3 .../ssh_tunnel/start_service.py

Each entry of ``conf.hosts`` becomes one :class:`project_code.stunnel.SSHTunnel`
in its own daemon thread.  SIGTERM/SIGINT stop all of them and exit cleanly, so
``systemctl restart`` does not leave a socket behind.

The unit redirects stdout to ``normal.log`` and stderr to ``serious.log``, so
routine INFO goes to stdout and warnings/errors to stderr - the split the
previous ``print`` based version had.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from project_code.stunnel import SSHTunnel

import conf

logger = logging.getLogger("start_service")

#: How long to wait for the tunnels to wind down after a stop request.
JOIN_TIMEOUT = 5.0


def setup_logging() -> None:
    """INFO and below to stdout, warnings and above to stderr."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    routine = logging.StreamHandler(sys.stdout)
    routine.setLevel(logging.DEBUG)
    routine.addFilter(lambda record: record.levelno < logging.WARNING)
    routine.setFormatter(fmt)

    problems = logging.StreamHandler(sys.stderr)
    problems.setLevel(logging.WARNING)
    problems.setFormatter(fmt)

    logging.basicConfig(level=logging.INFO, handlers=[routine, problems], force=True)


def main() -> int:
    setup_logging()

    tunnels = []
    threads = []
    for host_config in conf.hosts:
        tunnel = SSHTunnel(host_config=host_config)
        thread = threading.Thread(
            target=tunnel.start_tunn,
            name=f"tunnel-{tunnel.local_port}",
            daemon=True,
        )
        thread.start()
        tunnels.append(tunnel)
        threads.append(thread)
        # The tunnel logs when it is actually up; do not claim success here.
        logger.info(
            "starting tunnel 127.0.0.1:%s -> %s (ssh %s)",
            tunnel.local_port,
            host_config.get("remote_port"),
            host_config.get("hostname"),
        )

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("received signal %s - shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not stop.wait(1.0):
        if not any(thread.is_alive() for thread in threads):
            logger.error("all tunnels exited unexpectedly")
            return 1

    for tunnel in tunnels:
        tunnel.stop()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
    logger.info("all tunnels stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
