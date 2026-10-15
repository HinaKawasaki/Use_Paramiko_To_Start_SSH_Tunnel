# conf_example.py
"""Tunnel configuration.

Copy this file to ``conf.py`` and fill in your own values - ``conf.py`` is
git-ignored because it names your servers and points at your private key.
"""

# One entry per tunnel.  Everything in this list is started together by
# start_service.py, each in its own thread.
host1 = {
    "hostname": "socks-host.example.com",   # SSH server, IPv6 is fine
    "username": "your-name",
    "remote_port": 1080,                    # port to reach on the far side
    "local_port": 1080,                     # port opened on 127.0.0.1 here
    "key_filename": "/home/you/.ssh/id_ed25519",
    "sshd_port": 22,
}

host2 = {
    "hostname": "webdav-host.example.com",
    "username": "ubuntu",
    "remote_port": 8000,
    "local_port": 8000,
    "key_filename": "/home/you/.ssh/id_ed25519",
    "sshd_port": 22,
}

hosts = [host1, host2]

# Keys understood by SSHTunnel:
#
#   hostname      SSH server (required)
#   username      login name (required)
#   remote_port   port to reach on the far side (required)
#   local_port    port to open on 127.0.0.1 (required)
#   sshd_port     SSH port, default 22
#   target_host   what the far side connects to, default 127.0.0.1
#
# Authentication - the first one present wins:
#
#   "pkey": <paramiko.PKey>         an already loaded key object
#   "key_filename": "/path/to/key"  a private key file
#   "password": "..."               password authentication
