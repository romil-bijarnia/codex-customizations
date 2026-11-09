"""Small shared primitives; no upstream source is stored in this repository."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / 'manifest.json').read_text())
REPO = CONFIG['repository']
TARGET = CONFIG['target']


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def output(args, **kwargs):
    return subprocess.check_output([str(x) for x in args], text=True, **kwargs).strip()


def api(path, method='GET', fields=None):
    args = ['gh', 'api', path]
    if method != 'GET':
        args += ['--method', method]
    for key, value in (fields or {}).items():
        args += ['-f', f'{key}={value}']
    return json.loads(output(args))


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def patch_id():
    digest = hashlib.sha256()
    for path in sorted([ROOT / 'manifest.json', *list((ROOT / 'patches').rglob('*')), *list((ROOT / 'overlays').rglob('*'))]):
        if path.is_file():
            digest.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def release_tag(commit, digest=None):
    return f'dev-{commit[:12]}-{(digest or patch_id())[:12]}'


def atomic_write(path, content, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content.encode() if isinstance(content, str) else content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.chmod(existing_mode if mode is None else mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_link(path, destination):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.new-{os.getpid()}')
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(destination)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
