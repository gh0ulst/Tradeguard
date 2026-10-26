"""Launchd supervisor that does not restart the watcher after a safety halt."""
import subprocess
import sys
from pathlib import Path


def main():
    watcher = Path(__file__).resolve().with_name('auto_sl.py')
    result = subprocess.run(
        [sys.executable, '-u', str(watcher), '--execute', '--unattended'],
        check=False,
    )
    if result.returncode == 42:
        print('Watcher safety halt; automatic restart suppressed.', flush=True)
        return 0
    return result.returncode if result.returncode >= 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
