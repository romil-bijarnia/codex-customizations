#!/usr/bin/env python3
"""Install published personal Codex builds; never merge or compile on the laptop."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from common import ROOT, CONFIG, REPO, TARGET, api, atomic_link, atomic_write, output, patch_id, release_tag, run, sha256

HOME = Path.home()
CODEX_HOME = Path(os.environ.get('CODEX_HOME', HOME / '.codex')).expanduser()
PACKAGES = CODEX_HOME / 'packages/standalone'
STATE = CODEX_HOME / 'state/codex-customizations'
STATE_FILE = STATE / 'installed.json'


def read_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def platform_check():
    if platform.system() != 'Linux' or platform.machine() not in ('aarch64', 'arm64'):
        raise RuntimeError('This installation targets the current Mac running Linux/ARM64. A native macOS build is not selected automatically.')


def published(tag):
    result = subprocess.run(['gh', 'api', f'repos/{REPO}/releases/tags/{tag}'], capture_output=True, text=True)
    if result.returncode:
        if '404' in result.stderr or 'Not Found' in result.stdout:
            return None
        raise RuntimeError(result.stderr.strip())
    data = json.loads(result.stdout)
    if data.get('draft'):
        return None
    required = {'release-manifest.json', f'codex-custom-{TARGET}.tar.gz'}
    return data if required <= {a['name'] for a in data['assets']} else None


def download_release(tag, folder):
    run(['gh', 'release', 'download', tag, '--repo', REPO, '--pattern', 'release-manifest.json', '--dir', folder])
    manifest = json.loads((folder / 'release-manifest.json').read_text())
    if manifest['target'] != TARGET or manifest['patchset'] != patch_id():
        raise RuntimeError('The published build does not match this platform and stored patch set.')
    if manifest['release_tag'] != tag:
        raise RuntimeError('Published manifest has the wrong release identity.')
    asset = f'codex-custom-{TARGET}.tar.gz'
    if manifest['asset'] != asset:
        raise RuntimeError('Unexpected package asset name.')
    run(['gh', 'release', 'download', tag, '--repo', REPO, '--pattern', asset, '--dir', folder])
    archive = folder / asset
    if sha256(archive) != manifest['archive_sha256']:
        raise RuntimeError('Package checksum mismatch; the installed version was not changed.')
    return manifest, archive


def extract(archive, destination):
    with tarfile.open(archive, 'r:gz') as bundle:
        # The stdlib data filter rejects traversal and links outside the package.
        bundle.extractall(destination, filter='data')
    package = destination / 'package'
    if not (package / 'codex-package.json').is_file():
        raise RuntimeError('The download is not a complete Codex package.')
    return package


def verify_package(package, manifest):
    metadata = json.loads((package / 'codex-package.json').read_text())
    if metadata['target'] != TARGET or metadata['version'] != manifest['version']:
        raise RuntimeError('Package metadata does not match the published build.')
    for name in ('codex', 'codex-exec-server', 'codex-code-mode-host'):
        if sha256(package / 'bin' / name) != manifest['binary_sha256'][name]:
            raise RuntimeError(f'Package executable checksum mismatch: {name}')
    actual = output([package / 'bin/codex', '--version'])
    if manifest['version'] not in actual:
        raise RuntimeError(f'The downloaded executable reported an unexpected version: {actual}')


def select_package(package, manifest):
    """Check before selection; roll back in memory if committing the install fails."""
    verify_package(package, manifest)
    current = PACKAGES / 'current'
    previous = current.resolve(strict=True)
    destination = PACKAGES / 'releases' / ('custom-' + manifest['release_tag'])
    if destination.exists():
        verify_package(destination, manifest)
    else:
        shutil.move(str(package), destination)
    helper_pairs = [
        ('api-cli/codex-api-chat.py', HOME / '.local/bin/codex-api-chat'),
        ('visualize-cli/visualize.py', CODEX_HOME / 'local-overrides/visualize-cli/visualize.py'),
    ]
    previous_helpers = []
    PACKAGES.mkdir(parents=True, exist_ok=True)
    with (PACKAGES / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if current.resolve() != previous:
            raise RuntimeError('Another installer changed Codex; retry instead of overwriting it.')
        try:
            for relative, target in helper_pairs:
                source = destination / 'customizations' / relative
                if source.is_file():
                    previous_helpers.append((target, target.read_bytes() if target.exists() else None))
                    atomic_write(target, source.read_bytes(), 0o755)
            atomic_link(current, destination)
            installed = dict(manifest, package=str(destination), installed_at=time.time())
            atomic_write(STATE_FILE, json.dumps(installed, indent=2) + '\n')
        except BaseException:
            atomic_link(current, previous)
            for target, content in previous_helpers:
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_write(target, content, 0o755)
            raise
    return destination


def refresh_kit():
    if not (ROOT / '.git').is_dir():
        raise RuntimeError('Install the small customization repository before updating.')
    if output(['git', '-C', ROOT, 'status', '--porcelain']):
        raise RuntimeError(f'Customization files have uncommitted changes in {ROOT}; they were not overwritten.')
    before = output(['git', '-C', ROOT, 'rev-parse', 'HEAD'])
    run(['git', '-C', ROOT, 'fetch', '--quiet', 'origin', 'main', '--depth=1'])
    run(['git', '-C', ROOT, 'merge', '--quiet', '--ff-only', 'origin/main'])
    return before != output(['git', '-C', ROOT, 'rev-parse', 'HEAD'])


def status():
    print(json.dumps({'repository': REPO, 'channel': 'upstream main', 'patchset': patch_id(),
                      'installed': read_state(), 'selected_package': str((PACKAGES / 'current').resolve()),
                      'source_checkout_required': False}, indent=2))


def install_launcher():
    platform_check()
    selected = (PACKAGES / 'current/bin/codex').resolve(strict=True)
    launcher = HOME / '.local/bin/codex'
    own = ROOT / 'bin/codex'
    if launcher.exists() and launcher.resolve() not in (selected, own.resolve()):
        raise RuntimeError(f'The existing launcher is not the selected personal Codex: {launcher}')
    own.chmod(0o755)
    atomic_link(launcher, own)
    print(f'Installed codex update; ordinary Codex launches still execute {selected}.')


def update(args):
    platform_check()
    if not args.kit_refreshed and refresh_kit():
        os.execv(sys.executable, [sys.executable, str(ROOT / 'manage.py'), 'update', '--kit-refreshed', *sys.argv[2:]])
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another codex update is running; the working CLI was not changed.')
        commit = api(f"repos/{CONFIG['upstream_repository']}/commits/{CONFIG['upstream_branch']}")['sha']
        digest = patch_id()
        tag = release_tag(commit, digest)
        state = read_state()
        release = published(tag)
        info = {'upstream_commit': commit, 'patchset': digest, 'release_tag': tag,
                'build_available': bool(release), 'already_installed': state.get('release_tag') == tag,
                'source_checkout_required': False}
        if args.check:
            print(json.dumps(info, indent=2))
            return
        if info['already_installed']:
            print('Codex is already current with upstream development and your stored changes.')
            return
        if not release:
            print('A matching build is not published yet. Requesting the automated remote build.', flush=True)
            # CLI handles nested JSON input without exposing credentials in arguments.
            run(['gh', 'workflow', 'run', 'build.yml', '--repo', REPO, '--ref', 'main', '-f', f'upstream={commit}'])
            if args.no_wait:
                print(f'Build requested. Run codex update again when ready. https://github.com/{REPO}/actions')
                return
            started = time.monotonic()
            last_message = None
            while not release:
                if time.monotonic() - started > args.timeout:
                    raise RuntimeError(f'Remote build is still pending: https://github.com/{REPO}/actions . Your installation is unchanged.')
                runs = api(f'repos/{REPO}/actions/workflows/build.yml/runs?per_page=10')['workflow_runs']
                matching = [r for r in runs if commit[:12] in r.get('display_title', '')]
                if matching:
                    latest = matching[0]
                    message = latest['status'] + ' ' + latest['html_url']
                    if latest['status'] == 'completed' and latest.get('conclusion') not in ('success', 'skipped'):
                        raise RuntimeError(f"The automated build failed ({latest.get('conclusion')}): {latest['html_url']} . Your installation is unchanged.")
                else:
                    message = f'Waiting for GitHub Actions: https://github.com/{REPO}/actions'
                if message != last_message:
                    print(message, flush=True)
                    last_message = message
                time.sleep(15)
                release = published(tag)
        PACKAGES.joinpath('releases').mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.codex-update-', dir=PACKAGES / 'releases') as temp:
            folder = Path(temp)
            print('Downloading the verified combined package; no source checkout or local compilation.', flush=True)
            manifest, archive = download_release(tag, folder)
            if manifest['upstream_commit'] != commit:
                raise RuntimeError('Published source revision does not match the requested update.')
            destination = select_package(extract(archive, folder), manifest)
        print(f"Installed {manifest['version']} at {destination}. Existing sessions were not restarted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    u = commands.add_parser('update', help='Install upstream development plus your recorded customizations')
    u.add_argument('--check', action='store_true', help='Show availability without requesting a build or installing')
    u.add_argument('--no-wait', action='store_true', help='Request a missing build and return without installing')
    u.add_argument('--timeout', type=int, default=7200, help='Maximum seconds to wait for a remote build')
    u.add_argument('--kit-refreshed', action='store_true', help=argparse.SUPPRESS)
    commands.add_parser('status', help='Show the selected build and update channel')
    commands.add_parser('install-launcher', help='Enable codex update without changing the selected executable')
    args = parser.parse_args()
    try:
        {'update': lambda: update(args), 'status': status, 'install-launcher': install_launcher}[args.command]()
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print(f'codex update: {error}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
