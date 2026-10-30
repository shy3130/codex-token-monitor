#!/usr/bin/env python3
"""Sync the dashboard to existing local installs, including matching plugin caches."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_file(source, target, mode):
    handle, temporary = tempfile.mkstemp(prefix='.monitor-ui-', dir=target.parent)
    os.close(handle)
    temporary = Path(temporary)
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-home', type=Path, default=Path.home()/'.codex/plugins')
    parser.add_argument('--runtime-dir', type=Path,
                        default=Path.home()/'.local/share/chatgpt-token-monitor')
    args = parser.parse_args()
    manifest = json.loads((ROOT/'plugin/plugin.json').read_text())
    name, version = manifest['name'], manifest['version']
    source = ROOT/'plugin/ui/monitor.html'
    candidates = [args.plugin_home/name/'ui/monitor.html',
                  args.runtime_dir/'plugin/ui/monitor.html']
    # The desktop client runs from a versioned cache rather than the source install.
    candidates.extend(sorted((args.plugin_home/'cache').glob(
        f'*/{name}/{version}/ui/monitor.html')))
    targets = list(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))
    if not targets:
        parser.error('No existing local UI copies found; use --plugin-home / --runtime-dir if needed.')
    expected = digest(source)
    changed = [path for path in targets if digest(path) != expected]
    if not changed:
        print(f'OK: all {len(targets)} installed UI copies already match the source.')
        return
    backup = Path(tempfile.mkdtemp(prefix='token-monitor-ui-sync-'))
    originals = {}
    for index, target in enumerate(changed):
        saved = backup/f'{index}-monitor.html'
        shutil.copy2(target, saved)
        originals[target] = (saved, stat.S_IMODE(target.stat().st_mode))
    (backup/'manifest.json').write_text(json.dumps(
        {str(target): str(saved) for target, (saved, _) in originals.items()}, indent=2)+'\n')
    updated = []
    try:
        for target, (saved, mode) in originals.items():
            if digest(target) != digest(saved):
                raise RuntimeError(f'UI changed during sync: {target}')
            replace_file(source, target, mode)
            updated.append(target)
        if not all(digest(target) == expected for target in targets):
            raise RuntimeError('An installed UI copy differs after sync')
    except BaseException:
        for target in updated:
            saved, mode = originals[target]
            replace_file(saved, target, mode)
        raise
    print(f'OK: all {len(targets)} installed UI copies match the source ({len(changed)} updated).')
    for target in targets:
        print(target)
    print('Backup:', backup)


if __name__ == '__main__':
    main()
