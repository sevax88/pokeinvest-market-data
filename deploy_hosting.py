#!/usr/bin/env python3
"""Publish the two public feed files through Firebase Hosting REST, without the CLI.

Protocol: https://firebase.google.com/docs/hosting/api-deploy
Only a successful final releases.create changes the live site. No retries or redirects.
The short-lived access token stays in process memory and is never written to disk.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from market_feed import FeedError, MAX_FEED_BYTES, json_loads
from verify_public_feed import verify_body

PROJECT = 'pokeinvest-23904'
SITE = 'pokeinvest-market-data'
API = 'https://firebasehosting.googleapis.com/v1beta1/'
UPLOAD = 'https://upload-firebasehosting.googleapis.com/upload/'
SITE_NAME = f'sites/{SITE}'
RESOURCE_ID = r'[A-Za-z0-9_-]{1,128}'
VERSION_PATTERN = rf'{SITE_NAME}/versions/{RESOURCE_ID}'
MAX_INDEX_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 30
CONFIG = {'headers': [
    {'glob': '**', 'headers': {'Cache-Control': 'public, max-age=300', 'X-Content-Type-Options': 'nosniff'}},
    {'glob': '/feed.json', 'headers': {'Content-Type': 'application/json; charset=utf-8', 'Access-Control-Allow-Origin': '*'}},
]}


class DeploymentError(Exception):
    """A safe fixed-text error: never include response bodies or credentials."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeploymentError('HTTP redirect refused')


def allowed_request(method, url):
    """Constrain the bearer credential to the dedicated site's exact REST routes."""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port or parsed.fragment:
        return False
    if parsed.netloc == 'upload-firebasehosting.googleapis.com':
        return method == 'POST' and not parsed.query and bool(re.fullmatch(
            rf'/upload/{VERSION_PATTERN}/files/[a-f0-9]{{64}}', parsed.path))
    if parsed.netloc != 'firebasehosting.googleapis.com':
        return False
    if method == 'POST' and not parsed.query:
        return parsed.path == f'/v1beta1/{SITE_NAME}/versions' or bool(re.fullmatch(
            rf'/v1beta1/{VERSION_PATTERN}:populateFiles', parsed.path))
    if method == 'PATCH':
        return parsed.query == 'updateMask=status' and bool(re.fullmatch(rf'/v1beta1/{VERSION_PATTERN}', parsed.path))
    if method == 'POST' and parsed.path == f'/v1beta1/{SITE_NAME}/releases':
        # Encoding is deliberately canonical; no extra query options are permitted.
        match = re.fullmatch(rf'versionName=sites%2F{SITE}%2Fversions%2F({RESOURCE_ID})', parsed.query)
        return match is not None
    return False


class HostingClient:
    def __init__(self, token, opener=None):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*', token):
            raise DeploymentError('Missing or invalid FIREBASE_ACCESS_TOKEN')
        self._token = token
        self._open = opener if opener is not None else build_opener(NoRedirects()).open

    def request(self, method, url, body, *, upload=False):
        try:
            permitted = allowed_request(method, url)
        except (ValueError, TypeError):
            permitted = False
        if not permitted:
            raise DeploymentError('Unexpected Hosting destination')
        encoded = body if upload else json.dumps(body, separators=(',', ':'), allow_nan=False).encode('utf-8')
        request = Request(url, data=encoded, method=method, headers={
            'Authorization': 'Bearer ' + self._token,
            'Content-Type': 'application/octet-stream' if upload else 'application/json',
            'User-Agent': 'PokeInvestHostingPublisher/1.0',
        })
        try:
            with self._open(request, timeout=TIMEOUT_SECONDS) as response:
                if response.geturl() != url:
                    raise DeploymentError('HTTP redirect refused')
                if response.status not in (200, 201):
                    raise DeploymentError('Unexpected Hosting HTTP status')
                if upload:
                    return None
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise DeploymentError('Hosting response exceeds size limit')
        except HTTPError as error:
            # Do not read the response body or stringify the exception/headers.
            raise DeploymentError(f'Hosting HTTP error {int(error.code)}') from None
        except (URLError, OSError, TimeoutError):
            raise DeploymentError('Hosting network request failed; no retry attempted') from None
        try:
            result = json_loads(raw)
        except FeedError:
            raise DeploymentError('Hosting returned invalid JSON') from None
        if not isinstance(result, dict):
            raise DeploymentError('Hosting returned an invalid object')
        return result


def deterministic_gzip(body):
    # GzipFile avoids a file name, variable timestamp, and platform-specific OS byte.
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', filename='', mtime=0, compresslevel=9) as handle:
        handle.write(body)
    return buffer.getvalue()


def prepare_public(directory, now):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise DeploymentError('Public directory must be a real directory')
    entries = list(directory.iterdir())
    if {entry.name for entry in entries} != {'feed.json', 'index.html'} or any(
            entry.is_symlink() or not entry.is_file() for entry in entries):
        raise DeploymentError('Public directory must contain only regular feed.json and index.html files')
    bodies = {}
    for name, limit in [('feed.json', MAX_FEED_BYTES), ('index.html', MAX_INDEX_BYTES)]:
        with (directory / name).open('rb') as handle:
            body = handle.read(limit + 1)
        if not body or len(body) > limit:
            raise DeploymentError('A public file is empty or exceeds its size limit')
        bodies['/' + name] = body
    try:
        metadata = verify_body(bodies['/feed.json'], now)
        feed = json_loads(bodies['/feed.json'])
        if set(feed) != {'schemaVersion', 'currency', 'sourceBuildAt', 'generatedAt', 'prices'} or any(
                set(row) != {'productId', 'subTypeName', 'marketPrice', 'sourcePublishedAt'} for row in feed['prices']):
            raise FeedError('Unexpected fields')
        bodies['/index.html'].decode('utf-8')
    except (FeedError, UnicodeError, TypeError, ValueError):
        raise DeploymentError('Public feed contract, freshness, or text validation failed') from None
    compressed = {path: deterministic_gzip(body) for path, body in bodies.items()}
    files = {path: hashlib.sha256(body).hexdigest() for path, body in compressed.items()}
    return bodies, compressed, files, metadata


def deploy(public_dir, *, environ=None, opener=None, clock=None):
    environ = os.environ if environ is None else environ
    if environ.get('FIREBASE_PROJECT') != PROJECT or environ.get('FIREBASE_SITE') != SITE:
        raise DeploymentError('Only the approved Firebase project and dedicated site are allowed')
    client = HostingClient(environ.get('FIREBASE_ACCESS_TOKEN'), opener)
    clock = clock or (lambda: datetime.now(timezone.utc))
    bodies, compressed, files, metadata = prepare_public(public_dir, clock())
    version = client.request('POST', API + SITE_NAME + '/versions', {'config': CONFIG})
    name = version.get('name')
    if not isinstance(name, str) or not re.fullmatch(VERSION_PATTERN, name) or version.get('status') != 'CREATED':
        raise DeploymentError('Hosting returned an unexpected created version')
    populated = client.request('POST', API + name + ':populateFiles', {'files': files})
    expected_upload = UPLOAD + name + '/files'
    if populated.get('uploadUrl') != expected_upload:
        raise DeploymentError('Hosting returned an unexpected upload destination')
    required = populated.get('uploadRequiredHashes', [])  # Repeated fields may be omitted when empty.
    known = {digest: compressed[path] for path, digest in files.items()}
    if not isinstance(required, list) or any(not isinstance(digest, str) or digest not in known for digest in required):
        raise DeploymentError('Hosting requested unknown file hashes')
    if len(required) != len(set(required)):
        raise DeploymentError('Hosting requested duplicate file hashes')
    for digest in required:
        client.request('POST', expected_upload + '/' + digest, known[digest], upload=True)
    finalized = client.request('PATCH', API + name + '?updateMask=status', {'status': 'FINALIZED'})
    if finalized.get('name') != name or finalized.get('status') != 'FINALIZED':
        raise DeploymentError('Hosting did not confirm the finalized version; no release attempted')
    # Upload time must not let a price-file publication silently pass the age limit.
    try:
        verify_body(bodies['/feed.json'], clock())
    except FeedError:
        raise DeploymentError('Feed expired during preparation; no release attempted') from None
    release = client.request('POST', API + SITE_NAME + '/releases?' + urlencode({'versionName': name}), {})
    release_name = release.get('name')
    released_version = release.get('version')
    if not isinstance(release_name, str) or not re.fullmatch(rf'{SITE_NAME}/releases/{RESOURCE_ID}', release_name) or not isinstance(released_version, dict) or released_version.get('name') != name or released_version.get('status') != 'FINALIZED' or release.get('type') != 'DEPLOY':
        raise DeploymentError('Release response could not be verified; check Hosting before any further deployment')
    report = {'site': SITE, 'version': name, 'release': release_name, 'files': files,
              'uploadedFiles': len(required), 'prices': metadata['prices'],
              'sourceBuildAt': metadata['sourceBuildAt'], 'feedSha256': metadata['sha256']}
    if client._token in json.dumps(report):
        raise DeploymentError('Unsafe release metadata; output suppressed')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-dir', type=Path, default=Path('public'))
    args = parser.parse_args(argv)
    try:
        report = deploy(args.public_dir)
    except DeploymentError as error:
        print('Deployment stopped: ' + str(error), file=sys.stderr)
        return 1
    except Exception:
        # Also suppress unexpected exceptions: they can contain transport credentials.
        print('Deployment stopped: unexpected local or transport error; details suppressed', file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
