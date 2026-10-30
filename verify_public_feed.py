#!/usr/bin/env python3
"""Read one public Firebase feed; no credentials, TCGCSV requests or writes to services."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from market_feed import FeedError, MAX_FEED_BYTES, MAX_FEED_ROWS, fresh, instant, iso, json_loads, validate_feed


def verify_body(body, now, expected=None):
    if len(body) > MAX_FEED_BYTES:
        raise FeedError('Public feed exceeds 16 MiB')
    feed = validate_feed(json_loads(body), now)
    if not fresh(instant(feed['sourceBuildAt']), now):
        raise FeedError('Source build is not fresh')
    if any(not fresh(instant(row['sourcePublishedAt']), now) for row in feed['prices']):
        raise FeedError('At least one source file publication is older than 72 hours')
    digest = hashlib.sha256(body).hexdigest()
    if expected is not None and digest != hashlib.sha256(expected).hexdigest():
        raise FeedError('Public feed does not match the expected initial file')
    stamps = [row['sourcePublishedAt'] for row in feed['prices']]
    return {
        'checkedAt': iso(now), 'schemaVersion': feed['schemaVersion'], 'currency': feed['currency'],
        'sourceBuildAt': feed['sourceBuildAt'], 'generatedAt': feed['generatedAt'],
        'sourceBuildAgeHours': round((now - instant(feed['sourceBuildAt'])).total_seconds() / 3600, 3),
        'prices': len(feed['prices']), 'bytes': len(body), 'sha256': digest,
        'sourcePublishedAtMin': min(stamps), 'sourcePublishedAtMax': max(stamps),
        'subtypes': dict(Counter(row['subTypeName'] for row in feed['prices'])),
        'withinAndroidLimits': len(feed['prices']) <= MAX_FEED_ROWS,
        'historyPrices': sum('history' in row for row in feed['prices']),
        'complete7DayHistories': sum(row.get('history', {}).get('days7') == 7 for row in feed['prices']),
        'complete30DayHistories': sum(row.get('history', {}).get('days30') == 30 for row in feed['prices']),
        'expectedInitialFileMatches': expected is not None,
        'mew46466': [row for row in feed['prices'] if row['productId'] == 46466 and row['subTypeName'] == 'Reverse Holofoil'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--expected-file', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith(('.web.app', '.firebaseapp.com')) or parsed.path != '/feed.json' or parsed.query or parsed.username or parsed.password:
        raise FeedError('Expected a public Firebase HTTPS /feed.json URL')
    request = Request(args.url, headers={'User-Agent': 'PokeInvestFeedVerifier/1.0', 'Accept': 'application/json'})
    with urlopen(request, timeout=20) as response:
        if response.status != 200 or response.geturl() != args.url:
            raise FeedError('Expected HTTP 200 without a redirect')
        if response.headers.get_content_type() != 'application/json':
            raise FeedError('Expected application/json; an HTML Hosting fallback is not a price feed')
        body = response.read(MAX_FEED_BYTES + 1)
        report = verify_body(body, datetime.now(timezone.utc), args.expected_file.read_bytes() if args.expected_file else None)
        report['url'] = args.url
        report['httpStatus'] = response.status
        report['headers'] = {name: response.headers.get(name) for name in ('Content-Type', 'Cache-Control', 'Access-Control-Allow-Origin', 'ETag', 'Date')}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
