"""The Mac launcher must never become a second scheduled-sync owner."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_mac_launcher_disables_all_background_owners_even_if_inherited(tmp_path):
    launcher = tmp_path / 'start-local.sh'
    shutil.copy2(ROOT / 'start-local.sh', launcher)
    (tmp_path / '.env.app.references').write_text('# Test references only\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    # Stubs prevent a real tunnel, vault access or app launch.
    (bin_dir / 'uv').write_text('#!/bin/sh\nexit 0\n')
    (bin_dir / 'op').write_text(
        '#!/bin/sh\nprintf "%s\\n" "$SYNC_ENABLED" "$GARMIN_SYNC_ENABLED" '
        '"$WEBHOOK_WORKER_ENABLED" > "$TEST_FLAGS_FILE"\n'
    )
    for binary in bin_dir.iterdir():
        binary.chmod(0o700)
    flags = tmp_path / 'flags'
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
           'SYNC_ENABLED': 'true', 'GARMIN_SYNC_ENABLED': 'true',
           'WEBHOOK_WORKER_ENABLED': 'true', 'TEST_FLAGS_FILE': str(flags)}
    subprocess.run(['sh', str(launcher)], env=env, capture_output=True, check=True)
    assert flags.read_text().splitlines() == ['false', 'false', 'false']
