#!/usr/bin/env python3
"""One daily server-side TCGCSV sync. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BASE_URL = 'https://tcgcsv.com'
USER_AGENT = 'PokeInvestMarketFeed/1.0 (+https://tcgcsv.com/docs)'
SUBTYPES = frozenset({'Normal', 'Holofoil', 'Reverse Holofoil'})
MAX_AGE = timedelta(hours=72)
MAX_BYTES = 25 * 1024 * 1024
MAX_FEED_BYTES = 16 * 1024 * 1024
MAX_FEED_ROWS = 100_000
MAX_REQUESTS = 1000
UTC = timezone.utc


class FeedError(Exception):
    """Abort the complete publication and preserve the previous good feed."""


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise FeedError('Missing timestamp')
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timestamp without time zone')
        return parsed.astimezone(UTC)
    except ValueError as error:
        raise FeedError(f'Invalid timestamp: {value!r}') from error


def publication_date(value: str | None) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value or '')
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def fresh(timestamp: datetime, now: datetime) -> bool:
    return timedelta(0) <= now - timestamp <= MAX_AGE


def integer(value: Any) -> bool:
    return type(value) is int and 0 < value <= 9_007_199_254_740_991


def positive(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def json_loads(body: bytes | str) -> Any:
    try:
        return json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError) as error:
        raise FeedError('Invalid JSON response') from error


def results(body: bytes) -> list[dict]:
    data = json_loads(body)
    if not isinstance(data, dict) or data.get('success') is not True or data.get('errors') != [] or not isinstance(data.get('results'), list):
        raise FeedError('TCGCSV returned an unsuccessful or invalid collection')
    if any(not isinstance(row, dict) for row in data['results']):
        raise FeedError('Invalid collection row')
    return data['results']


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n'
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.pending-', delete=False, encoding='utf-8') as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def last_modified(self) -> str | None:
        return self.headers.get('last-modified')


class HttpClient:
    """Serial requests, >=110ms between requests, bounded bodies, no retries."""
    def __init__(self, opener=urlopen, sleep=time.sleep, monotonic=time.monotonic):
        self.opener = opener
        self.sleep = sleep
        self.monotonic = monotonic
        self.last_finished: float | None = None
        self.requests = 0

    def get(self, path: str, cache: dict | None = None) -> Response:
        if not re.fullmatch(r'/last-updated\.txt|/tcgplayer/3/groups|/tcgplayer/3/[1-9][0-9]*/(?:prices|products)', path):
            raise FeedError('Unexpected TCGCSV endpoint')
        if self.requests >= MAX_REQUESTS:
            raise FeedError('Request budget exceeded; no publication')
        if self.last_finished is not None:
            self.sleep(max(0.0, 0.11 - (self.monotonic() - self.last_finished)))
        headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json, text/plain'}
        if cache and cache.get('lastModified'):
            headers['If-Modified-Since'] = cache['lastModified']
        if cache and cache.get('etag'):
            headers['If-None-Match'] = cache['etag']
        self.requests += 1
        try:
            with self.opener(Request(BASE_URL + path, headers=headers), timeout=20) as response:
                if response.status != 200:
                    raise FeedError(f'Unexpected HTTP {response.status}: {path}')
                if urlparse(response.geturl()).hostname != 'tcgcsv.com':
                    raise FeedError('Unexpected response host')
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise FeedError('Response exceeds size limit')
                return Response(200, {k.lower(): v for k, v in response.headers.items()}, body)
        except HTTPError as error:
            if error.code == 304 and cache:
                return Response(304, {k.lower(): v for k, v in error.headers.items()}, b'')
            raise FeedError(f'HTTP {error.code}: {path}; no retry') from error
        except (URLError, TimeoutError, OSError) as error:
            raise FeedError(f'Network failure: {path}; no retry') from error
        finally:
            self.last_finished = self.monotonic()


def cache_headers(response: Response) -> dict:
    return {'lastModified': response.last_modified, 'etag': response.headers.get('etag')}


def validate_groups(rows: list[dict]) -> list[dict]:
    groups: dict[int, dict] = {}
    for row in rows:
        group_id = row.get('groupId')
        if not integer(group_id) or row.get('categoryId') != 3 or not isinstance(row.get('name'), str):
            raise FeedError('Invalid Pokemon group identity')
        if group_id in groups and groups[group_id] != row:
            raise FeedError(f'Conflicting group {group_id}')
        groups[group_id] = row
    if not groups or len(groups) > 450:
        raise FeedError('Empty or unexpected group count')
    return sorted(groups.values(), key=lambda row: row['groupId'])


def catalog(rows: list[dict], group_id: int) -> dict:
    all_products: dict[int, dict] = {}
    singles = []
    for row in rows:
        pid = row.get('productId')
        if not integer(pid) or row.get('categoryId') != 3 or row.get('groupId') != group_id:
            raise FeedError(f'Product identity mismatch in group {group_id}')
        if pid in all_products and all_products[pid] != row:
            raise FeedError(f'Conflicting product identity {pid}')
        all_products[pid] = row
        fields = row.get('extendedData')
        if not isinstance(fields, list):
            continue
        number = next((field.get('value') for field in fields if isinstance(field, dict) and field.get('name') == 'Number'), None)
        rarity = next((field.get('value') for field in fields if isinstance(field, dict) and field.get('name') == 'Rarity'), '')
        # A set number is a conservative positive identification of a single card.
        # Sealed products/code cards without a meaningful card number are omitted.
        valid_number = isinstance(number, str) and re.search(r'[A-Za-z0-9]', number) and number.strip().lower() not in {'n/a', 'na', 'none', 'unknown', 'code card'}
        if valid_number and 'code' not in str(rarity).lower() and isinstance(row.get('name'), str) and row['name'].strip():
            singles.append(pid)
    return {'allIds': sorted(all_products), 'singleIds': sorted(set(singles))}


def merge_price(output: dict, row: dict) -> None:
    key = (row['productId'], row['subTypeName'])
    if key in output and output[key] != row:
        raise FeedError(f'Conflicting duplicate price {key}')
    output[key] = row


def validate_feed(feed: Any, now: datetime) -> dict:
    if not isinstance(feed, dict) or type(feed.get('schemaVersion')) is not int or feed.get('schemaVersion') != 1 or feed.get('currency') != 'USD':
        raise FeedError('Invalid previous feed schema')
    build, generated = instant(feed.get('sourceBuildAt')), instant(feed.get('generatedAt'))
    if build > now or generated > now or build > generated:
        raise FeedError('Future/inconsistent previous feed timestamp')
    rows = feed.get('prices')
    if not isinstance(rows, list) or not rows or len(rows) > MAX_FEED_ROWS:
        raise FeedError('Invalid previous feed prices')
    merged: dict = {}
    for row in rows:
        if not isinstance(row, dict) or not integer(row.get('productId')) or row.get('subTypeName') not in SUBTYPES or not positive(row.get('marketPrice')):
            raise FeedError('Invalid previous feed price')
        published = instant(row.get('sourcePublishedAt'))
        if published > now or published > build:
            raise FeedError('Future/inconsistent source file publication date')
        merge_price(merged, row)
    return feed


def load_previous(url: str, now: datetime, opener=urlopen) -> dict | None:
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith(('.web.app', '.firebaseapp.com')) or parsed.path != '/feed.json' or parsed.query or parsed.username or parsed.password:
        raise FeedError('Previous feed must use the dedicated Firebase /feed.json URL')
    try:
        with opener(Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'}), timeout=15) as response:
            if response.status != 200 or response.geturl() != url:
                raise FeedError('Unexpected previous-feed response')
            body = response.read(MAX_FEED_BYTES + 1)
            if len(body) > MAX_FEED_BYTES:
                raise FeedError('Previous feed exceeds size limit')
            return validate_feed(json_loads(body), now)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise FeedError(f'Could not check previous feed: HTTP {error.code}') from error
    except (URLError, TimeoutError, OSError) as error:
        raise FeedError('Could not check previous feed') from error


def write_public(directory: Path, feed: dict) -> None:
    encoded = json.dumps(feed, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n'
    if len(feed['prices']) > MAX_FEED_ROWS or len(encoded.encode('utf-8')) > MAX_FEED_BYTES:
        raise FeedError('Feed exceeds Android limits (100000 prices / 16 MiB); keeping previous publication')
    directory.mkdir(parents=True, exist_ok=True)
    # Only these two files belong in the public deployment directory.
    unexpected = [p.name for p in directory.iterdir() if p.name not in {'feed.json', 'index.html'}]
    if unexpected:
        raise FeedError('Public directory contains unexpected files; refusing deployment preparation')
    atomic_json(directory / 'feed.json', feed)
    text = (Path(__file__).with_name('index.template.html')).read_text(encoding='utf-8')
    for key, value in {'BUILD': feed['sourceBuildAt'], 'GENERATED': feed['generatedAt'], 'COUNT': len(feed['prices'])}.items():
        text = text.replace('{{' + key + '}}', html.escape(str(value)))
    temporary = directory / '.index.pending'
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, directory / 'index.html')


class Ingestor:
    def __init__(self, client: HttpClient, state_dir: Path, output_dir: Path, now: datetime | None = None):
        self.client, self.state_dir, self.output_dir = client, state_dir, output_dir
        self.now = now or datetime.now(UTC)
        self.state_file = state_dir / 'state.json'
        self.state = json_loads(self.state_file.read_bytes()) if self.state_file.exists() else {'version': 1, 'groups': {}}
        if not isinstance(self.state, dict) or self.state.get('version') != 1:
            raise FeedError('Invalid ingestion state; investigate before resetting it')
        self.stats: Counter = Counter()

    def save(self) -> None:
        atomic_json(self.state_file, self.state)

    def run(self, previous_url: str | None = None) -> dict:
        marker = self.client.get('/last-updated.txt')
        if marker.status != 200:
            raise FeedError('Build marker must return HTTP 200')
        try:
            build = instant(marker.body.decode('utf-8').strip())
        except UnicodeError as error:
            raise FeedError('Invalid build marker') from error
        if not fresh(build, self.now):
            raise FeedError('Source build is future-dated or older than 72 hours; keeping previous feed')
        build_text = iso(build)
        previous = self.state.get('lastFeed')
        if previous is None and previous_url:
            previous = load_previous(previous_url, self.now)
            if previous:
                self.state['lastFeed'] = previous
                self.save()
        if previous:
            validate_feed(previous, self.now)
            prior_build = instant(previous['sourceBuildAt'])
            if build < prior_build:
                raise FeedError('Source build regressed; keeping previous feed')
            if build == prior_build:
                if any(not fresh(instant(row['sourcePublishedAt']), self.now) for row in previous['prices']):
                    raise FeedError('Unchanged build contains source files older than 72 hours; do not republish as current')
                # Preserve every timestamp, including generatedAt. A fresh download
                # or rerun never refreshes the age of a previously published price.
                write_public(self.output_dir, previous)
                return {'changed': False, 'ready': True, 'reason': 'same-source-build', 'sourceBuildAt': build_text, 'prices': len(previous['prices'])}
        if self.state.get('lastAttemptedBuildAt') == build_text:
            raise FeedError('This source build already failed/was interrupted; waiting for the next build, without duplicate downloads')
        self.state['lastAttemptedBuildAt'] = build_text
        self.state['lastAttemptAt'] = iso(self.now)
        self.save()
        groups = validate_groups(results(self.client.get('/tcgplayer/3/groups').body))
        staged_groups = deepcopy(self.state.get('groups', {}))
        previous_rows = previous['prices'] if previous else []
        output: dict = {}
        for group in groups:
            gid = group['groupId']
            meta = staged_groups.get(str(gid), {})
            previous_single_ids = set(meta.get('singleIds', []))
            old_rows = [row for row in previous_rows if row['productId'] in previous_single_ids]
            conditional = meta.get('prices') if previous is not None and meta.get('allIds') is not None else None
            response = self.client.get(f'/tcgplayer/3/{gid}/prices', conditional)
            effective_headers = meta.get('prices', {}) if response.status == 304 else cache_headers(response)
            published = publication_date(effective_headers.get('lastModified'))
            if published is None or not fresh(published, self.now):
                self.stats['groupsSkippedMissingFutureOrOldPublication'] += 1
                continue
            if published > build:
                raise FeedError(f'Group {gid} was published after the global marker; source build may be in progress')
            price_rows = old_rows if response.status == 304 else results(response.body)
            price_ids = {row['productId'] for row in price_rows if integer(row.get('productId'))}
            known_ids = set(meta.get('allIds', []))
            catalog_old = not meta.get('catalogCheckedAt') or self.now - instant(meta['catalogCheckedAt']) > timedelta(days=7)
            need_products = not meta or meta.get('groupModifiedOn') != group.get('modifiedOn') or not price_ids.issubset(known_ids) or catalog_old
            if need_products:
                products = self.client.get(f'/tcgplayer/3/{gid}/products', meta.get('products'))
                if products.status == 304:
                    if 'allIds' not in meta:
                        raise FeedError('304 product catalog without known identities')
                else:
                    meta.update(catalog(results(products.body), gid))
                    meta['products'] = cache_headers(products)
                meta['catalogCheckedAt'] = iso(self.now)
            single_ids = set(meta.get('singleIds', []))
            for row in price_rows:
                if not integer(row.get('productId')) or row.get('productId') not in single_ids:
                    self.stats['nonSingleOrUnknownProductRows'] += 1
                    continue
                if row.get('subTypeName') not in SUBTYPES or not positive(row.get('marketPrice')):
                    self.stats['invalidOrUnsupportedPriceRows'] += 1
                    continue
                merge_price(output, {'productId': row['productId'], 'subTypeName': row['subTypeName'], 'marketPrice': row['marketPrice'], 'sourcePublishedAt': iso(published)})
            meta['prices'] = effective_headers
            meta['groupModifiedOn'] = group.get('modifiedOn')
            staged_groups[str(gid)] = meta
            self.stats['groupsAccepted'] += 1
        if not output:
            raise FeedError('No fresh verified single-card prices; keeping previous feed')
        feed = {'schemaVersion': 1, 'currency': 'USD', 'sourceBuildAt': build_text, 'generatedAt': iso(self.now), 'prices': sorted(output.values(), key=lambda row: (row['productId'], row['subTypeName']))}
        validate_feed(feed, self.now)
        # No output or successful state is replaced before every group has completed.
        write_public(self.output_dir, feed)
        self.state.update(lastSuccessfulBuildAt=build_text, lastFeed=feed, groups=staged_groups)
        self.save()
        return {'changed': True, 'ready': True, 'sourceBuildAt': build_text, 'prices': len(feed['prices']), 'groups': len(groups), 'stats': dict(self.stats)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path('.state'))
    parser.add_argument('--output-dir', type=Path, default=Path('public'))
    parser.add_argument('--previous-feed-url')
    args = parser.parse_args(argv)
    try:
        ingestor = Ingestor(HttpClient(), args.state_dir, args.output_dir)
        report = ingestor.run(args.previous_feed_url)
        report['requests'] = ingestor.client.requests
        atomic_json(args.state_dir / 'last-report.json', report)
        print(json.dumps(report))
        if os.getenv('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
                handle.write('ready=true\n')
                handle.write(f"changed={str(report['changed']).lower()}\n")
        return 0
    except (FeedError, OSError) as error:
        print(f'Feed not published: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
