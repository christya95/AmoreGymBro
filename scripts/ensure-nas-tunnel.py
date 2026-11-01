"""Restore the private Mac-to-NAS database tunnel, with local 1Password approval."""
from pathlib import Path
import os
import shlex
import shutil
import socket
import subprocess
from urllib.parse import urlparse

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
config = dotenv_values(ROOT / '.env')
uri = urlparse(os.environ.get('MONGODB_URI') or config.get('MONGODB_URI') or '')
if uri.hostname not in {'127.0.0.1', 'localhost'} or uri.port != 27019:
    raise SystemExit(0)
try:
    with socket.create_connection(('127.0.0.1', 27019), timeout=1):
        pass
    raise SystemExit(0)
except OSError:
    pass
host = config.get('NAS_SSH_HOST') or '192.168.2.49'
user = config.get('NAS_SSH_USER') or 'Joss'
port = int(config.get('NAS_SSH_PORT') or '22')
try:
    with socket.create_connection((host, port), timeout=3):
        pass
except OSError:
    raise SystemExit('NAS SSH is off or unavailable. Enable LAN-only SSH in UGOS Control Panel → Terminal, then run ./start-local.sh again. Your database stays on the NAS.')
op = shutil.which('op')
if not op:
    raise SystemExit('Install 1Password CLI and unlock the desktop app to connect to the NAS.')
private = ROOT / 'data' / 'ssh'
private.mkdir(mode=0o700, parents=True, exist_ok=True)
private.chmod(0o700)
reference = config.get('NAS_SSH_PASSWORD_REF') or 'op://Personal/UGREEN PANEL/password'
askpass = private / 'nas-askpass.sh'
askpass.write_text('#!/bin/sh\nexec ' + shlex.quote(op) + ' read ' + shlex.quote(reference) + '\n')
askpass.chmod(0o700)
env = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE='force', DISPLAY='amoregymbro-local')
args = ['ssh', '-M', '-N', '-f', '-S', str(private / 'nas-db-control'),
    '-o', 'ControlPersist=yes', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
    '-o', 'StrictHostKeyChecking=yes', '-o', 'NumberOfPasswordPrompts=1',
    '-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password',
    '-p', str(port), '-L', '127.0.0.1:27019:127.0.0.1:27018', user + '@' + host]
try:
    result = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=90)
except subprocess.TimeoutExpired:
    raise SystemExit('NAS connection needs local 1Password unlock/approval. No password was printed.')
if result.returncode:
    raise SystemExit('NAS tunnel could not start. Check SSH access and approve the local 1Password prompt; no password belongs in chat.')
print('Private NAS database tunnel is ready.')
