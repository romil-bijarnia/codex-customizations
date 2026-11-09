#!/usr/bin/env python3
"""Mechanical patch application and CI build. All upstream trees are temporary."""
import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

from common import ROOT, CONFIG, REPO, TARGET, api, atomic_write, output, patch_id, release_tag, run, sha256


def plan(commit=None):
    commit = commit or api(f"repos/{CONFIG['upstream_repository']}/commits/{CONFIG['upstream_branch']}")['sha']
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise RuntimeError('Upstream must be a complete commit SHA, not a shell expression or branch supplied by a third party.')
    digest = patch_id()
    return {'upstream_commit': commit, 'patchset': digest, 'release_tag': release_tag(commit, digest), 'target': TARGET}


def prepare(source, commit):
    source = Path(source)
    source.mkdir(parents=True, exist_ok=False)
    run(['git', 'init', '-q', source])
    run(['git', '-C', source, 'remote', 'add', 'origin', 'https://github.com/' + CONFIG['upstream_repository'] + '.git'])
    for revision in dict.fromkeys((CONFIG['base_commit'], commit)):
        run(['git', '-C', source, 'fetch', '--quiet', '--depth=1', 'origin', revision])
    run(['git', '-C', source, '-c', 'advice.detachedHead=false', 'checkout', '--quiet', '--detach', commit])
    for patch in CONFIG['patches']:
        result = subprocess.run(['git', '-C', str(source), 'apply', '--3way', '--index', str(ROOT / patch)], capture_output=True, text=True)
        if result.returncode:
            failed = output(['git', '-C', source, 'diff', '--name-only', '--diff-filter=U'])
            raise RuntimeError('Stored patch did not apply. No working installation is changed.\n' + failed + '\n' + result.stderr[-6000:])
    shutil.copytree(ROOT / 'overlays', source, dirs_exist_ok=True)
    return source


def official_resources(folder):
    name = 'codex-package-' + CONFIG['resource_target'] + '.tar.gz'
    for release in api(f"repos/{CONFIG['upstream_repository']}/releases?per_page=15"):
        asset = next((a for a in release['assets'] if a['name'] == name), None)
        if asset:
            run(['gh', 'release', 'download', release['tag_name'], '--repo', CONFIG['upstream_repository'], '--pattern', name, '--dir', folder])
            path = folder / name
            expected = asset.get('digest') or ''
            if expected.startswith('sha256:') and sha256(path) != expected[7:]:
                raise RuntimeError('Official resource package failed its published checksum.')
            return release, path
    raise RuntimeError('No official package with the matching Linux ARM64 resources was found.')


def archive_package(package, output_dir, manifest):
    output_dir.mkdir(parents=True, exist_ok=True)
    asset = output_dir / f'codex-custom-{TARGET}.tar.gz'
    def public_package(info):
        if info.name == 'package/romil-build.json':
            return None  # Local source/log paths are not release artifacts.
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        return info
    with tarfile.open(asset, 'w:gz', compresslevel=2) as bundle:
        bundle.add(package, arcname='package', filter=public_package)
        if not (package / 'custom-build.json').exists():
            content = (json.dumps(manifest, indent=2) + '\n').encode()
            info = tarfile.TarInfo('package/custom-build.json');info.size = len(content);info.mode = 0o644
            bundle.addfile(info, io.BytesIO(content))
    manifest = dict(manifest, asset=asset.name, archive_sha256=sha256(asset))
    (output_dir / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def package_manifest(package, build_plan, **extra):
    return dict(build_plan, version=json.loads((package / 'codex-package.json').read_text())['version'],
                binary_sha256={name: sha256(package / 'bin' / name) for name in ('codex', 'codex-exec-server', 'codex-code-mode-host')},
                repository=REPO, created_at=datetime.now(timezone.utc).isoformat(), **extra)


def publish(output_dir, manifest):
    tag = manifest['release_tag']
    body = (f"Upstream: openai/codex@{manifest['upstream_commit']}\n\n"
            f"Stored changes: {manifest['patchset']}\n\n"
            "Built automatically; no agent-mediated merge. Profile settings and running sessions are not replaced.\n")
    args = ['gh', 'release', 'create', tag, '--repo', REPO, '--target', output(['git', '-C', ROOT, 'rev-parse', 'HEAD']),
            '--prerelease', '--latest=false', '--title', manifest['version'], '--notes', body,
            output_dir / manifest['asset'], output_dir / 'release-manifest.json']
    run(args)


def build(work, build_plan, output_dir):
    work.mkdir(parents=True, exist_ok=False)
    source = prepare(work / 'source', build_plan['upstream_commit'])
    release, resource_archive = official_resources(work)
    version_base = re.match(r'rust-v(\d+\.\d+\.\d+)', release['tag_name']).group(1)
    # Constant within an upstream series: do not invalidate every workspace crate for each timestamp.
    cargo_version = version_base + '-dev'
    import tomllib
    cargo = source / 'codex-rs/Cargo.toml'
    old = tomllib.loads(cargo.read_text())['workspace']['package']['version']
    text = cargo.read_text();section = text.index('[workspace.package]');tail = text[section:]
    tail = tail.replace(f'version = "{old}"', f'version = "{cargo_version}"', 1)
    cargo.write_text(text[:section] + tail)
    lockfile = source / 'codex-rs/Cargo.lock'
    pieces = lockfile.read_text().split('[[package]]')
    for i in range(1, len(pieces)):
        entry = tomllib.loads('[[package]]' + pieces[i])['package'][0]
        if entry.get('version') == old and 'source' not in entry:
            pieces[i] = pieces[i].replace(f'version = "{old}"', f'version = "{cargo_version}"', 1)
    lockfile.write_text('[[package]]'.join(pieces))
    toolchain = tomllib.loads((source / 'codex-rs/rust-toolchain.toml').read_text())['toolchain']['channel']
    run(['rustup', 'toolchain', 'install', toolchain, '--profile', 'minimal'])
    env = dict(os.environ, RUSTUP_TOOLCHAIN=toolchain, CODEX_REPO_ROOT=str(source),
               CARGO_PROFILE_RELEASE_DEBUG='0', CARGO_PROFILE_RELEASE_STRIP='symbols',
               CARGO_PROFILE_RELEASE_LTO='false', CARGO_PROFILE_RELEASE_CODEGEN_UNITS='16',
               STABLE_GIT_COMMIT=build_plan['upstream_commit'])
    env['CARGO_TARGET_DIR'] = str(Path.home() / '.cache/codex-customizations/target')
    env['PATH'] = str(Path(output(['rustup', 'which', '--toolchain', toolchain, 'cargo'])).parent) + os.pathsep + env['PATH']
    os.environ['CODEX_REPO_ROOT'] = str(source)
    sys.path.insert(0, str(source / 'scripts'))
    from codex_package.targets import TARGET_SPECS
    from codex_package.v8 import resolve_codex_v8_cargo_env
    env.update(resolve_codex_v8_cargo_env(TARGET_SPECS[TARGET], environ=env, cache_root=Path.home() / '.cache/codex-customizations/v8'))
    command = ['cargo', 'build', '--locked', '--release', '--jobs', '2']
    for project, binary in [('codex-cli', 'codex'), ('codex-exec-server-cli', 'codex-exec-server'), ('codex-code-mode-host', 'codex-code-mode-host'), ('codex-bwrap', 'bwrap')]:
        command += ['-p', project, '--bin', binary]
    run(command, cwd=source / 'codex-rs', env=env)
    unpacked = work / 'official';unpacked.mkdir()
    with tarfile.open(resource_archive) as bundle:bundle.extractall(unpacked, filter='data')
    metadata_files = list(unpacked.rglob('codex-package.json'))
    if len(metadata_files) != 1:raise RuntimeError('Official package layout is ambiguous.')
    package = metadata_files[0].parent
    target = Path(env['CARGO_TARGET_DIR']) / 'release'
    for name in ('codex', 'codex-exec-server', 'codex-code-mode-host'):
        (package / 'bin' / name).unlink(missing_ok=True);shutil.copy2(target / name, package / 'bin' / name)
    (package / 'codex-resources/bwrap').unlink(missing_ok=True);shutil.copy2(target / 'bwrap', package / 'codex-resources/bwrap')
    metadata = json.loads(metadata_files[0].read_text())
    date = datetime.now(timezone.utc).strftime('%Y%m%d')
    metadata.update(target=TARGET, version=f"{version_base}-dev.{date}+romil.{build_plan['upstream_commit'][:12]}.{build_plan['patchset'][:8]}")
    metadata_files[0].write_text(json.dumps(metadata, indent=2) + '\n')
    shutil.copytree(ROOT / 'overlays/local-customizations', package / 'customizations')
    qa_home = work / 'qa-home';(qa_home / '.codex').mkdir(parents=True)
    models = json.loads((source / 'codex-rs/models-manager/models.json').read_text())
    if not any(m['slug'] == 'gpt-6.1-sol' for m in models['models']):raise RuntimeError('The upstream model catalog no longer contains the terminal fixture model.')
    (qa_home / '.codex/models_cache.json').write_text(json.dumps(models))
    for origin, destination in [('api-cli/codex-api-chat.py', qa_home / '.local/bin/codex-api-chat'), ('visualize-cli/visualize.py', qa_home / '.codex/local-overrides/visualize-cli/visualize.py')]:
        destination.parent.mkdir(parents=True, exist_ok=True);shutil.copy2(package / 'customizations' / origin, destination)
    python = Path(sys.executable)
    test_env = dict(os.environ, HOME=str(qa_home))
    test_env.pop('CODEX_HOME', None)
    suites = []
    for name, script, extra in [('defaults','defaults-qa.py',[]),('picker-display','picker-order-qa.py',['--display-only']),('integrations','terminal-integrations-qa.py',[]),('regressions','cli-regression-qa.py',['--source',str(source)]),('steering','steering-qa.py',[])]:
        run([python, ROOT / 'checks' / script, package / 'bin/codex', work / 'checks' / name, *extra], env=test_env)
        suites.append(name)
    manifest = package_manifest(package, build_plan, check_suites=suites, real_model_requests=0,
                                kernel_activation_verified=False, official_resources_tag=release['tag_name'], build_kind='remote-source-build')
    (package / 'custom-build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    manifest = archive_package(package, output_dir, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('plan');p.add_argument('--upstream');p.add_argument('--out', type=Path, required=True)
    p = commands.add_parser('prepare');p.add_argument('--upstream', required=True);p.add_argument('--source', type=Path, required=True)
    p = commands.add_parser('build');p.add_argument('--plan', type=Path, required=True);p.add_argument('--work', type=Path, required=True);p.add_argument('--out', type=Path, required=True);p.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if args.command == 'plan':
        data = plan(args.upstream);args.out.write_text(json.dumps(data, indent=2) + '\n')
        from manage import published
        exists = bool(published(data['release_tag']))
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as f:f.write(f"commit={data['upstream_commit']}\ntag={data['release_tag']}\nexists={str(exists).lower()}\n")
        print(json.dumps(dict(data, exists=exists), indent=2))
    elif args.command == 'prepare':
        prepare(args.source, args.upstream);print('Stored patch applied without an agent.')
    else:
        manifest = build(args.work, json.loads(args.plan.read_text()), args.out)
        if args.publish:publish(args.out, manifest)
        print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
