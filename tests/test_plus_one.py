"""Thin-client protocol checks against a disposable local backend, never paid inference."""
import contextlib
import io
import json
import os
from pathlib import Path
import pty
import runpy
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / 'bin/codex+1'
WRAPPER = ROOT / 'bin/codex'

BACKEND = r'''#!/usr/bin/env python3
import json
import os
import signal
import sys

def record(value):
    with open(os.environ['PLUS_ONE_TEST_LOG'], 'a') as log:
        log.write(json.dumps(value) + '\n')

def emit(value):
    print(json.dumps(value), flush=True)

def stop(signum, frame):
    record({'stopped': signum})
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
record({'started': os.getpid(), 'argv': sys.argv[1:],
        'inherited_session': 'CODEX_THREAD_ID' in os.environ or 'CODEX_SESSION_ID' in os.environ})
if 'app-server' not in sys.argv[1:]:
    print(json.dumps(sys.argv[1:]))
    sys.exit(0)
mode = os.environ.get('PLUS_ONE_TEST_MODE', 'success')
for line in sys.stdin:
    request = json.loads(line)
    record(request)
    method = request.get('method')
    if method == 'initialized':
        continue
    if method == 'initialize':
        emit({'id': request['id'], 'result': {'userAgent': 'fake'}})
    elif method == 'config/read':
        emit({'id': request['id'], 'result': {'config': {
            'mcp_servers': {'extra': {'command': 'unused'}, 'project': {'command': 'also unused'}}}}})
    elif method == 'thread/start':
        emit({'id': request['id'], 'result': {'thread': {'id': 'test-thread'},
              'model': 'other-model' if mode == 'wrong-model' else 'gpt-5.6-luna',
              'reasoningEffort': 'medium' if mode == 'wrong-effort' else 'xhigh'}})
    elif method == 'turn/start':
        if mode == 'rpc-error':
            emit({'id': request['id'], 'error': {'code': -1, 'message': 'Daybreak access denied'}})
            continue
        if mode == 'disconnect':
            sys.exit(4)
        done = {'method': 'turn/completed', 'params': {'turn': {
            'id': 'test-turn', 'status': 'failed' if mode == 'failed' else 'completed',
            'error': {'message': 'editing failed'} if mode == 'failed' else None}}}
        if mode == 'early-completion':
            emit(done)
        emit({'id': request['id'], 'result': {'turn': {'id': 'test-turn', 'status': 'inProgress'}}})
        if mode == 'rerouted':
            emit({'method': 'model/rerouted', 'params': {'toModel': 'other-model'}})
        elif mode == 'error':
            emit({'method': 'error', 'params': {'error': {'message': 'provider unavailable'}, 'willRetry': False}})
        elif mode == 'question':
            emit({'id': 999, 'method': 'item/tool/requestUserInput', 'params': {}})
        else:
            if mode == 'retry':
                emit({'method': 'error', 'params': {'error': {'message': 'retrying'}, 'willRetry': True}})
            emit({'method': 'item/agentMessage/delta', 'params': {'delta': 'fixture complete'}})
            if mode != 'early-completion':
                emit(done)
'''


class InteractiveInput(io.StringIO):
    def isatty(self):
        return True


class PlusOneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='codex-plus-test-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.profile = self.home / '.codex'
        self.native = self.profile / 'packages/standalone/current/bin/codex'
        self.native.parent.mkdir(parents=True)
        self.native.write_text(BACKEND)
        self.native.chmod(0o755)
        self.log = self.home / 'messages.jsonl'
        self.config = self.profile / 'config.toml'
        self.config.write_text('model = "ordinary-model"\n[mcp_servers.extra]\ncommand = "unused"\n')
        self.env = dict(os.environ, CODEX_HOME=str(self.profile), PLUS_ONE_TEST_LOG=str(self.log),
                        CODEX_THREAD_ID='parent-thread', CODEX_SESSION_ID='parent-session')
        self.env.pop('CODEX_PLUS_CWD', None)

    def run_client(self, args=(), text='', mode='success', command=CLIENT, **environment):
        return subprocess.run([sys.executable, str(command), *args], input=text,
            text=True, capture_output=True, timeout=10,
            env=dict(self.env, PLUS_ONE_TEST_MODE=mode, **environment))

    def messages(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def requests(self, method):
        return [item['params'] for item in self.messages() if item.get('method') == method]

    def assert_backend_stopped(self):
        events = self.messages()
        pid = next(item['started'] for item in events if 'started' in item)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_pins_protocol_and_disables_extras_without_touching_profile(self):
        before = self.config.read_bytes()
        result = self.run_client(args=['change the launcher'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'fixture complete\n')
        startup = self.messages()[0]
        self.assertIn('app-server', startup['argv'])
        self.assertIn('--stdio', startup['argv'])
        self.assertFalse(startup['inherited_session'])
        self.assertTrue(self.requests('initialize')[0]['capabilities']['experimentalApi'])
        self.assertEqual([event.get('method') for event in self.messages() if 'method' in event],
                         ['initialize', 'initialized', 'config/read', 'thread/start', 'turn/start'])
        thread = self.requests('thread/start')[0]
        self.assertEqual(thread['model'], 'gpt-5.6-luna')
        self.assertFalse(thread['allowProviderModelFallback'])
        self.assertEqual(thread['approvalPolicy'], 'never')
        self.assertEqual(thread['sandbox'], 'danger-full-access')
        self.assertTrue(thread['ephemeral'])
        self.assertEqual(thread['cwd'], str(ROOT))
        self.assertNotEqual(thread.get('environments'), [])
        self.assertEqual(self.requests('config/read')[0], {'cwd': str(ROOT), 'includeLayers': False})
        self.assertEqual(thread['developerInstructions'], '')
        self.assertIn('launcher', thread['baseInstructions'])
        for key in ('features.plugins', 'features.apps', 'features.hooks', 'features.multi_agent',
                    'features.memories', 'cloud.skills.enabled', 'skills.include_instructions',
                    'skills.bundled.enabled', 'mcp_servers.extra.enabled', 'mcp_servers.project.enabled'):
            self.assertIs(thread['config'][key], False, key)
        self.assertEqual(thread['config']['model_reasoning_effort'], 'xhigh')
        self.assertEqual(self.config.read_bytes(), before)
        self.assert_backend_stopped()

    def test_multiline_stdin_is_one_unchanged_prompt(self):
        text = 'Change this function:\n\ndef f(x):\n    return "a b"\n\nKeep the final newline.\n'
        result = self.run_client(text=text)
        self.assertEqual(result.returncode, 0, result.stderr)
        turns = self.requests('turn/start')
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]['input'], [{'type': 'text', 'text': text}])
        self.assertEqual(turns[0]['threadId'], 'test-thread')
        self.assertEqual(turns[0]['model'], 'gpt-5.6-luna')
        self.assertEqual(turns[0]['effort'], 'xhigh')
        self.assertEqual(turns[0]['cyberAccessProgram'], 'daybreakBlue')

    def test_interactive_request_pins_configuration_and_exits_without_followup(self):
        output = io.StringIO()
        with patch.dict(os.environ, self.env), patch.object(sys, 'argv', [str(CLIENT)]), \
             patch.object(sys, 'stdin', InteractiveInput()), \
             patch('builtins.input', side_effect=['first request', AssertionError('No follow-up prompt')]) as read, \
             contextlib.redirect_stdout(output):
            runpy.run_path(str(CLIENT), run_name='__main__')
        turns = self.requests('turn/start')
        self.assertEqual(read.call_count, 1)
        self.assertEqual([turn['input'][0]['text'] for turn in turns], ['first request'])
        self.assertTrue(all(turn['model'] == 'gpt-5.6-luna' and turn['effort'] == 'xhigh'
                            and turn['cyberAccessProgram'] == 'daybreakBlue' for turn in turns))
        self.assertEqual(output.getvalue(), 'fixture complete\n')
        self.assert_backend_stopped()

    def test_interactive_eof_exits_without_starting_a_turn(self):
        with patch.dict(os.environ, self.env), patch.object(sys, 'argv', [str(CLIENT)]), \
             patch.object(sys, 'stdin', InteractiveInput()), patch('builtins.input', side_effect=EOFError):
            runpy.run_path(str(CLIENT), run_name='__main__')
        self.assertFalse(self.requests('turn/start'))
        self.assert_backend_stopped()

    def test_empty_stdin_does_not_start_a_turn(self):
        result = self.run_client(text=' \n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.requests('turn/start'))
        self.assert_backend_stopped()

    def test_terminal_bracketed_paste_is_preserved_then_client_exits(self):
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, str(CLIENT)], stdin=slave, stdout=slave,
            stderr=slave, env=self.env, start_new_session=True)
        os.close(slave)
        output = bytearray()

        def wait_for(needle, start=0):
            deadline = time.monotonic() + 5
            while needle not in output[start:]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.fail(f'Terminal did not emit {needle!r}: {bytes(output)!r}')
                if select.select([master], [], [], remaining)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        chunk = b''
                    if not chunk:
                        self.fail(f'Terminal disconnected: {bytes(output)!r}')
                    output.extend(chunk)

        try:
            wait_for(b'> ')
            first = 'Change this:\n\ndef f(x):\n    return "a b"\n'
            marker = len(output)
            os.write(master, b'\x1b[200~' + first.encode() + b'\x1b[201~\r')
            wait_for(b'fixture complete', marker)
            self.assertEqual(process.wait(timeout=5), 0)
            turns = self.requests('turn/start')
            self.assertEqual([turn['input'][0]['text'] for turn in turns], [first])
            self.assert_backend_stopped()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            os.close(master)

    def test_working_directory_override(self):
        result = self.run_client(args=['test'], CODEX_PLUS_CWD=str(self.home))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.requests('thread/start')[0]['cwd'], str(self.home))

    def test_model_or_reasoning_mismatch_fails_before_any_turn(self):
        for mode in ('wrong-model', 'wrong-effort'):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                result = self.run_client(args=['test'], mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Backend did not accept GPT-5.6-Luna / xhigh', result.stderr)
                self.assertFalse(self.requests('turn/start'))
                self.assert_backend_stopped()

    def test_backend_errors_fail_and_do_not_change_model_or_program(self):
        for mode, error in (('rpc-error', 'Daybreak access denied'), ('rerouted', 'rerouted'),
                            ('failed', 'editing failed'), ('disconnect', 'disconnected'),
                            ('error', 'provider unavailable'), ('question', 'Unexpected backend request')):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                result = self.run_client(args=['test'], mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.assertEqual(len(self.requests('thread/start')), 1)
                self.assertEqual(len(self.requests('turn/start')), 1)
                self.assert_backend_stopped()

    def test_retry_notification_is_not_a_final_failure(self):
        result = self.run_client(args=['test'], mode='retry')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'fixture complete\n')

    def test_completion_before_rpc_response_is_not_lost(self):
        result = self.run_client(args=['test'], mode='early-completion')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_backend_stopped()

    def test_help_does_not_start_backend(self):
        result = self.run_client(args=['--help'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GPT-5.6-Luna / xhigh / Daybreak Blue', result.stdout)
        self.assertFalse(self.log.exists())

    def test_wrapper_aliases_preserve_quoted_and_multiline_prompts(self):
        prompt = 'Edit this:\n  print("a b")\n'
        for prefix in (['+', '1'], ['+1']):
            with self.subTest(prefix=prefix):
                self.log.unlink(missing_ok=True)
                result = self.run_client(args=[*prefix, prompt], command=WRAPPER)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.requests('turn/start')[0]['input'][0]['text'], prompt)

    def test_wrapper_alias_help_does_not_open_native_cli(self):
        for prefix in (['+', '1'], ['+1']):
            with self.subTest(prefix=prefix):
                result = self.run_client(args=[*prefix, '--help'], command=WRAPPER)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Usage: codex+1', result.stdout)
                self.assertFalse(self.log.exists())

    def test_ordinary_wrapper_arguments_are_untouched(self):
        arguments = ['exec', 'a\nb with spaces', '--literal', '+', '2']
        result = self.run_client(args=arguments, command=WRAPPER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)
        self.assertFalse(any(item.get('method') for item in self.messages()))

    def test_installer_is_idempotent_and_preserves_profile(self):
        sys.path.insert(0, str(ROOT))
        import manage
        before = self.config.read_bytes()
        with patch.multiple(manage, HOME=self.home, CODEX_HOME=self.profile,
                            PACKAGES=self.profile / 'packages/standalone'), \
             patch.object(manage, 'platform_check'), contextlib.redirect_stdout(io.StringIO()):
            manage.install_launcher()
            manage.install_launcher()
        self.assertEqual((self.home / '.local/bin/codex').resolve(), WRAPPER.resolve())
        self.assertEqual((self.home / '.local/bin/codex+1').resolve(), CLIENT.resolve())
        self.assertEqual(self.config.read_bytes(), before)

    def test_installer_refuses_unrelated_plus_one_before_changing_launcher(self):
        sys.path.insert(0, str(ROOT))
        import manage
        launcher = self.home / '.local/bin/codex'
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(self.native)
        plus = launcher.with_name('codex+1')
        plus.write_text('unrelated user command\n')
        with patch.multiple(manage, HOME=self.home, CODEX_HOME=self.profile,
                            PACKAGES=self.profile / 'packages/standalone'), \
             patch.object(manage, 'platform_check'):
            with self.assertRaisesRegex(RuntimeError, 'unrelated'):
                manage.install_launcher()
        self.assertEqual(launcher.resolve(), self.native.resolve())
        self.assertEqual(plus.read_text(), 'unrelated user command\n')


if __name__ == '__main__':
    unittest.main()
