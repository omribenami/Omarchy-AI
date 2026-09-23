import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from omarchy_ai.core import updates


def bundle(directory, version='0.4.0', extra=None):
    archive = directory / f'omarchy-ai-{version}-linux-x86_64.tar.gz'
    root = f'omarchy-ai-{version}-linux-x86_64'
    files = {'pyproject.toml': f'[project]\nversion = "{version}"\n',
             'uv.lock': '', 'scripts/setup.sh': '', 'systemd/omarchy-ai.service': ''}
    with tarfile.open(archive, 'w:gz') as tar:
        for name, text in files.items():
            data = text.encode()
            member = tarfile.TarInfo(root + '/' + name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
        if extra:
            tar.addfile(extra)
    checksum = archive.with_name(archive.name + '.sha256')
    checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name)
    return archive, checksum


CHANGELOG = """# Changelog

## [Unreleased]

### Highlights

- Not announced yet.

## [0.4.0] - 2026-10-01

### Highlights

- Scheduled tasks keep running between
  conversations.
- Skills.

### Fixes

- A fix.

## [0.3.9] - 2026-09-22

### Highlights

- Older.
"""


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.patch = patch.object(updates, 'STATE', self.base / 'state')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        package = 'omarchy-ai-0.4.0-linux-x86_64.tar.gz'
        self.release = {
            'latest_version': '0.4.0', 'commit': None, 'tag': 'v0.4.0', 'package': package,
            'package_url': f'https://github.com/omribenami/Omarchy-AI/releases/download/v0.4.0/{package}',
            'checksum_url': f'https://github.com/omribenami/Omarchy-AI/releases/download/v0.4.0/{package}.sha256',
            'source': 'release',
        }
        # Never fetch a real changelog from GitHub in tests.
        self.changelog = patch.object(updates, '_fetch_changelog', return_value=CHANGELOG)
        self.fetch = self.changelog.start()
        self.addCleanup(self.changelog.stop)

    def _github_release(self, version, *, draft=False, prerelease=False, assets=None, tag=None):
        package = f'omarchy-ai-{version}-linux-x86_64.tar.gz'
        if assets is None:
            assets = [{'name': package}, {'name': package + '.sha256'}]
        return {'tag_name': tag if tag is not None else f'v{version}', 'draft': draft,
                'prerelease': prerelease, 'assets': assets}

    def test_numeric_order_and_ignore_unpaired_or_non_versioned_files(self):
        names = ['omarchy-ai-0.9.0-linux-x86_64.tar.gz', 'omarchy-ai-0.10.0-linux-x86_64.tar.gz',
                 'omarchy-ai-9.0.0-linux-x86_64.tar.gz', 'demo-media', 'omarchy-ai-1.0.0-rc1-linux-x86_64.tar.gz']
        entries = [{'name': n, 'type': 'file'} for n in names + [n + '.sha256' for n in names[:2]]]
        releases = [self._github_release('0.1.0', tag='demo-media', assets=[{'name': 'clip.mp4'}])]
        with patch.object(updates, '_list_releases', return_value=releases), patch.object(
                updates, '_json', side_effect=[{'sha': 'a' * 40}, entries]) as get:
            result = updates.discover()
        self.assertEqual(result['latest_version'], '0.10.0')
        self.assertEqual(result['source'], 'dist')
        self.assertIn('ref=' + 'a' * 40, get.call_args.args[0])

    def test_release_assets_ignore_demo_drafts_prereleases_and_mismatched_names(self):
        releases = [
            self._github_release('0.1.0', tag='demo-media', assets=[{'name': 'clip.mp4'}]),
            self._github_release('0.9.0', draft=True),
            self._github_release('9.0.0', prerelease=True),
            self._github_release('0.8.0', assets=[
                {'name': 'omarchy-ai-0.8.0-linux-x86_64.tar.gz'}]),
            self._github_release('0.7.0', assets=[
                {'name': 'omarchy-ai-0.4.0-linux-x86_64.tar.gz'},
                {'name': 'omarchy-ai-0.4.0-linux-x86_64.tar.gz.sha256'}]),
            self._github_release('0.10.0'),
            self._github_release('0.9.0'),
        ]
        found = updates.candidates_from_releases(releases)
        versions = sorted(item['latest_version'] for item in found)
        self.assertEqual(versions, ['0.10.0', '0.9.0'])
        chosen = [item for item in found if item['latest_version'] == '0.10.0'][0]
        self.assertEqual(
            chosen['package_url'],
            'https://github.com/omribenami/Omarchy-AI/releases/download/v0.10.0/omarchy-ai-0.10.0-linux-x86_64.tar.gz')
        self.assertNotIn('raw.githubusercontent.com', chosen['package_url'])

    def test_equal_version_prefers_release_asset_over_dist(self):
        dist = updates.candidates_from_dist('a' * 40, [
            {'name': 'omarchy-ai-0.4.0-linux-x86_64.tar.gz', 'type': 'file'},
            {'name': 'omarchy-ai-0.4.0-linux-x86_64.tar.gz.sha256', 'type': 'file'},
        ])
        with patch.object(updates, '_list_releases', return_value=[self._github_release('0.4.0')]), \
                patch.object(updates, '_dist_candidates', return_value=dist):
            result = updates.discover()
        self.assertEqual(result['source'], 'release')
        self.assertTrue(result['package_url'].startswith(
            'https://github.com/omribenami/Omarchy-AI/releases/download/v0.4.0/'))

    def test_newer_dist_bundle_still_wins_until_a_release_catches_up(self):
        dist = updates.candidates_from_dist('b' * 40, [
            {'name': 'omarchy-ai-0.5.0-linux-x86_64.tar.gz', 'type': 'file'},
            {'name': 'omarchy-ai-0.5.0-linux-x86_64.tar.gz.sha256', 'type': 'file'},
        ])
        with patch.object(updates, '_list_releases', return_value=[self._github_release('0.4.0')]), \
                patch.object(updates, '_dist_candidates', return_value=dist):
            result = updates.discover()
        self.assertEqual(result['latest_version'], '0.5.0')
        self.assertEqual(result['source'], 'dist')
        self.assertIn('/' + 'b' * 40 + '/dist/', result['package_url'])

    def test_release_listing_failure_does_not_use_dist(self):
        with patch.object(updates, '_list_releases', side_effect=OSError('rate limit')), \
                patch.object(updates, '_dist_candidates') as dist:
            with self.assertRaises(OSError):
                updates.discover()
        dist.assert_not_called()

    def test_release_pagination_follows_github_api_only(self):
        first = updates.API + '/releases?per_page=100'
        second = updates.API + '/releases?per_page=100&page=2'
        pages = {
            first: ([], f'<{second}>; rel="next"'),
            second: ([self._github_release('0.4.0')], ''),
        }
        with patch.object(updates, '_read_github', side_effect=lambda url, limit=2_000_000: pages[url]):
            found = updates.candidates_from_releases(updates._list_releases())
        self.assertEqual(found[0]['latest_version'], '0.4.0')
        with self.assertRaisesRegex(ValueError, 'pagination'):
            updates._next_link('<https://evil.example/releases?page=2>; rel="next"')

    def test_release_page_cap(self):
        link = '<https://api.github.com/repos/omribenami/Omarchy-AI/releases?per_page=100&page=2>; rel="next"'
        with patch.object(updates, '_read_github', return_value=([], link)):
            with self.assertRaisesRegex(ValueError, 'page limit'):
                updates._list_releases()

    def test_bundle_url_allows_release_and_pinned_dist_only(self):
        name = 'omarchy-ai-0.4.0-linux-x86_64.tar.gz'
        checksum = name + '.sha256'
        release = f'https://github.com/omribenami/Omarchy-AI/releases/download/v0.4.0/{name}'
        dist = f'https://raw.githubusercontent.com/omribenami/Omarchy-AI/{"a" * 40}/dist/{name}'
        self.assertTrue(updates._bundle_url_ok(release, name))
        self.assertTrue(updates._bundle_url_ok(release + '.sha256', checksum))
        self.assertTrue(updates._bundle_url_ok(dist, name))
        self.assertFalse(updates._bundle_url_ok('https://example.com/' + name, name))
        self.assertFalse(updates._bundle_url_ok(
            'https://github.com/omribenami/Omarchy-AI/releases/download/v0.4.0/omarchy-ai-0.9.0-linux-x86_64.tar.gz',
            'omarchy-ai-0.9.0-linux-x86_64.tar.gz'))
        self.assertFalse(updates._bundle_url_ok(
            f'https://raw.githubusercontent.com/omribenami/Omarchy-AI/{"a" * 39}/dist/{name}', name))

    def test_check_caches_and_announces_only_newer_version(self):
        with patch.object(updates, 'installed_version', return_value='0.3.0'), patch.object(updates, 'discover', return_value=self.release) as discover:
            self.assertTrue(updates.check_updates()['available'])
            self.assertTrue(updates.check_updates()['available'])
            self.assertIn('0.4.0', updates.wake_notice())
            discover.assert_called_once()
        with patch.object(updates, 'installed_version', return_value='0.4.0'):
            self.assertEqual(updates.wake_notice(), '')
            self.assertFalse(updates.check_updates()['available'])

    def test_network_error_does_not_announce_stale_update(self):
        with patch.object(updates, 'installed_version', return_value='0.3.0'), patch.object(updates, 'discover', side_effect=OSError('offline')):
            self.assertFalse(updates.check_updates()['ok'])
            self.assertEqual(updates.wake_notice(), '')

    def test_old_cache_is_not_announced(self):
        updates._write(updates.STATE / 'check.json', {'ok': True, 'latest_version': '0.4.0', 'checked_at': time.time() - 100000})
        self.assertEqual(updates.wake_notice(), '')

    def test_invalid_version_rejected(self):
        for value in ['demo-media', 'v0.4.0', '0.4.0; rm', '../0.4.0', '0.4.0-beta']:
            with self.assertRaises(ValueError):
                updates.version_tuple(value)

    def test_current_install_does_not_launch_worker(self):
        with patch.object(updates, 'check_updates', return_value={'ok': True, 'available': False, 'current_version': '0.3.0', 'latest_version': '0.3.0'}), patch.object(updates.subprocess, 'run') as run:
            self.assertTrue(updates.request_update()[0])
            run.assert_not_called()

    def test_worker_is_separate_systemd_unit_and_result_is_not_completion(self):
        with patch.object(updates, 'check_updates', return_value={**self.release, 'ok': True, 'available': True}), patch.object(updates.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            ok, message = updates.request_update()
        self.assertTrue(ok)
        self.assertIn('NOT complete', message)
        self.assertEqual(run.call_args.args[0][0], 'systemd-run')
        self.assertIn('--unit=omarchy-ai-update', run.call_args.args[0])
        self.assertIn('--install', run.call_args.args[0])

    def test_duplicate_worker_failure_is_not_success(self):
        with patch.object(updates, 'check_updates', return_value={**self.release, 'ok': True, 'available': True}), patch.object(updates.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'Unit already exists')):
            self.assertFalse(updates.request_update()[0])

    def test_checksum_verified_and_version_matched(self):
        archive, checksum = bundle(self.base)
        root = updates.unpack_verified(archive, checksum, self.base / 'unpacked', '0.4.0')
        self.assertEqual(updates.installed_version(root), '0.4.0')

    def test_corrupt_download_not_extracted(self):
        archive, checksum = bundle(self.base)
        archive.write_bytes(archive.read_bytes() + b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            updates.unpack_verified(archive, checksum, self.base / 'unpacked', '0.4.0')
        self.assertFalse((self.base / 'unpacked').exists())

    def test_traversal_and_symlink_rejected(self):
        for name, link in [('../escape', False), ('omarchy-ai-0.4.0-linux-x86_64/link', True)]:
            member = tarfile.TarInfo(name)
            if link:
                member.type = tarfile.SYMTYPE
                member.linkname = '/etc'
            archive, checksum = bundle(self.base, extra=member)
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                updates.unpack_verified(archive, checksum, self.base / 'unpacked', '0.4.0')

    def test_new_version_notice_added_to_shared_instructions(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.live import build_session_config
        with patch.object(updates, 'wake_notice', return_value='Omarchy AI version 0.4.0 is available.'):
            config = build_session_config(Config())
        self.assertIn('VERIFIED STARTUP UPDATE NOTICE', config['instructions'])
        tools = [t['name'] for t in config['delegation']['responses']['tools']]
        for name in ('check_assistant_updates', 'update_assistant', 'get_update_status'):
            self.assertIn(name, tools)

    def test_changelog_parsing_joins_wrapped_bullets_and_skips_unreleased(self):
        releases = updates.parse_changelog(CHANGELOG)
        self.assertEqual(releases['0.4.0']['sections']['Highlights'][0],
                         'Scheduled tasks keep running between conversations.')
        self.assertEqual([e['version'] for e in updates.notes_between(releases, '0.3.8', '0.4.0')], ['0.4.0', '0.3.9'])
        self.assertEqual(updates.notes_between(releases, '0.4.0', '0.4.0'), [])

    def test_available_update_caches_its_highlights_from_the_pinned_commit(self):
        with patch.object(updates, 'installed_version', return_value='0.3.9'), patch.object(updates, 'discover', return_value=self.release):
            result = updates.check_updates()
            self.fetch.assert_called_once_with('v0.4.0')  # the tag a Release asset is built from
            self.assertEqual(result['release_notes'][0]['version'], '0.4.0')
            notice = updates.wake_notice()
        self.assertIn("what's new", notice)

    def test_no_offer_to_explain_when_the_changelog_cannot_be_read(self):
        self.fetch.side_effect = OSError('offline')
        with patch.object(updates, 'installed_version', return_value='0.3.9'), patch.object(updates, 'discover', return_value=self.release):
            self.assertTrue(updates.check_updates()['available'])
            notice = updates.wake_notice()
            notes = updates.release_notes()
        self.assertIn('0.4.0', notice)
        self.assertNotIn("what's new", notice)
        self.assertFalse(notes['ok'])

    def test_release_notes_for_update_and_for_installed_version(self):
        with patch.object(updates, 'installed_version', return_value='0.3.9'), patch.object(updates, 'discover', return_value=self.release):
            notes = updates.release_notes()
        self.assertTrue(notes['update_available'])
        self.assertEqual(notes['releases'][0]['sections']['Fixes'], ['A fix.'])
        with patch.object(updates, 'installed_version', return_value='0.4.0'), patch.object(updates, 'discover', return_value=self.release), \
                patch.object(updates, 'local_changelog', return_value=updates.parse_changelog(CHANGELOG)):
            notes = updates.release_notes()
        self.assertFalse(notes['update_available'])
        self.assertEqual(notes['releases'][0]['version'], '0.4.0')

    def test_first_wakes_after_a_completed_update_offer_its_highlights_then_stop(self):
        updates._write(updates.STATE / 'install.json', {'state': 'completed', 'version': '0.4.0'})
        with patch.object(updates, 'installed_version', return_value='0.4.0'), \
                patch.object(updates, 'local_changelog', return_value=updates.parse_changelog(CHANGELOG)):
            first = updates.wake_notice()
            same_session = updates.wake_notice()
            with patch.object(updates.time, 'time', return_value=time.time() + 3600):
                later = updates.wake_notice()
        self.assertIn('just updated to Omarchy AI version 0.4.0', first)
        self.assertIn("what's new", first)
        self.assertEqual(first, same_session)
        self.assertEqual(later, '')

    def test_repo_changelog_has_highlights_for_the_packaged_version(self):
        root = Path(updates.__file__).resolve().parents[3]
        version = updates.installed_version(root)
        entry = updates.parse_changelog((root / 'CHANGELOG.md').read_text())[version]
        self.assertTrue(entry['sections'].get('Highlights'))

    def test_health_check_rejects_restart_loop(self):
        replies = [subprocess.CompletedProcess([], 0, f'ActiveState=active\nMainPID={pid}\n') for pid in (1, 2)]
        with patch.object(updates.subprocess, 'run', side_effect=replies), patch.object(updates.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'restarted'):
                updates._healthy()

    def test_install_failure_restores_old_installation(self):
        old = self.base / 'old'
        old.mkdir()
        (old / 'pyproject.toml').write_text('[project]\nversion="0.3.0"')
        new = self.base / 'new'
        new.mkdir()
        commands = []
        def command(args, **kwargs):
            commands.append(args)
            if args == ['bash', 'scripts/setup.sh']:
                raise RuntimeError('plugin installation failed')
        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=self.release), patch.object(updates, '_download'), patch.object(updates, 'unpack_verified', return_value=new), patch.object(updates, '_command', side_effect=command), patch.object(updates, '_snapshot', return_value=['backup']), patch.object(updates, '_restore') as restore, patch.object(updates, '_healthy'), patch.object(updates.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'plugin installation failed'):
                updates.install('0.4.0', old)
        restore.assert_called_once_with(['backup'])
        self.assertIn(['systemctl', '--user', 'start', updates.SERVICE], commands)
        self.assertEqual(updates.update_status()['state'], 'failed')
        self.assertIn('restored and running', updates.update_status()['message'])
        self.assertTrue((old / 'pyproject.toml').exists())

    def test_install_downloads_release_asset_urls(self):
        old = self.base / 'old'
        old.mkdir()
        (old / 'pyproject.toml').write_text('[project]\nversion="0.3.0"')
        new = self.base / 'new'
        new.mkdir()
        urls = []

        def download(url, path, limit):
            urls.append(url)
            path.write_bytes(b'x')

        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=self.release), patch.object(updates, '_download', side_effect=download), patch.object(updates, 'unpack_verified', return_value=new), patch.object(updates, '_command'), patch.object(updates, '_snapshot', return_value=[]), patch.object(updates, '_healthy'), patch.object(updates.time, 'sleep'):
            updates.install('0.4.0', old)
        self.assertEqual(urls, [self.release['package_url'], self.release['checksum_url']])

    def test_install_refuses_unexpected_download_url(self):
        old = self.base / 'old'
        old.mkdir()
        (old / 'pyproject.toml').write_text('[project]\nversion="0.3.0"')
        bad = {**self.release, 'package_url': 'https://example.com/omarchy-ai-0.4.0-linux-x86_64.tar.gz'}
        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=bad), patch.object(updates, '_download') as download, patch.object(updates, '_command'):
            with self.assertRaisesRegex(ValueError, 'unexpected update URL'):
                updates.install('0.4.0', old)
        download.assert_not_called()

    def test_download_failure_never_stops_assistant(self):
        old = self.base / 'old'
        old.mkdir()
        (old / 'pyproject.toml').write_text('[project]\nversion="0.3.0"')
        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=self.release), patch.object(updates, '_download', side_effect=OSError('offline')), patch.object(updates, '_command') as command:
            with self.assertRaises(OSError):
                updates.install('0.4.0', old)
        command.assert_not_called()
        self.assertEqual(updates.update_status()['state'], 'failed')

    def test_failure_files_a_github_issue_when_a_token_is_configured(self):
        # Real report from another machine (see the 0.3.8 uv.lock incident):
        # an update failure used to just sit in install.json with nothing
        # actually reported anywhere. A failed install must now attempt to
        # file a GitHub issue and record the real outcome (URL, or why not)
        # so get_update_status can be honest instead of the live model
        # guessing or promising something that never happened.
        old = self.base / 'old'
        old.mkdir()
        (old / 'pyproject.toml').write_text('[project]\nversion="0.3.0"')
        from omarchy_ai.core import issues
        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=self.release), patch.object(updates, '_download', side_effect=OSError('offline')), patch.object(updates, '_command'), \
                patch.object(issues, 'file_issue', return_value=(True, 'https://github.com/omribenami/Omarchy-AI/issues/42')) as file_issue:
            with self.assertRaises(OSError):
                updates.install('0.4.0', old)
        file_issue.assert_called_once()
        title = file_issue.call_args.args[0]
        self.assertIn('0.4.0', title)
        status = updates.update_status()
        self.assertEqual(status['issue_url'], 'https://github.com/omribenami/Omarchy-AI/issues/42')
        self.assertIsNone(status['issue_error'])

    def test_failure_reports_no_issue_filed_without_a_token(self):
        old = self.base / 'old'
        old.mkdir()
        (old / 'pyproject.toml').write_text('[project]\nversion="0.3.0"')
        from omarchy_ai.core import issues
        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=self.release), patch.object(updates, '_download', side_effect=OSError('offline')), patch.object(updates, '_command'), \
                patch.object(issues, 'file_issue', return_value=(False, 'no GitHub issue token configured on this machine')):
            with self.assertRaises(OSError):
                updates.install('0.4.0', old)
        status = updates.update_status()
        self.assertIsNone(status['issue_url'])
        self.assertIn('no GitHub issue token', status['issue_error'])

    def test_issue_filing_failure_never_masks_the_real_update_error(self):
        old = self.base / 'old'
        old.mkdir()
        (old / 'pyproject.toml').write_text('[project]\nversion="0.3.0"')
        from omarchy_ai.core import issues
        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=self.release), patch.object(updates, '_download', side_effect=OSError('offline')), patch.object(updates, '_command'), \
                patch.object(issues, 'file_issue', side_effect=RuntimeError('boom')):
            with self.assertRaises(OSError):
                updates.install('0.4.0', old)
        self.assertEqual(updates.update_status()['state'], 'failed')


    def test_successful_install_keeps_old_tree_and_marks_completed(self):
        old = self.base / 'old'
        old.mkdir()
        (old / 'pyproject.toml').write_text('[project]\nversion="0.3.0"')
        new = self.base / 'new'
        new.mkdir()
        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=self.release), patch.object(updates, '_download'), patch.object(updates, 'unpack_verified', return_value=new), patch.object(updates, '_command') as command, patch.object(updates, '_snapshot', return_value=[]), patch.object(updates, '_healthy') as healthy, patch.object(updates.time, 'sleep'):
            updates.install('0.4.0', old)
        self.assertEqual(updates.update_status()['state'], 'completed')
        self.assertTrue((old / 'pyproject.toml').exists())
        healthy.assert_called_once()
        calls = [call.args[0] for call in command.call_args_list]
        self.assertLess(calls.index(['uv', 'sync', '--locked']), calls.index(['systemctl', '--user', 'stop', updates.SERVICE]))

    def test_malformed_cache_is_refreshed(self):
        updates._write(updates.STATE / 'check.json', {'ok': True, 'checked_at': time.time(), 'latest_version': 'demo-media'})
        with patch.object(updates, 'installed_version', return_value='0.3.0'), patch.object(updates, 'discover', return_value=self.release):
            self.assertTrue(updates.check_updates()['available'])

    def test_snapshot_restore_preserves_settings_and_rc_files(self):
        config = self.base / 'config'
        home = self.base / 'home'
        home.mkdir()
        unit = config / 'systemd/user/omarchy-ai.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('old service')
        shell = config / 'omarchy'
        shell.mkdir()
        (shell / 'shell.json').write_text('old layout')
        (home / '.bashrc').write_text('user customizations')
        backup = self.base / 'backup'
        backup.mkdir()
        with patch.object(updates, 'CONFIG', config), patch.object(Path, 'home', return_value=home):
            records = updates._snapshot(backup)
        unit.write_text('new service')
        (shell / 'shell.json').write_text('new layout')
        (home / '.zshrc').write_text('new hook')
        updates._restore(records)
        self.assertEqual(unit.read_text(), 'old service')
        self.assertEqual((shell / 'shell.json').read_text(), 'old layout')
        self.assertEqual((home / '.bashrc').read_text(), 'user customizations')
        self.assertFalse((home / '.zshrc').exists())


class UpdateAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_wake_requests_notice_without_installing(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from omarchy_ai.voice.gemini_live import announce_update
        session = SimpleNamespace(send_client_content=AsyncMock())
        with patch.object(updates, 'wake_notice', return_value='Version 0.4.0 is available'), patch.object(updates, 'request_update') as install:
            await announce_update(session)
        session.send_client_content.assert_awaited_once()
        self.assertIn('0.4.0', str(session.send_client_content.call_args))
        install.assert_not_called()

    async def test_no_gemini_startup_speech_when_current(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from omarchy_ai.voice.gemini_live import announce_update
        session = SimpleNamespace(send_client_content=AsyncMock())
        with patch.object(updates, 'wake_notice', return_value=''):
            await announce_update(session)
        session.send_client_content.assert_not_awaited()
