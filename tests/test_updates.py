import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
import urllib.error
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


class _UpdateIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.patch = patch.object(updates, 'STATE', self.base / 'state/omarchy-ai/updates')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # Never touch the real ~/.local/share/omarchy-ai, the real service,
        # or this repository's own checkout state (tests run from one).
        share = self.base / 'share'
        isolation = {
            'DATA': share, 'RELEASES': share / 'releases', 'VERSIONS': share / 'versions',
            'CURRENT': share / 'current', 'WATCH_SECONDS': 0,
        }
        for name, value in isolation.items():
            started = patch.object(updates, name, value)
            started.start()
            self.addCleanup(started.stop)
        for name, kwargs in {
            '_unit_state': {'return_value': 'active'},
            '_trusted_keys': {'return_value': []},
            'is_dev_checkout': {'side_effect': lambda root=None: root is not None and (Path(root) / '.git').exists()},
        }.items():
            started = patch.object(updates, name, **kwargs)
            started.start()
            self.addCleanup(started.stop)
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


class UpdateTests(_UpdateIsolation):
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

    def test_untracked_dist_folder_leaves_only_release_assets(self):
        # main no longer tracks dist/, so GitHub answers its listing with 404.
        def fake_json(url):
            if url == updates.API + '/commits/main':
                return {'sha': 'c' * 40}
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        with patch.object(updates, '_json', side_effect=fake_json):
            self.assertEqual(updates._dist_candidates(), [])
        with patch.object(updates, '_list_releases', return_value=[self._github_release('0.15.0')]), \
                patch.object(updates, '_json', side_effect=fake_json):
            result = updates.discover()
        self.assertEqual((result['latest_version'], result['source']), ('0.15.0', 'release'))

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
            if args == ['bash', str(updates.CURRENT / 'scripts/setup.sh')]:
                raise RuntimeError('plugin installation failed')
        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=self.release), patch.object(updates, '_download'), patch.object(updates, 'unpack_verified', return_value=new), patch.object(updates, 'verify_release', return_value='SHA-256 verified'), patch.object(updates, '_command', side_effect=command), patch.object(updates, '_snapshot', return_value=['backup']), patch.object(updates, '_restore') as restore, patch.object(updates, '_healthy'), patch.object(updates.time, 'sleep'):
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

        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=self.release), patch.object(updates, '_download', side_effect=download), patch.object(updates, 'unpack_verified', return_value=new), patch.object(updates, 'verify_release', return_value='SHA-256 verified'), patch.object(updates, '_command'), patch.object(updates, '_snapshot', return_value=[]), patch.object(updates, '_healthy'), patch.object(updates.time, 'sleep'):
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
        with patch.object(updates, 'RELEASES', self.base / 'releases'), patch.object(updates, 'discover', return_value=self.release), patch.object(updates, '_download'), patch.object(updates, 'unpack_verified', return_value=new), patch.object(updates, 'verify_release', return_value='SHA-256 verified'), patch.object(updates, '_command') as command, patch.object(updates, '_snapshot', return_value=[]), patch.object(updates, '_healthy') as healthy, patch.object(updates.time, 'sleep'):
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


class SideBySideUpdateTests(_UpdateIsolation):
    """Release 3.6: versions/<v> trees, the `current` switch, the
    post-update doctor check and automatic rollback."""

    def _old_tree(self, path, version='0.3.0'):
        path.mkdir(parents=True)
        (path / 'pyproject.toml').write_text(f'[project]\nversion="{version}"')
        return path

    def _new_tree(self):
        new = self.base / 'unpacked/omarchy-ai-0.4.0-linux-x86_64'
        new.mkdir(parents=True)
        (new / 'pyproject.toml').write_text('[project]\nversion="0.4.0"')
        return new

    def _install(self, previous, *, doctor=None, watch=None, commands=None, release=None, verify='SHA-256 verified'):
        commands = [] if commands is None else commands
        def command(args, **kwargs):
            commands.append(args)
        doctor = doctor or (lambda root: None)
        new = self._new_tree()
        with patch.object(updates, 'discover', return_value=release or self.release), patch.object(updates, '_download'), \
                patch.object(updates, 'verify_release', return_value=verify), patch.object(updates, 'unpack_verified', return_value=new), \
                patch.object(updates, '_command', side_effect=command), patch.object(updates, '_snapshot', return_value=['backup']), \
                patch.object(updates, '_restore') as restore, patch.object(updates, '_healthy'), patch.object(updates.time, 'sleep'), \
                patch.object(updates, '_doctor_quick', side_effect=doctor), \
                patch.object(updates, '_watch', side_effect=watch), \
                patch.object(updates, '_report_failure', return_value=(None, 'no token')):
            try:
                updates.install('0.4.0', previous)
            finally:
                self.restore = restore
        return commands

    def test_installs_side_by_side_and_switches_current(self):
        old = self._old_tree(updates.VERSIONS / '0.3.0')
        updates.CURRENT.parent.mkdir(parents=True, exist_ok=True)
        updates.CURRENT.symlink_to(old)
        commands = self._install(old)
        target = updates.VERSIONS / '0.4.0'
        self.assertTrue(updates.CURRENT.is_symlink())
        self.assertEqual(updates.CURRENT.resolve(), target.resolve())
        self.assertEqual(updates.installed_version(target), '0.4.0')
        self.assertTrue((old / 'pyproject.toml').exists())
        stop = commands.index(['systemctl', '--user', 'stop', updates.SERVICE])
        setup = commands.index(['bash', str(updates.CURRENT / 'scripts/setup.sh')])
        start = commands.index(['systemctl', '--user', 'start', updates.SERVICE])
        self.assertLess(commands.index(['uv', 'sync', '--locked']), stop)
        self.assertLess(stop, setup)
        self.assertLess(setup, start)
        status = updates.update_status()
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(status['verification'], 'SHA-256 verified')
        self.assertEqual(sorted(p.name for p in updates.VERSIONS.iterdir()), ['0.3.0', '0.4.0'])
        self.assertFalse(any(p.name.startswith('.staging-') for p in updates.VERSIONS.iterdir()))

    def test_keeps_only_the_last_two_versions(self):
        self._old_tree(updates.VERSIONS / '0.2.0', '0.2.0')
        old = self._old_tree(updates.VERSIONS / '0.3.0')
        updates.CURRENT.symlink_to(old)
        legacy = self._old_tree(updates.RELEASES / '0.1.0-abc/omarchy-ai-0.1.0-linux-x86_64', '0.1.0')
        self._install(old)
        self.assertEqual(sorted(p.name for p in updates.VERSIONS.iterdir()), ['0.3.0', '0.4.0'])
        self.assertTrue(legacy.exists())  # outside versions/: never pruned

    def test_legacy_install_migrates_once_and_rolls_back_to_its_tree(self):
        legacy = self._old_tree(self.base / 'extracted/omarchy-ai-0.3.0-linux-x86_64')
        self._install(legacy)
        self.assertEqual(updates.CURRENT.resolve(), (updates.VERSIONS / '0.4.0').resolve())
        self.assertTrue(legacy.exists())
        # A second, failing update from the migrated install goes back to it.
        updates.CURRENT.unlink()
        legacy2 = self._old_tree(self.base / 'extracted2/omarchy-ai-0.3.0-linux-x86_64')
        with self.assertRaises(RuntimeError):
            self._install(legacy2, watch=RuntimeError('entered crash-loop safe mode'))
        self.assertEqual(updates.CURRENT.resolve(), legacy2.resolve())
        self.restore.assert_called_once_with(['backup'])

    def test_failed_post_update_doctor_rolls_back(self):
        old = self._old_tree(updates.VERSIONS / '0.3.0')
        updates.CURRENT.symlink_to(old)
        reports = iter([
            {'systemd': {'status': 'green', 'reason': 'active'}, 'api_keys': {'status': 'red', 'reason': 'no key'}},
            {'systemd': {'status': 'red', 'reason': 'not running'}, 'api_keys': {'status': 'red', 'reason': 'no key'}},
        ])
        commands = []
        with self.assertRaisesRegex(RuntimeError, 'Post-update check failed: systemd'):
            self._install(old, doctor=lambda root: next(reports), commands=commands)
        self.assertEqual(updates.CURRENT.resolve(), old.resolve())
        self.restore.assert_called_once_with(['backup'])
        self.assertFalse((updates.VERSIONS / '0.4.0').exists())
        self.assertEqual(commands[-1][:3], ['omarchy-shell', 'shell', 'rescanPlugins'])
        self.assertIn(['systemctl', '--user', 'start', updates.SERVICE], commands[commands.index(['systemctl', '--user', 'daemon-reload']):])
        status = updates.update_status()
        self.assertEqual(status['state'], 'failed')
        self.assertIn('rolled back', status['message'])
        self.assertNotIn('api_keys', status['message'])  # already red before: not the update's doing

    def test_reds_that_were_already_there_do_not_roll_back(self):
        old = self._old_tree(updates.VERSIONS / '0.3.0')
        updates.CURRENT.symlink_to(old)
        report = {'api_keys': {'status': 'red', 'reason': 'no key'}, 'systemd': {'status': 'green', 'reason': ''}}
        self._install(old, doctor=lambda root: report)
        self.assertEqual(updates.update_status()['state'], 'completed')
        self.assertEqual(updates.CURRENT.resolve(), (updates.VERSIONS / '0.4.0').resolve())

    def test_doctor_that_cannot_run_before_the_switch_stops_the_update_untouched(self):
        old = self._old_tree(updates.VERSIONS / '0.3.0')
        updates.CURRENT.symlink_to(old)
        def broken(root):
            raise RuntimeError('omarchy-ai-doctor --quick did not produce a report')
        commands = []
        with self.assertRaisesRegex(RuntimeError, 'did not produce a report'):
            self._install(old, doctor=broken, commands=commands)
        self.assertNotIn(['systemctl', '--user', 'stop', updates.SERVICE], commands)
        self.assertEqual(updates.CURRENT.resolve(), old.resolve())

    def test_safe_mode_within_the_watch_rolls_back(self):
        old = self._old_tree(updates.VERSIONS / '0.3.0')
        updates.CURRENT.symlink_to(old)
        with self.assertRaisesRegex(RuntimeError, 'safe mode'):
            self._install(old, watch=RuntimeError('The updated assistant entered crash-loop safe mode 4 min after the update'))
        self.assertEqual(updates.CURRENT.resolve(), old.resolve())
        self.assertIn('rolled back', updates.update_status()['message'])

    def _health(self, data):
        path = updates._health_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version': 1, **data}))

    def test_watch_raises_on_safe_mode_after_the_switch(self):
        switched_at = 1000.0
        clock = iter([1000.0 + 60 * minute for minute in range(20)])
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 4:  # 4 min after the switch the new version starts crash-looping
                self._health({'safe_mode': {'since': switched_at + 240, 'crashes': 3}, 'crashes': [1, 2, 3]})
        self._health({})
        with self.assertRaisesRegex(RuntimeError, 'safe mode 4 min after'):
            updates._watch(switched_at, seconds=600, interval=60, clock=lambda: next(clock), sleep=sleep)

    def test_watch_ignores_older_safe_mode_and_ends_after_ten_minutes(self):
        switched_at = 1000.0
        self._health({'safe_mode': {'since': switched_at - 30, 'crashes': 3}})
        now = [switched_at]
        def sleep(seconds):
            now[0] += seconds
        updates._watch(switched_at, seconds=600, interval=60, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(now[0], switched_at + 600)

    def test_watch_raises_when_the_unit_fails(self):
        self._health({})
        with patch.object(updates, '_unit_state', return_value='failed'):
            with self.assertRaisesRegex(RuntimeError, 'service failed'):
                updates._watch(1000.0, seconds=600, interval=60, clock=lambda: 1000.0, sleep=lambda s: None)

    def test_doctor_quick_reads_the_trees_own_json_report(self):
        root = self.base / 'tree'
        self.assertIsNone(updates._doctor_quick(root))  # a tree from before the doctor existed
        doctor = root / '.venv/bin/omarchy-ai-doctor'
        doctor.parent.mkdir(parents=True)
        report = {'checks': [{'id': 'systemd', 'status': 'red', 'reason': 'not running'}]}
        doctor.write_text('#!/bin/sh\n[ "$1 $2" = "--quick --json" ] || exit 9\n'
                          f"cat <<'EOF'\n{json.dumps(report)}\nEOF\nexit 1\n")
        doctor.chmod(0o755)
        self.assertEqual(updates._doctor_quick(root), {'systemd': {'status': 'red', 'reason': 'not running'}})
        doctor.write_text('#!/bin/sh\necho Traceback\nexit 1\n')
        with self.assertRaisesRegex(RuntimeError, 'did not produce a report'):
            updates._doctor_quick(root)

    def test_switch_resets_the_crash_counter_for_the_new_version(self):
        old = self._old_tree(updates.VERSIONS / '0.3.0')
        updates.CURRENT.symlink_to(old)
        self._health({'safe_mode': {'since': 1.0, 'crashes': 3}, 'crashes': [1.0, 2.0, 3.0]})
        self._install(old)
        from omarchy_ai.core import health
        self.assertEqual(health.status(updates._health_file()), {'safe_mode': False, 'since': None, 'crashes': 0})

    def test_source_checkout_is_never_converted(self):
        checkout = self._old_tree(self.base / 'checkout')
        (checkout / '.git').write_text('gitdir: elsewhere\n')  # a worktree has a .git file
        commands = []
        with patch.object(updates, 'discover') as discover:
            self._install(checkout, commands=commands)
        discover.assert_not_called()
        self.assertEqual(commands, [])
        self.assertFalse(updates.CURRENT.exists())
        self.assertFalse(updates.VERSIONS.exists())
        status = updates.update_status()
        self.assertEqual(status['state'], 'refused')
        self.assertIn('source checkout', status['message'])

    def test_source_checkout_does_not_start_the_worker(self):
        with patch.object(updates, 'is_dev_checkout', return_value=True), \
                patch.object(updates, 'check_updates', return_value={**self.release, 'ok': True, 'available': True, 'current_version': '0.3.0'}), \
                patch.object(updates.subprocess, 'run') as run:
            ok, message = updates.request_update()
        self.assertFalse(ok)
        self.assertIn('git pull', message)
        run.assert_not_called()

    def test_worker_runs_as_a_module_from_the_physical_tree(self):
        with patch.object(updates, 'check_updates', return_value={**self.release, 'ok': True, 'available': True}), \
                patch.object(updates.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            updates.request_update()
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('-m') + 1], 'omarchy_ai.core.updates')
        self.assertIn('--working-directory=' + str(updates.ROOT), argv)


class ReleaseSignatureTests(_UpdateIsolation):
    """Signature policy (verify_release) and the asset names it relies on."""

    def setUp(self):
        super().setUp()
        from test_release_signing import ThrowawayKey
        self.key = ThrowawayKey()
        self.archive, self.checksum = bundle(self.base)
        self.signature = self.archive.with_name(self.archive.name + '.minisig')

    def test_old_asset_names_still_found_and_signature_is_optional(self):
        package = 'omarchy-ai-0.5.0-linux-x86_64.tar.gz'
        old_style = self._github_release('0.5.0')
        signed = self._github_release('0.6.0', assets=[
            {'name': 'omarchy-ai-0.6.0-linux-x86_64.tar.gz'}, {'name': 'omarchy-ai-0.6.0-linux-x86_64.tar.gz.sha256'},
            {'name': 'omarchy-ai-0.6.0-linux-x86_64.tar.gz.minisig'}])
        unsigned, newer = updates.candidates_from_releases([old_style, signed])
        self.assertEqual(unsigned['package'], package)
        self.assertIsNone(unsigned['signature_url'])
        self.assertEqual(newer['signature_url'],
                         'https://github.com/omribenami/Omarchy-AI/releases/download/v0.6.0/omarchy-ai-0.6.0-linux-x86_64.tar.gz.minisig')
        self.assertTrue(updates._bundle_url_ok(newer['signature_url'], 'omarchy-ai-0.6.0-linux-x86_64.tar.gz.minisig'))
        self.assertFalse(updates._bundle_url_ok(newer['signature_url'], 'omarchy-ai-0.6.0-linux-x86_64.tar.gz.sig'))
        # A signature alone is not a bundle: the two original names are required.
        only_sig = self._github_release('0.7.0', assets=[{'name': 'omarchy-ai-0.7.0-linux-x86_64.tar.gz'},
                                                         {'name': 'omarchy-ai-0.7.0-linux-x86_64.tar.gz.minisig'}])
        self.assertEqual(updates.candidates_from_releases([only_sig]), [])
        self.assertEqual(updates.bundle_names('0.6.0'), ('omarchy-ai-0.6.0-linux-x86_64.tar.gz',
                                                       'omarchy-ai-0.6.0-linux-x86_64.tar.gz.sha256'))

    def test_valid_signature_is_accepted(self):
        self.signature.write_text(self.key.sign(self.archive))
        note = updates.verify_release(self.archive, self.checksum, self.signature, '0.4.0', keys=[self.key.public()])
        self.assertIn('signature verified', note)

    def test_invalid_signature_is_refused(self):
        from test_release_signing import ThrowawayKey
        self.signature.write_text(ThrowawayKey().sign(self.archive))
        with self.assertRaisesRegex(ValueError, 'signature refused'):
            updates.verify_release(self.archive, self.checksum, self.signature, '0.4.0', keys=[self.key.public()])

    def test_signature_of_a_corrupt_archive_never_gets_checked(self):
        self.signature.write_text(self.key.sign(self.archive))
        self.archive.write_bytes(self.archive.read_bytes() + b'x')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            updates.verify_release(self.archive, self.checksum, self.signature, '0.4.0', keys=[self.key.public()])

    def test_unsigned_release_is_sha256_only_until_a_key_is_pinned(self):
        self.assertIn('not signed', updates.verify_release(self.archive, self.checksum, None, '0.4.0', keys=[]))
        with self.assertRaisesRegex(ValueError, 'not signed'):
            updates.verify_release(self.archive, self.checksum, None, '0.4.0', keys=[self.key.public()])

    def test_signed_release_without_a_pinned_key_is_sha256_only(self):
        self.signature.write_text(self.key.sign(self.archive))
        note = updates.verify_release(self.archive, self.checksum, self.signature, '0.4.0', keys=[])
        self.assertIn('pins no release key', note)

    def test_install_downloads_and_checks_the_signature(self):
        old = self.base / 'old'
        old.mkdir()
        (old / 'pyproject.toml').write_text('[project]\nversion="0.3.0"')
        signed = {**self.release, 'signature_url': self.release['package_url'] + '.minisig'}
        urls = []
        def download(url, path, limit):
            urls.append(url)
            path.write_bytes(b'x')
        with patch.object(updates, 'discover', return_value=signed), patch.object(updates, '_download', side_effect=download), \
                patch.object(updates, 'verify_release', side_effect=ValueError('Release signature refused: bad')) as verify, \
                patch.object(updates, '_command') as command, patch.object(updates, '_report_failure', return_value=(None, 'x')):
            with self.assertRaisesRegex(ValueError, 'signature refused'):
                updates.install('0.4.0', old)
        self.assertEqual(urls[-1], signed['signature_url'])
        self.assertTrue(verify.call_args.args[2].name.endswith('.minisig'))
        command.assert_not_called()
        self.assertFalse(updates.CURRENT.exists())

    def test_pinned_key_hides_a_newer_unsigned_release(self):
        with patch.object(updates, '_trusted_keys', return_value=[self.key.public()]), \
                patch.object(updates, 'installed_version', return_value='0.3.0'), \
                patch.object(updates, 'discover', return_value={**self.release, 'signature_url': None}):
            result = updates.check_updates(force=True)
            self.assertFalse(result['available'])
            self.assertTrue(result['unsigned'])
            self.assertFalse(updates.check_updates()['available'])  # the cached answer too
            self.assertEqual(updates.wake_notice(), '')
            with patch.object(updates.subprocess, 'run') as run:
                ok, message = updates.request_update()
        self.assertFalse(ok)
        self.assertIn('not signed', message)
        run.assert_not_called()


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
