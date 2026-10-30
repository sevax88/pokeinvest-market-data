from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import deploy_hosting as hosting
from market_feed import instant

NOW = instant('2026-10-30T17:00:00Z')
TOKEN = 'not-a-real-token.only-used-offline'
ENV = {'FIREBASE_PROJECT': hosting.PROJECT, 'FIREBASE_SITE': hosting.SITE, 'FIREBASE_ACCESS_TOKEN': TOKEN}
VERSION = hosting.SITE_NAME + '/versions/test-version-123'
UPLOAD = hosting.UPLOAD + VERSION + '/files'
FEED = {'schemaVersion': 1, 'currency': 'USD', 'sourceBuildAt': '2026-10-29T20:06:01Z',
        'generatedAt': '2026-10-30T15:20:33Z', 'prices': [{'productId': 46466,
        'subTypeName': 'Reverse Holofoil', 'marketPrice': 649.13, 'sourcePublishedAt': '2026-10-29T20:03:47Z'}]}


class Reply:
    def __init__(self, url, body, status=200):
        self.url, self.status = url, status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def geturl(self):
        return self.url
    def read(self, size):
        return self.body[:size]


class FakeHosting:
    def __init__(self, overrides=None, cached=False):
        self.overrides = overrides or {}
        self.cached = cached
        self.calls = []
        self.uploads = 0
        self.stage_calls = []
    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        url = request.full_url
        if url.endswith('/versions'):
            stage, value = 'create', {'name': VERSION, 'status': 'CREATED'}
        elif url.endswith(':populateFiles'):
            files = json.loads(request.data)['files']
            stage, value = 'populate', {'uploadUrl': UPLOAD, 'uploadRequiredHashes': [] if self.cached else list(files.values())}
        elif url.startswith(hosting.UPLOAD):
            self.uploads += 1
            stage, value = 'upload' + str(self.uploads), b''
        elif request.method == 'PATCH':
            stage, value = 'finalize', {'name': VERSION, 'status': 'FINALIZED'}
        elif '/releases?' in url:
            stage, value = 'release', {'name': hosting.SITE_NAME + '/releases/test-release-123',
                                      'version': {'name': VERSION, 'status': 'FINALIZED'}, 'type': 'DEPLOY'}
        else:
            raise AssertionError('Unexpected request route')
        self.stage_calls.append(stage)
        value = self.overrides.get(stage, value)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Reply) else Reply(url, value)


class HostingDeploymentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.public = self.root / 'public'
        self.public.mkdir()
        self.write_feed(FEED)
        (self.public / 'index.html').write_text('<!doctype html><title>Public price data</title>', encoding='utf-8')

    def write_feed(self, feed):
        (self.public / 'feed.json').write_text(json.dumps(feed), encoding='utf-8')

    def deploy(self, fake=None, **kwargs):
        return hosting.deploy(self.public, environ=ENV, opener=fake or FakeHosting(), clock=lambda: NOW, **kwargs)

    def test_full_protocol_hashes_compressed_bytes_and_releases_last(self):
        fake = FakeHosting()
        report = self.deploy(fake)
        self.assertEqual(fake.stage_calls, ['create', 'populate', 'upload1', 'upload2', 'finalize', 'release'])
        self.assertEqual(report['prices'], 1)
        self.assertEqual(report['uploadedFiles'], 2)
        self.assertEqual(json.loads(fake.calls[0][0].data), {'config': hosting.CONFIG})
        files = json.loads(fake.calls[1][0].data)['files']
        self.assertEqual(set(files), {'/feed.json', '/index.html'})
        for request, timeout in fake.calls:
            self.assertEqual(timeout, 30)
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
            self.assertTrue(hosting.allowed_request(request.method, request.full_url))
        for request, _ in fake.calls[2:4]:
            digest = hashlib.sha256(request.data).hexdigest()
            self.assertTrue(request.full_url.endswith('/' + digest))
            path = next(path for path, value in files.items() if value == digest)
            raw = (self.public / path.removeprefix('/')).read_bytes()
            self.assertEqual(gzip.decompress(request.data), raw)
            self.assertEqual(request.data, hosting.deterministic_gzip(raw))
            self.assertEqual(request.data[4:8], bytes(4))
            self.assertEqual(request.data[9], 255)
            self.assertNotEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(fake.calls[-2][0].full_url, hosting.API + VERSION + '?updateMask=status')
        self.assertNotIn(TOKEN, json.dumps(report))

    def test_cached_content_does_not_upload_and_missing_repeated_field_means_empty(self):
        fake = FakeHosting({'populate': {'uploadUrl': UPLOAD}}, cached=True)
        report = self.deploy(fake)
        self.assertEqual(report['uploadedFiles'], 0)
        self.assertEqual(fake.stage_calls, ['create', 'populate', 'finalize', 'release'])

    def test_target_and_token_are_required_before_any_request(self):
        for key, value in [('FIREBASE_PROJECT', 'other-project'), ('FIREBASE_SITE', hosting.PROJECT),
                           ('FIREBASE_SITE', ''), ('FIREBASE_ACCESS_TOKEN', ''),
                           ('FIREBASE_ACCESS_TOKEN', 'a\r\nAuthorization: other')]:
            with self.subTest(key=key, value=value):
                fake, env = FakeHosting(), dict(ENV)
                env[key] = value
                with self.assertRaises(hosting.DeploymentError):
                    hosting.deploy(self.public, environ=env, opener=fake, clock=lambda: NOW)
                self.assertFalse(fake.calls)

    def test_only_two_regular_files_are_allowed(self):
        extra = self.public / 'private.json'
        extra.write_text('{}')
        with self.assertRaises(hosting.DeploymentError):
            self.deploy()
        extra.unlink()
        (self.public / 'index.html').unlink()
        private = self.root / 'private.html'
        private.write_text('private')
        (self.public / 'index.html').symlink_to(private)
        with self.assertRaises(hosting.DeploymentError):
            self.deploy()
        (self.public / 'index.html').unlink()
        with self.assertRaises(hosting.DeploymentError):
            self.deploy()

    def test_feed_contract_dates_and_file_size_fail_before_network(self):
        changes = [lambda f: f.update(currency='EUR'), lambda f: f.update(extraPrivateField='private'),
                   lambda f: f['prices'][0].update(sourcePublishedAt='2026-10-20T20:03:47Z'),
                   lambda f: f.update(sourceBuildAt='2027-01-01T00:00:00Z'),
                   lambda f: f['prices'][0].update(marketPrice=-1)]
        for change in changes:
            feed, fake = deepcopy(FEED), FakeHosting()
            change(feed)
            self.write_feed(feed)
            with self.assertRaises(hosting.DeploymentError):
                self.deploy(fake)
            self.assertFalse(fake.calls)
        self.write_feed(FEED)
        with patch.object(hosting, 'MAX_FEED_BYTES', 10), self.assertRaises(hosting.DeploymentError):
            self.deploy()

    def test_created_version_is_bound_to_exact_site_and_safe_id(self):
        for name in ['sites/another-site/versions/id', VERSION + '?redirect=other', VERSION + '/../id']:
            fake = FakeHosting({'create': {'name': name, 'status': 'CREATED'}})
            with self.assertRaises(hosting.DeploymentError):
                self.deploy(fake)
            self.assertEqual(fake.stage_calls, ['create'])

    def test_upload_url_cannot_change_host_site_version_scheme_or_add_query(self):
        urls = [UPLOAD.replace('https:', 'http:'), UPLOAD.replace('googleapis.com', 'googleapis.com.evil.test'),
                UPLOAD.replace(hosting.SITE, 'other-site'), UPLOAD.replace('test-version-123', 'other-version'),
                UPLOAD + '?token=anything', UPLOAD + '#fragment', UPLOAD.replace('https://', 'https://user@'),
                UPLOAD.replace('.com/', '.com:443/')]
        for url in urls:
            with self.subTest(url=url):
                fake = FakeHosting({'populate': {'uploadUrl': url, 'uploadRequiredHashes': []}})
                with self.assertRaises(hosting.DeploymentError):
                    self.deploy(fake)
                self.assertEqual(fake.stage_calls, ['create', 'populate'])

    def test_unknown_duplicate_or_invalid_hash_list_cannot_upload_or_release(self):
        _, _, files, _ = hosting.prepare_public(self.public, NOW)
        digest = next(iter(files.values()))
        for required in [['0' * 64], [digest, digest], 'not-an-array', [None], {}]:
            fake = FakeHosting({'populate': {'uploadUrl': UPLOAD, 'uploadRequiredHashes': required}})
            with self.assertRaises(hosting.DeploymentError):
                self.deploy(fake)
            self.assertEqual(fake.stage_calls, ['create', 'populate'])

    def test_every_pre_release_transport_failure_stops_without_retry_or_release(self):
        stages = ['create', 'populate', 'upload1', 'upload2', 'finalize']
        for stage in stages:
            with self.subTest(stage=stage):
                fake = FakeHosting({stage: URLError('private transport text ' + TOKEN)})
                with self.assertRaisesRegex(hosting.DeploymentError, 'network request failed'):
                    self.deploy(fake)
                self.assertNotIn('release', fake.stage_calls)
                self.assertEqual(fake.stage_calls.count(stage), 1)
        fake = FakeHosting({'finalize': {'name': VERSION, 'status': 'CREATED'}})
        with self.assertRaisesRegex(hosting.DeploymentError, 'no release attempted'):
            self.deploy(fake)
        self.assertNotIn('release', fake.stage_calls)
        times = iter([NOW, instant('2026-11-02T17:00:00Z')])
        fake = FakeHosting()
        with self.assertRaisesRegex(hosting.DeploymentError, 'expired'):
            hosting.deploy(self.public, environ=ENV, opener=fake, clock=lambda: next(times))
        self.assertNotIn('release', fake.stage_calls)

    def test_redirects_and_non_allowlisted_requests_are_rejected(self):
        with self.assertRaisesRegex(hosting.DeploymentError, 'redirect refused'):
            hosting.NoRedirects().redirect_request(Request(hosting.API), None, 302, 'redirect', {}, 'https://evil.test')
        fake = FakeHosting({'create': Reply('https://evil.test', {})})
        with self.assertRaisesRegex(hosting.DeploymentError, 'redirect refused'):
            self.deploy(fake)
        self.assertEqual(fake.stage_calls, ['create'])
        fake = FakeHosting()
        client = hosting.HostingClient(TOKEN, fake)
        for url in ['https://evil.test', hosting.API + 'sites/other-site/versions',
                    hosting.API + hosting.SITE_NAME + '/versions?key=value']:
            with self.assertRaisesRegex(hosting.DeploymentError, 'destination'):
                client.request('POST', url, {})
        self.assertFalse(fake.calls)

    def test_invalid_or_oversize_api_response_never_releases(self):
        for value in [b'<html>not JSON</html>', [], b' ' * (hosting.MAX_RESPONSE_BYTES + 1)]:
            fake = FakeHosting({'create': value})
            with self.assertRaises(hosting.DeploymentError):
                self.deploy(fake)
            self.assertEqual(fake.stage_calls, ['create'])

    def test_cli_does_not_print_token_body_or_exception_details(self):
        for error in [HTTPError(hosting.API, 403, TOKEN, {}, io.BytesIO(('private response ' + TOKEN).encode())),
                      RuntimeError('unexpected private exception ' + TOKEN)]:
            fake, out, err = FakeHosting({'create': error}), io.StringIO(), io.StringIO()
            actual_deploy = hosting.deploy
            def offline_deploy(directory):
                return actual_deploy(directory, environ=ENV, opener=fake, clock=lambda: NOW)
            with patch.object(hosting, 'deploy', side_effect=offline_deploy), redirect_stdout(out), redirect_stderr(err):
                code = hosting.main(['--public-dir', str(self.public)])
            self.assertEqual(code, 1)
            self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
            self.assertNotIn('private', out.getvalue() + err.getvalue())
            self.assertNotIn('Traceback', err.getvalue())
            self.assertEqual(fake.stage_calls, ['create'])


if __name__ == '__main__':
    unittest.main()
