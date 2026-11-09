"""Focused filesystem checks for installation safety and CLI argument forwarding."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manage
from common import sha256


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.profile = self.home / '.codex'
        self.packages = self.profile / 'packages/standalone'
        old = self.packages / 'releases/old';old.mkdir(parents=True)
        self.packages.joinpath('current').symlink_to(old)
        self.old = old
        self.profile.joinpath('config.toml').write_text('personal = "unchanged"\n')
        self.profile.joinpath('sessions').mkdir()
        self.profile.joinpath('sessions/history.jsonl').write_text('existing conversation\n')
        state = self.profile / 'state/codex-customizations'
        replacements = dict(HOME=self.home, CODEX_HOME=self.profile, PACKAGES=self.packages, STATE=state, STATE_FILE=state / 'installed.json')
        self.addCleanup(patch.stopall)
        patch.multiple(manage, **replacements).start()

    def candidate(self):
        package = self.home / 'candidate';(package / 'bin').mkdir(parents=True)
        for name in ('codex', 'codex-exec-server', 'codex-code-mode-host'):
            p = package / 'bin' / name;p.write_text('#!/bin/sh\nprintf "codex-cli 2.3.4\\n"\n');p.chmod(0o755)
        (package / 'codex-package.json').write_text(json.dumps({'target':manage.TARGET,'version':'2.3.4'}))
        manifest = {'target':manage.TARGET,'version':'2.3.4','release_tag':'test-package','binary_sha256':{n:sha256(package / 'bin' / n) for n in ('codex','codex-exec-server','codex-code-mode-host')}}
        return package, manifest

    def test_success_preserves_profile_and_history(self):
        package, manifest = self.candidate()
        selected = manage.select_package(package, manifest)
        self.assertEqual((self.packages / 'current').resolve(), selected)
        self.assertEqual((self.profile / 'config.toml').read_text(), 'personal = "unchanged"\n')
        self.assertEqual((self.profile / 'sessions/history.jsonl').read_text(), 'existing conversation\n')
        self.assertTrue(self.old.is_dir())

    def test_invalid_download_does_not_change_working_selection(self):
        package, manifest = self.candidate();manifest['binary_sha256']['codex'] = '0' * 64
        with self.assertRaises(RuntimeError):manage.select_package(package, manifest)
        self.assertEqual((self.packages / 'current').resolve(), self.old)
        self.assertFalse(manage.STATE_FILE.exists())

    def test_archive_path_escape_is_rejected(self):
        archive = self.home / 'bad.tar.gz'
        with tarfile.open(archive,'w:gz') as f:
            item=tarfile.TarInfo('../outside');item.size=4;f.addfile(item,io.BytesIO(b'nope'))
        with self.assertRaises((tarfile.TarError, OSError)):
            manage.extract(archive,self.home / 'extract')
        self.assertFalse((self.home / 'outside').exists())

    def test_launcher_preserves_native_invocation(self):
        package, _ = self.candidate();(self.packages / 'current').unlink();(self.packages / 'current').symlink_to(package)
        env=dict(os.environ,CODEX_HOME=str(self.profile))
        result=subprocess.run([str(manage.ROOT / 'bin/codex'),'--version'],env=env,text=True,capture_output=True,check=True)
        self.assertEqual(result.stdout.strip(),'codex-cli 2.3.4')


if __name__ == '__main__':unittest.main()
