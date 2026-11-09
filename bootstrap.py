#!/usr/bin/env python3
"""Publish the already-verified installed build, without pretending to recompile it."""
import json
from pathlib import Path
import sys

from common import CONFIG, ROOT, atomic_write, patch_id, sha256
from build import archive_package, package_manifest, plan, publish

home = Path.home()
package = (home / '.codex/packages/standalone/current').resolve(strict=True)
receipt = json.loads((package / 'romil-build.json').read_text())
assert receipt['source']['head'] == CONFIG['bootstrap_source_commit']
assert sha256(package / 'bin/codex') == receipt['executable_sha256']
for name, digest in receipt['companion_sha256'].items():
    assert sha256(package / 'bin' / name) == digest
assert receipt['suites_passed'] == ['defaults', 'picker', 'integrations']
assert receipt['additional_suites_passed'] == ['regressions', 'steering']
manifest = package_manifest(package, plan(CONFIG['base_commit']), build_kind='reused-verified-bootstrap',
                            original_native_source_commit=CONFIG['bootstrap_source_commit'],
                            check_suites=receipt['suites_passed'] + receipt['additional_suites_passed'],
                            real_model_requests=0, kernel_activation_verified=True)
# The bootstrap archive uses the current owned helper files, which match the source overlay.
manifest = archive_package(package, Path(sys.argv[1]), manifest)
publish(Path(sys.argv[1]), manifest)
state = home / '.codex/state/codex-customizations/installed.json'
atomic_write(state, json.dumps(dict(manifest, package=str(package), adopted_existing_installation=True), indent=2) + '\n')
print(json.dumps(manifest, indent=2))
