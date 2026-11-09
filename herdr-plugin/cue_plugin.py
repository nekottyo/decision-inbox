#!/usr/bin/env python3
"""Herdr entrypoints for the standalone Cue TUI."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import AppError, object_fields, parse_json, tui_web_url


def configuration():
    directory = os.environ.get('HERDR_PLUGIN_CONFIG_DIR')
    if not directory:
        return {}
    path = Path(directory) / 'config.json'
    if not path.exists():
        return {}
    with path.open('rb') as handle:
        raw = handle.read(4097)
    if len(raw) > 4096:
        raise AppError(400, 'Cue の plugin 設定が大きすぎます')
    value = parse_json(raw)
    object_fields(value, (), ('web_url',))
    if 'web_url' in value:
        value['web_url'] = tui_web_url(value['web_url'])
    return value


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in (['open'], ['tui']):
        print('Usage: cue_plugin.py open|tui', file=sys.stderr)
        return 2
    try:
        config = configuration()
        if arguments == ['open']:
            herdr = os.environ.get('HERDR_BIN_PATH') or 'herdr'
            return subprocess.run([herdr, 'plugin', 'pane', 'open', '--plugin', 'nekottyo.cue', '--entrypoint', 'desk'], check=False).returncode
        command = [sys.executable, str(ROOT / 'app.py'), 'tui']
        if config.get('web_url'):
            command.extend(['--web-url', config['web_url']])
        os.execv(sys.executable, command)
    except (AppError, OSError) as error:
        message = str(error) if isinstance(error, AppError) else 'Cue の plugin を起動できませんでした'
        print(json.dumps({'error': message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
