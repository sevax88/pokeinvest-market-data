#!/usr/bin/env python3
"""Retain observed daily USD prices in a durable, atomically published Hosting bundle."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from market_feed import FeedError, MAX_FEED_BYTES, SUBTYPES, UTC, atomic_json, instant, integer, iso, json_loads, positive, validate_feed
from verify_public_feed import verify_body

RETENTION_DAYS = 35
SHARD_COUNT = 64
MAX_SHARD_BYTES = 8 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024
MAX_SERIES = 100_000
INDEX_PATH = '/history/index.json'
HISTORY_ORIGINS = {'https://pokeinvest-market-data.web.app', 'https://pokeinvest-market-data.firebaseapp.com'}
BUCKETS = [f'{value:02x}' for value in range(SHARD_COUNT)]
HISTORY_PATHS = frozenset([INDEX_PATH] + [f'/history/{bucket}.json' for bucket in BUCKETS])


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def stamp(value):
    parsed = instant(value)
    if iso(parsed) != value:
        raise FeedError('History timestamps must be canonical UTC seconds')
    return parsed


def summary_for(observations):
    latest = observations[-1]
    last_day = stamp(latest['observedAt']).date()
    summary = {'observedAt': latest['observedAt']}
    for days in (7, 30):
        start = last_day - timedelta(days=days - 1)
        values = [row['marketPrice'] for row in observations if start <= stamp(row['observedAt']).date() <= last_day]
        summary[f'days{days}'] = len(values)
        summary[f'avg{days}'] = math.fsum(value / days for value in values) if len(values) == days else None
    return summary


def validate_bundle(bundle, now, feed=None):
    if not isinstance(bundle, dict) or set(bundle) != HISTORY_PATHS:
        raise FeedError('History bundle is incomplete or contains unexpected files')
    if len(bundle[INDEX_PATH]) > MAX_INDEX_BYTES:
        raise FeedError('History index exceeds size limit')
    manifest = json_loads(bundle[INDEX_PATH])
    fields = {'schemaVersion', 'currency', 'retentionDays', 'sourceBuildAt', 'generatedAt', 'shardCount', 'shards'}
    if not isinstance(manifest, dict) or set(manifest) != fields or type(manifest['schemaVersion']) is not int or manifest['schemaVersion'] != 2 or manifest['currency'] != 'USD' or type(manifest['retentionDays']) is not int or manifest['retentionDays'] != RETENTION_DAYS or type(manifest['shardCount']) is not int or manifest['shardCount'] != SHARD_COUNT:
        raise FeedError('Invalid history manifest')
    build, generated = stamp(manifest['sourceBuildAt']), stamp(manifest['generatedAt'])
    if build > generated or generated > now:
        raise FeedError('Future/inconsistent history manifest')
    earliest = generated.date() - timedelta(days=RETENTION_DAYS - 1)
    descriptors = manifest['shards']
    if not isinstance(descriptors, list) or len(descriptors) != SHARD_COUNT:
        raise FeedError('Incomplete history shard index')
    all_series, seen_buckets = {}, set()
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or set(descriptor) != {'bucket', 'path', 'sha256', 'bytes'}:
            raise FeedError('Invalid history shard descriptor')
        bucket = descriptor['bucket']
        if not isinstance(bucket, str) or bucket not in BUCKETS or bucket in seen_buckets or descriptor['path'] != f'/history/{bucket}.json':
            raise FeedError('Unexpected/duplicate history bucket')
        seen_buckets.add(bucket)
        body = bundle[descriptor['path']]
        if type(descriptor['bytes']) is not int or not 0 < descriptor['bytes'] <= MAX_SHARD_BYTES or len(body) != descriptor['bytes'] or hashlib.sha256(body).hexdigest() != descriptor['sha256']:
            raise FeedError('History shard hash or size mismatch')
        shard = json_loads(body)
        if not isinstance(shard, dict) or set(shard) != {'schemaVersion', 'currency', 'bucket', 'series'} or type(shard['schemaVersion']) is not int or shard['schemaVersion'] != 2 or shard['currency'] != 'USD' or shard['bucket'] != bucket or not isinstance(shard['series'], list):
            raise FeedError('Invalid history shard')
        for series in shard['series']:
            if not isinstance(series, dict) or set(series) != {'productId', 'subTypeName', 'observations'} or not integer(series['productId']) or series['subTypeName'] not in SUBTYPES or f"{series['productId'] % SHARD_COUNT:02x}" != bucket:
                raise FeedError('Invalid history identity or bucket')
            key = (series['productId'], series['subTypeName'])
            if key in all_series:
                raise FeedError('Duplicate history series')
            rows = series['observations']
            if not isinstance(rows, list) or not 1 <= len(rows) <= RETENTION_DAYS:
                raise FeedError('Invalid history observation count')
            last_day, last_publication = None, None
            for row in rows:
                if not isinstance(row, dict) or set(row) != {'observedAt', 'marketPrice', 'sourcePublishedAt'} or not positive(row['marketPrice']):
                    raise FeedError('Invalid history observation')
                observed, published = stamp(row['observedAt']), stamp(row['sourcePublishedAt'])
                day = observed.date()
                if not earliest <= day <= generated.date() or observed > generated or published > observed or published > build or (observed - published) > timedelta(hours=72):
                    raise FeedError('Invalid history observation dates')
                if last_day is not None and (day <= last_day or published < last_publication):
                    raise FeedError('Duplicate/unordered days or regressed history publication')
                last_day, last_publication = day, published
            all_series[key] = series
            if len(all_series) > MAX_SERIES:
                raise FeedError('History series limit exceeded')
    if feed is not None:
        validate_feed(feed, now)
        if len({(row['productId'], row['subTypeName']) for row in feed['prices']}) != len(feed['prices']):
            raise FeedError('Duplicate current price cannot match history')
        if feed['sourceBuildAt'] != manifest['sourceBuildAt'] or feed['generatedAt'] != manifest['generatedAt']:
            raise FeedError('Feed and history generation do not match')
        for row in feed['prices']:
            series = all_series.get((row['productId'], row['subTypeName']))
            if not series:
                raise FeedError('Current price is missing from history')
            observations = series['observations']
            latest = observations[-1]
            if latest['observedAt'] != feed['generatedAt'] or latest['marketPrice'] != row['marketPrice'] or latest['sourcePublishedAt'] != row['sourcePublishedAt'] or row.get('history') != summary_for(observations):
                raise FeedError('Current price or summary does not match its last observation')
    return manifest


def series_from_bundle(bundle):
    return {(series['productId'], series['subTypeName']): series
            for bucket in BUCKETS for series in json_loads(bundle[f'/history/{bucket}.json'])['series']}


def build_history(feed, previous, now):
    # Current prices must still be fresh; old retained observations need not be.
    verify_body(encoded(feed), now)
    current = deepcopy(feed)
    build, observed = stamp(feed['sourceBuildAt']), stamp(feed['generatedAt'])
    series_by_key = {}
    if previous is not None:
        manifest = validate_bundle(previous, now)
        prior_build, prior_observed = stamp(manifest['sourceBuildAt']), stamp(manifest['generatedAt'])
        if build < prior_build or observed < prior_observed:
            raise FeedError('Feed/history rollback refused')
        series_by_key = deepcopy(series_from_bundle(previous))
        if build == prior_build:
            if observed != prior_observed:
                raise FeedError('Unchanged source build must preserve observedAt')
            for row in current['prices']:
                series = series_by_key.get((row['productId'], row['subTypeName']))
                if not series:
                    raise FeedError('Unchanged build has a new identity')
                row['history'] = summary_for(series['observations'])
            validate_bundle(previous, now, current)
            return current, previous
    cutoff = observed.date() - timedelta(days=RETENTION_DAYS - 1)
    series_by_key = {key: {**series, 'observations': [row for row in series['observations'] if stamp(row['observedAt']).date() >= cutoff]}
                     for key, series in series_by_key.items()}
    series_by_key = {key: series for key, series in series_by_key.items() if series['observations']}
    seen = set()
    for row in current['prices']:
        key = (row['productId'], row['subTypeName'])
        if key in seen:
            raise FeedError('Duplicate current price cannot seed history')
        seen.add(key)
        series = series_by_key.setdefault(key, {'productId': key[0], 'subTypeName': key[1], 'observations': []})
        rows = series['observations']
        if rows and stamp(row['sourcePublishedAt']) < stamp(rows[-1]['sourcePublishedAt']):
            raise FeedError('Source publication rolled back for a price')
        observation = {'observedAt': feed['generatedAt'], 'marketPrice': row['marketPrice'], 'sourcePublishedAt': row['sourcePublishedAt']}
        if rows and stamp(rows[-1]['observedAt']).date() == observed.date():
            rows[-1] = observation
        else:
            rows.append(observation)
        row['history'] = summary_for(rows)
    shards = {bucket: {'schemaVersion': 2, 'currency': 'USD', 'bucket': bucket, 'series': []} for bucket in BUCKETS}
    for key in sorted(series_by_key):
        shards[f'{key[0] % SHARD_COUNT:02x}']['series'].append(series_by_key[key])
    bundle = {f'/history/{bucket}.json': encoded(shard) for bucket, shard in shards.items()}
    manifest = {'schemaVersion': 2, 'currency': 'USD', 'retentionDays': RETENTION_DAYS,
                'sourceBuildAt': feed['sourceBuildAt'], 'generatedAt': feed['generatedAt'], 'shardCount': SHARD_COUNT,
                'shards': [{'bucket': bucket, 'path': f'/history/{bucket}.json',
                            'sha256': hashlib.sha256(bundle[f'/history/{bucket}.json']).hexdigest(),
                            'bytes': len(bundle[f'/history/{bucket}.json'])} for bucket in BUCKETS]}
    bundle[INDEX_PATH] = encoded(manifest)
    validate_bundle(bundle, now, current)
    if len(encoded(current)) > MAX_FEED_BYTES:
        raise FeedError('Feed with history summaries exceeds Android 16 MiB limit')
    return current, bundle


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FeedError('History redirects are not allowed')


def load_previous_history(url, now, cache_dir=None, allow_bootstrap=False, opener=None):
    origin = next((origin for origin in HISTORY_ORIGINS if url == origin + INDEX_PATH), None)
    if origin is None:
        raise FeedError('History must use the dedicated Firebase index URL')
    open_request = opener or build_opener(NoRedirects()).open

    def download(path, limit, missing_ok=False):
        try:
            request = Request(origin + path, headers={'User-Agent': 'PokeInvestHistory/2.0', 'Accept': 'application/json', 'Accept-Encoding': 'gzip'})
            with open_request(request, timeout=20) as response:
                if response.status != 200 or response.geturl() != origin + path:
                    raise FeedError('Unexpected history response')
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise FeedError('History response exceeds size limit')
                encoding = response.headers.get('Content-Encoding', '').lower()
                if encoding == 'gzip':
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as handle:
                        body = handle.read(limit + 1)
                elif encoding not in ('', 'identity'):
                    raise FeedError('Unexpected history encoding')
                if len(body) > limit:
                    raise FeedError('Decompressed history exceeds size limit')
                return body
        except HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            raise FeedError(f'History unavailable: HTTP {int(error.code)}; previous publication retained') from None
        except (URLError, OSError, EOFError):
            raise FeedError('History download failed; previous publication retained') from None

    cache_dir = Path(cache_dir) if cache_dir is not None else None
    has_history_cache = cache_dir is not None and (cache_dir / 'index.json').exists()
    index = download(INDEX_PATH, MAX_INDEX_BYTES, missing_ok=allow_bootstrap and not has_history_cache)
    if index is None:
        return None
    manifest = json_loads(index)
    # Validate descriptors before using them as paths, even before full bundle validation.
    descriptors = manifest.get('shards') if isinstance(manifest, dict) else None
    if not isinstance(descriptors, list) or len(descriptors) != SHARD_COUNT:
        raise FeedError('Invalid history shard index')
    bundle = {INDEX_PATH: index}
    for entry in descriptors:
        if not isinstance(entry, dict) or not isinstance(entry.get('bucket'), str) or entry['bucket'] not in BUCKETS or entry.get('path') != f"/history/{entry['bucket']}.json" or not isinstance(entry.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256']) or type(entry.get('bytes')) is not int or not 0 < entry['bytes'] <= MAX_SHARD_BYTES or entry['path'] in bundle:
            raise FeedError('Invalid history shard descriptor')
        cached = cache_dir / f"{entry['bucket']}.json" if cache_dir else None
        body = cached.read_bytes() if cached is not None and cached.is_file() and not cached.is_symlink() and cached.stat().st_size == entry['bytes'] else None
        if body is None or hashlib.sha256(body).hexdigest() != entry['sha256']:
            body = download(entry['path'], MAX_SHARD_BYTES)
        bundle[entry['path']] = body
    validate_bundle(bundle, now)
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for path, body in bundle.items():
            atomic_bytes(cache_dir / Path(path).name, body)
    return bundle


def atomic_bytes(path, body):
    temporary = path.with_name(path.name + '.pending')
    temporary.write_bytes(body)
    os.replace(temporary, path)


def write_history_public(directory, feed, bundle, now):
    directory = Path(directory)
    validate_bundle(bundle, now, feed)
    body = encoded(feed)
    if len(body) > MAX_FEED_BYTES:
        raise FeedError('Feed with history summaries exceeds Android 16 MiB limit')
    if directory.is_symlink() or any(path.name not in {'feed.json', 'index.html', 'history'} or path.is_symlink() for path in directory.iterdir()):
        raise FeedError('Unexpected public files during history preparation')
    history_dir = directory / 'history'
    if history_dir.exists() and (not history_dir.is_dir() or any('/history/' + path.name not in HISTORY_PATHS or not path.is_file() or path.is_symlink() for path in history_dir.iterdir())):
        raise FeedError('Unexpected history output files')
    history_dir.mkdir(exist_ok=True)
    for path, content in bundle.items():
        atomic_bytes(directory / path.removeprefix('/'), content)
    atomic_bytes(directory / 'feed.json', body)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-dir', type=Path, default=Path('public'))
    parser.add_argument('--cache-dir', type=Path, default=Path('.state/history'))
    parser.add_argument('--previous-history-url')
    parser.add_argument('--bootstrap-history', action='store_true', help='Explicit first-history creation; an unavailable existing history is never silently reset')
    args = parser.parse_args(argv)
    try:
        now = datetime.now(UTC)
        if args.previous_history_url:
            previous = load_previous_history(args.previous_history_url, now, args.cache_dir, args.bootstrap_history)
        elif args.bootstrap_history and not (args.cache_dir / 'index.json').exists() and not (args.public_dir / 'history').exists():
            previous = None  # Offline initial preparation from a genuinely observed local feed.
        else:
            raise FeedError('Durable previous-history URL required outside explicit initial bootstrap')
        feed = json_loads((args.public_dir / 'feed.json').read_bytes())
        enriched, bundle = build_history(feed, previous, now)
        write_history_public(args.public_dir, enriched, bundle, now)
        report = {'schemaVersion': 2, 'historyChanged': previous != bundle, 'bootstrap': previous is None,
                  'shards': SHARD_COUNT, 'prices': len(enriched['prices']), 'sourceBuildAt': enriched['sourceBuildAt'],
                  'observedAt': enriched['generatedAt'], 'historyBytes': sum(map(len, bundle.values())), 'feedBytes': len(encoded(enriched))}
        atomic_json(args.cache_dir.parent / 'history-report.json', report)
        print(json.dumps(report))
        return 0
    except (FeedError, OSError, ValueError, TypeError) as error:
        print(f'History not published: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
