#!/usr/bin/env python3
"""Offline updater contracts. Never source/execute the installers' entrypoints.

Only extracted function definitions run. Mock curl allows fixture URLs only;
service-control stubs abort immediately if preparation ever reaches mutation.
"""
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
COMMIT = 'a' * 40
OTHER = 'b' * 40
API = 'https://api.github.com/repos/LelandJin/3x-ui'
RAW = 'https://raw.githubusercontent.com/LelandJin/3x-ui'
ASSET = 'https://github.com/LelandJin/3x-ui/releases/download/dev-latest/x-ui-linux-amd64.tar.gz'


def function(name, script):
    return re.search(r'^' + re.escape(name) + r'\(\) [({]\n.*?^[})]\n',
                     (ROOT / script).read_text(), re.M | re.S).group()


class ForkUpdates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.routes = {}
        self.bin = self.base / 'mock-bin'
        self.bin.mkdir()
        mock = self.bin / 'curl'
        mock.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
url = next(a for a in args if a.startswith('https://'))
with open(os.environ['URL_LOG'], 'a') as log: log.write(url + '\\n')
routes = json.loads(pathlib.Path(os.environ['ROUTES']).read_text())
if url not in routes: sys.exit(22)
data = pathlib.Path(routes[url]).read_bytes()
if '-o' in args: pathlib.Path(args[args.index('-o')+1]).write_bytes(data)
else: sys.stdout.buffer.write(data)
''')
        mock.chmod(0o755)
        self.old = self.base / 'x-ui'
        self.old.mkdir()
        binary = self.old / 'x-ui'
        binary.write_text('#!/bin/bash\necho dev+oldbuild\n')
        binary.chmod(0o755)
        (self.old / 'custom-file').write_text('preserve me')
        self.service = self.base / 'units'
        self.service.mkdir()
        (self.service / 'x-ui.service').write_text('existing unit')
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                        ROUTES=str(self.base / 'routes.json'), URL_LOG=str(self.base / 'urls'),
                        XUI_EXPECTED_COMMIT='', XUI_UPDATE_TAG='')
        self.make_release()

    def route(self, url, data):
        with tempfile.NamedTemporaryFile(dir=self.base, delete=False) as fixture:
            dest = Path(fixture.name)
        dest.write_bytes(data.encode() if isinstance(data, str) else data)
        self.routes[url] = str(dest)

    def make_release(self, marker=COMMIT, missing=None, tag='dev-latest'):
        files = {'x-ui': '#!/bin/bash\necho dev+aaaaaaaa\n',
                 '.release-commit': marker, 'x-ui.sh': '#!/bin/bash\necho menu\n',
                 'x-ui.service.debian': 'new unit', 'x-ui.rc': '#!/sbin/openrc-run\n',
                 'bin/xray-linux-amd64': 'xray'}
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as tar:
            for name, content in files.items():
                if name == missing:
                    continue
                data = content.encode()
                info = tarfile.TarInfo('x-ui/' + name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        data = archive.getvalue()
        self.route(ASSET, data)
        self.route(ASSET + '.sha256', hashlib.sha256(data).hexdigest() + '  archive.tar.gz\n')
        self.release = {'tag_name': tag, 'body': 'commit=' + COMMIT,
                        'target_commitish': OTHER, 'prerelease': True,
                        'assets': [{'name': 'x-ui-linux-amd64.tar.gz' + suffix,
                                    'browser_download_url': ASSET + suffix}
                                   for suffix in ('', '.sha256')]}
        self.save_release()

    def save_release(self):
        self.route(API + '/releases/tags/dev-latest', json.dumps(self.release))

    def run_bash(self, code, script=None):
        (self.base / 'routes.json').write_text(json.dumps(self.routes))
        prelude = (ROOT / 'deploy/release-source.sh').read_text() + '\n'
        if script:
            name = 'install_x-ui' if script == 'install.sh' else 'update_x-ui'
            prelude += function(name, script) + '\n'
        prelude += '''
arch() { echo amd64; }
load_xui_env() { :; }
_fail() { echo "$*" >&2; exit 1; }
mutation() { echo mutation > "${TEST_BASE}/mutation"; exit 97; }
systemctl() { mutation; }
rc-service() { mutation; }
pkill() { mutation; }
'''
        env = dict(self.env, TEST_BASE=str(self.base))
        if script:
            env['XUI_EXPECTED_COMMIT'] = COMMIT
        prelude += f'\nxui_folder="{self.old}"\nxui_service="{self.service}"\nrelease=debian\n'
        result = subprocess.run(['bash', '-c', prelude + code], env=env,
                                text=True, capture_output=True, timeout=20)
        return result

    def assert_preserved(self):
        self.assertEqual((self.old / 'custom-file').read_text(), 'preserve me')
        self.assertEqual((self.old / 'x-ui').read_text(), '#!/bin/bash\necho dev+oldbuild\n')
        self.assertEqual((self.service / 'x-ui.service').read_text(), 'existing unit')
        self.assertFalse((self.base / 'mutation').exists(), 'service touched before preparation succeeded')

    def test_default_dev_and_stale_target_ignored(self):
        r = self.run_bash('xui_resolve_release && echo "$XUI_RELEASE_TAG $XUI_RELEASE_COMMIT"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), 'dev-latest ' + COMMIT)
        self.assertNotIn('releases/latest', (self.base / 'urls').read_text())

    def test_stable_unavailable_and_bad_version_do_not_fallback(self):
        for tag in ('stable', 'main', 'v1;touch /tmp/unwanted'):
            with self.subTest(tag=tag):
                r = self.run_bash('xui_resolve_release ' + shlex.quote(tag))
                self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('MHSanaei', (self.base / 'urls').read_text())

    def test_explicit_version_resolves_full_tag_commit(self):
        self.release['tag_name'] = 'v3.10.2'
        self.route(API + '/releases/tags/v3.10.2', json.dumps(self.release))
        self.route(API + '/commits/v3.10.2', json.dumps({'sha': COMMIT}))
        r = self.run_bash('xui_resolve_release 3.10.2 && echo "$XUI_RELEASE_COMMIT"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), COMMIT)

    def test_commit_marker_and_launcher_pin_required(self):
        for body in ('commit=deadbeef', 'commit=' + COMMIT + 'a', 'no commit'):
            self.release['body'] = body
            self.save_release()
            self.assertNotEqual(self.run_bash('xui_resolve_release dev').returncode, 0)
        self.release['body'] = 'commit=' + COMMIT
        self.save_release()
        self.env['XUI_EXPECTED_COMMIT'] = OTHER
        self.assertNotEqual(self.run_bash('xui_resolve_release dev').returncode, 0)

    def test_stage_success_and_commit_pinned_companions(self):
        self.make_release(missing='x-ui.service.debian')
        self.route(RAW + '/' + COMMIT + '/x-ui.service.debian', 'pinned unit')
        stage = self.base / 'stage'
        stage.mkdir()
        r = self.run_bash(f'xui_resolve_release && xui_prepare_release "{stage}" amd64 debian')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((stage / 'service-unit').read_text(), 'pinned unit')
        self.assertEqual((stage / 'x-ui/.release-commit').read_text(), COMMIT)
        self.assertNotIn('/main/', (self.base / 'urls').read_text())
        self.assert_preserved()

    def test_missing_or_foreign_asset_metadata_is_refused(self):
        for assets in ([], self.release['assets'][:1], [
                {'name': 'x-ui-linux-amd64.tar.gz', 'browser_download_url': ASSET.replace('LelandJin', 'MHSanaei')}]):
            with self.subTest(assets=assets):
                self.release['assets'] = assets
                self.save_release()
                stage = Path(tempfile.mkdtemp(dir=self.base))
                r = self.run_bash(f'xui_resolve_release && xui_prepare_release "{stage}" amd64 debian')
                self.assertNotEqual(r.returncode, 0)
                self.assert_preserved()

    def test_openrc_companion_is_staged_at_release_commit(self):
        self.make_release(missing='x-ui.rc')
        for available in (False, True):
            stage = Path(tempfile.mkdtemp(dir=self.base))
            if available:
                self.route(RAW + '/' + COMMIT + '/x-ui.rc', '#!/sbin/openrc-run\n')
            r = self.run_bash(f'xui_resolve_release && xui_prepare_release "{stage}" amd64 alpine')
            self.assertEqual(r.returncode == 0, available, r.stderr)
            self.assert_preserved()

    def test_archive_traversal_is_refused_before_extraction(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as tar:
            info = tarfile.TarInfo('x-ui/../../escaped')
            info.size = 1
            tar.addfile(info, io.BytesIO(b'x'))
        data = archive.getvalue()
        self.route(ASSET, data)
        self.route(ASSET + '.sha256', hashlib.sha256(data).hexdigest())
        stage = Path(tempfile.mkdtemp(dir=self.base))
        r = self.run_bash(f'xui_resolve_release && xui_prepare_release "{stage}" amd64 debian')
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.base / 'escaped').exists())
        self.assert_preserved()

    def test_install_and_update_failures_preserve_existing_installation(self):
        for script in ('install.sh', 'update.sh'):
            for problem in ('release', 'asset', 'checksum', 'bad_checksum', 'empty_checksum',
                            'marker', 'missing_marker', 'binary', 'xray', 'script', 'unit', 'corrupt_archive'):
                with self.subTest(script=script, problem=problem):
                    self.routes = {}
                    missing = {'missing_marker': '.release-commit', 'binary': 'x-ui',
                               'xray': 'bin/xray-linux-amd64', 'script': 'x-ui.sh',
                               'unit': 'x-ui.service.debian'}.get(problem)
                    self.make_release(marker=OTHER if problem == 'marker' else COMMIT, missing=missing)
                    if problem == 'release':
                        del self.routes[API + '/releases/tags/dev-latest']
                    elif problem in ('asset', 'checksum'):
                        del self.routes[ASSET + ('.sha256' if problem == 'checksum' else '')]
                    elif problem == 'bad_checksum':
                        self.route(ASSET + '.sha256', '0' * 64)
                    elif problem == 'empty_checksum':
                        self.route(ASSET + '.sha256', '')
                    elif problem == 'corrupt_archive':
                        self.route(ASSET, b'invalid gzip')
                        self.route(ASSET + '.sha256', hashlib.sha256(b'invalid gzip').hexdigest())
                    r = self.run_bash('install_x-ui' if script == 'install.sh' else 'update_x-ui', script)
                    self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assert_preserved()

    def test_cli_bootstrap_pins_and_refuses_old_updater(self):
        defs = function('run_release_script', 'x-ui.sh')
        self.route(RAW + '/' + COMMIT + '/update.sh', '#!/bin/bash\necho unsafe-old-updater\n')
        r = self.run_bash(defs + '\nrun_release_script update.sh dev')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('unsafe-old-updater', r.stdout)
        self.route(RAW + '/' + COMMIT + '/update.sh', '#!/bin/bash\n' +
                   '# FORK_RELEASE_PROTOCOL=1\nXUI_RELEASE_REPO="LelandJin/3x-ui"\n' +
                   'printf "%s %s %s" "$XUI_EXPECTED_COMMIT" "$XUI_UPDATE_TAG" "$1"\n')
        r = self.run_bash(defs + '\nrun_release_script update.sh dev')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, COMMIT + ' dev-latest dev-latest')

    def test_standalone_main_download_reexecutes_release_script(self):
        for script, name in (('install.sh', 'install_x-ui'), ('update.sh', 'update_x-ui')):
            self.route(RAW + '/' + COMMIT + '/' + script, '#!/bin/bash\n' +
                       '# FORK_RELEASE_PROTOCOL=1\nXUI_RELEASE_REPO="LelandJin/3x-ui"\n' +
                       'echo "pinned $XUI_EXPECTED_COMMIT $1"\n')
            r = self.run_bash('unset XUI_EXPECTED_COMMIT\n' + name, script)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('pinned ' + COMMIT + ' dev-latest', r.stdout)
            self.assert_preserved()

    def test_cli_install_sentinel_and_update_failure_status(self):
        defs = '\n'.join(function(name, 'x-ui.sh') for name in ('install', 'update', 'update_dev'))
        defs += '\nconfirm() { return 0; }\nstart() { :; }\nbefore_show_menu() { :; }\nLOGI() { :; }\n'
        defs += 'run_release_script() { printf "%s %s" "$1" "$2"; }\n'
        r = self.run_bash(defs + '\ninstall 0')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, 'install.sh dev-latest')
        for name in ('install', 'update', 'update_dev'):
            r = self.run_bash(defs + '\nrun_release_script() { return 23; }\n' + name + ' 0')
            self.assertNotEqual(r.returncode, 0, name)

    def test_script_refresh_uses_installed_commit_only(self):
        defs = function('installed_script_url', 'x-ui.sh')
        self.assertNotEqual(self.run_bash(defs + '\ninstalled_script_url').returncode, 0)
        (self.old / '.release-commit').write_text(OTHER)
        r = self.run_bash(defs + '\ninstalled_script_url')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), RAW + '/' + OTHER + '/x-ui.sh')

    def test_embedded_sources_and_entrypoints(self):
        subprocess.run(['python3', str(ROOT / 'deploy/sync-release-source.py'), '--check'], check=True)
        for script in ('install.sh', 'update.sh', 'x-ui.sh'):
            text = (ROOT / script).read_text()
            self.assertNotRegex(text, r'(?i)(raw.githubusercontent.com|api.github.com/repos|github.com)/mhsanaei/3x-ui')
            self.assertNotIn('bash <(curl', text)
        for name in ('install', 'update', 'update_dev', 'legacy_version'):
            self.assertIn('run_release_script', function(name, 'x-ui.sh'))
        workflow = (ROOT / '.github/workflows/release.yml').read_text()
        self.assertIn('"$GITHUB_SHA" > x-ui/.release-commit', workflow)
        self.assertIn('cp x-ui.rc x-ui/', workflow)

    def test_packaged_companions_trigger_release_and_regression_ci(self):
        workflow = (ROOT / '.github/workflows/release.yml').read_text()
        # Each root-level companion copied into the Linux archive must be able
        # to refresh dev-latest independently, including the non-.sh OpenRC unit.
        companions = set(re.findall(
            r'^\s+cp (x-ui\.(?:sh|rc|service(?:\.[\w-]+)?)) x-ui/\s*$', workflow, re.M))
        self.assertIn('x-ui.rc', companions)
        self.assertTrue(any(name.startswith('x-ui.service') for name in companions))

        def path_filters(text):
            # Restrict to event filters, excluding job steps. These workflows
            # deliberately use simple quoted lists; no YAML dependency is needed.
            events = text.split('\njobs:', 1)[0]
            return [re.findall(r'^      - "([^"]+)"$', block, re.M)
                    for block in re.findall(r'^    paths:\n((?:      - [^\n]+\n)+)', events, re.M)]

        release_filters = path_filters(workflow)
        self.assertEqual(len(release_filters), 1, 'expected the main push path filter')
        for name in sorted(companions):
            self.assertTrue(any(fnmatch.fnmatchcase(name, pattern) for pattern in release_filters[0]),
                            f'{name} is packaged but cannot trigger a release on its own')

        # Changing packaged inputs or the release filter must also run this
        # regression contract on both pull requests and main pushes.
        ci_filters = path_filters((ROOT / '.github/workflows/ci.yml').read_text())
        self.assertEqual(len(ci_filters), 2, 'expected PR and main push path filters')
        for patterns in ci_filters:
            for name in sorted(companions | {'.github/workflows/release.yml'}):
                self.assertTrue(any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns),
                                f'{name} cannot trigger the companion regression CI')


if __name__ == '__main__':
    unittest.main(verbosity=2)
