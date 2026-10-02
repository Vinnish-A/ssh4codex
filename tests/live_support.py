"""Private storage for opt-in integration tests; never part of the runtime."""
import json
import os
from pathlib import Path

PRIVATE_ROOT = Path(os.environ.get('SSH4CODEX_TEST_STATE', '~/.local/state/ssh4codex/tests')).expanduser()
REPORTS = Path(os.environ.get('SSH4CODEX_REPORTS', '~/.local/state/ssh4codex/reports')).expanduser()


def write_report(name, payload):
    REPORTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = REPORTS / name
    path.write_text(json.dumps(payload, indent=2) + '\n')
    path.chmod(0o600)
    return path
