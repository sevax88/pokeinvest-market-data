from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError

from history_feed import INDEX_PATH, SHARD_COUNT, build_history, encoded, load_previous_history, series_from_bundle, validate_bundle, write_history_public
from market_feed import FeedError, instant, iso, validate_feed, write_public
from deploy_hosting import prepare_public, DeploymentError

START = instant('2026-10-01T21:00:00Z')
URL = 'https://pokeinvest-market-data.web.app/history/index.json'


def feed_at(day=0, price=100, variant='Normal', hours=0):
    observed = START + timedelta(days=day, hours=hours)
    return {'schemaVersion': 1, 'currency': 'USD', 'sourceBuildAt': iso(observed - timedelta(minutes=30)),
            'generatedAt': iso(observed), 'prices': [{'productId': 46466, 'subTypeName': variant,
            'marketPrice': price, 'sourcePublishedAt': iso(observed - timedelta(hours=1))}]}


def replace_shard(bundle, edit):
    bundle = deepcopy(bundle)
    manifest = json.loads(bundle[INDEX_PATH])
    entry = next(entry for entry in manifest['shards'] if entry['bucket'] == f'{46466 % SHARD_COUNT:02x}')
    shard = json.loads(bundle[entry['path']])
    edit(shard)
    bundle[entry['path']] = encoded(shard)
    entry.update(bytes=len(bundle[entry['path']]), sha256=hashlib.sha256(bundle[entry['path']]).hexdigest())
    bundle[INDEX_PATH] = encoded(manifest)
    return bundle


class PublicReply:
    def __init__(self, url, body):
        self.url, self.body = url, body
        self.status, self.headers = 200, {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, size):
        return self.body[:size]
    def geturl(self):
        return self.url


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build_days(self, days, variant='Normal', price=lambda day: 100 + day):
        bundle = None
        for day in days:
            feed, bundle = build_history(feed_at(day, price(day), variant), bundle, START + timedelta(days=day))
        return feed, bundle

    def test_bootstrap_uses_real_generated_date_and_preserves_source_file_date(self):
        original = feed_at()
        feed, bundle = build_history(original, None, START + timedelta(hours=1))
        self.assertEqual(len(bundle), 65)
        self.assertEqual(feed['schemaVersion'], 1)
        self.assertNotIn('history', original['prices'][0])
        self.assertEqual(feed['prices'][0]['history'], {'observedAt': original['generatedAt'], 'days7': 1, 'days30': 1, 'avg7': None, 'avg30': None})
        row = series_from_bundle(bundle)[(46466, 'Normal')]['observations'][0]
        self.assertEqual(row, {'observedAt': original['generatedAt'], 'marketPrice': 100, 'sourcePublishedAt': original['prices'][0]['sourcePublishedAt']})
        self.assertEqual(validate_feed(original, START), original)  # Legacy schema remains valid.
        self.assertEqual(validate_feed(feed, START), feed)
        legacy_projection = deepcopy(feed)
        for row in legacy_projection['prices']:
            row.pop('history', None)
        self.assertEqual(legacy_projection, original)  # Existing clients ignore only the additive field.

    def test_complete_windows_average_only_exact_variant_and_consecutive_days(self):
        feed, bundle = self.build_days(range(30))
        history = feed['prices'][0]['history']
        self.assertEqual(history['days7'], 7)
        self.assertEqual(history['days30'], 30)
        self.assertAlmostEqual(history['avg7'], 126)
        self.assertAlmostEqual(history['avg30'], 114.5)
        new_feed = feed_at(30, 999, 'Reverse Holofoil')
        enriched, _ = build_history(new_feed, bundle, START + timedelta(days=30))
        self.assertEqual(enriched['prices'][0]['history']['days30'], 1)
        self.assertIsNone(enriched['prices'][0]['history']['avg30'])

    def test_missing_days_never_receive_synthetic_prices_or_averages(self):
        feed, bundle = self.build_days([0, 1, 2, 4, 5, 6])
        self.assertEqual(feed['prices'][0]['history']['days7'], 6)
        self.assertIsNone(feed['prices'][0]['history']['avg7'])
        self.assertEqual(len(series_from_bundle(bundle)[(46466, 'Normal')]['observations']), 6)

    def test_retention_is_35_calendar_days_and_drops_inactive_series(self):
        feed, bundle = self.build_days(range(40))
        observations = series_from_bundle(bundle)[(46466, 'Normal')]['observations']
        self.assertEqual(len(observations), 35)
        self.assertEqual(observations[0]['observedAt'], feed_at(5)['generatedAt'])
        replacement, bundle = build_history(feed_at(80, 42, 'Holofoil'), bundle, START + timedelta(days=80))
        self.assertEqual(set(series_from_bundle(bundle)), {(46466, 'Holofoil')})
        self.assertEqual(replacement['prices'][0]['history']['days30'], 1)

    def test_new_build_same_utc_day_replaces_one_observation_only(self):
        _, bundle = self.build_days([0])
        original = feed_at(0, 120, hours=1)
        feed, bundle = build_history(original, bundle, START + timedelta(hours=1))
        observations = series_from_bundle(bundle)[(46466, 'Normal')]['observations']
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]['marketPrice'], 120)
        self.assertEqual(feed['prices'][0]['history']['observedAt'], '2026-10-01T22:00:00Z')
        _, bundle = build_history(feed_at(0, 130, hours=4), bundle, START + timedelta(hours=4))
        self.assertEqual(len(series_from_bundle(bundle)[(46466, 'Normal')]['observations']), 2)  # Crossed UTC midnight.

    def test_same_build_is_byte_identical_without_inventing_next_day(self):
        feed, bundle = self.build_days([0])
        feed_again, bundle_again = build_history(feed_at(), bundle, START + timedelta(days=1))
        self.assertEqual(feed_again, feed)
        self.assertEqual(bundle_again, bundle)
        altered = feed_at(price=999)
        with self.assertRaises(FeedError):
            build_history(altered, bundle, START + timedelta(days=1))
        altered = feed_at()
        altered['generatedAt'] = iso(START + timedelta(hours=1))
        with self.assertRaisesRegex(FeedError, 'preserve observedAt'):
            build_history(altered, bundle, START + timedelta(days=1))

    def test_build_observation_and_publication_rollbacks_fail(self):
        _, bundle = self.build_days([0, 1])
        with self.assertRaisesRegex(FeedError, 'rollback'):
            build_history(feed_at(0), bundle, START + timedelta(days=2))
        regression = feed_at(2)
        regression['prices'][0]['sourcePublishedAt'] = feed_at(0)['prices'][0]['sourcePublishedAt']
        with self.assertRaisesRegex(FeedError, 'rolled back'):
            build_history(regression, bundle, START + timedelta(days=2))

    def test_duplicate_days_series_and_input_prices_fail(self):
        feed, bundle = self.build_days([0])
        edits = [lambda s: s['series'][0]['observations'].append(deepcopy(s['series'][0]['observations'][0])),
                 lambda s: s['series'].append(deepcopy(s['series'][0]))]
        for edit in edits:
            with self.assertRaises(FeedError):
                validate_bundle(replace_shard(bundle, edit), START)
        original = feed_at()
        original['prices'].append(deepcopy(original['prices'][0]))
        with self.assertRaisesRegex(FeedError, 'Duplicate current'):
            build_history(original, None, START)

    def test_bundle_rejects_corruption_wrong_summary_or_latest_price(self):
        feed, bundle = self.build_days([0])
        broken = deepcopy(bundle)
        broken['/history/00.json'] += b' '
        with self.assertRaisesRegex(FeedError, 'hash or size'):
            validate_bundle(broken, START)
        for change in [lambda f: f['prices'][0].update(marketPrice=7),
                       lambda f: f['prices'][0]['history'].update(days7=2)]:
            wrong = deepcopy(feed)
            change(wrong)
            with self.assertRaises(FeedError):
                validate_bundle(bundle, START, wrong)

    def test_restore_without_actions_cache_and_reuse_hash_verified_cache(self):
        feed, bundle = self.build_days([0])
        calls = []
        def opener(request, timeout):
            self.assertEqual(timeout, 20)
            calls.append(request.full_url)
            return PublicReply(request.full_url, bundle[request.full_url.removeprefix('https://pokeinvest-market-data.web.app')])
        cache = self.root / 'cache'
        restored = load_previous_history(URL, START, cache, opener=opener)
        self.assertEqual(restored, bundle)
        self.assertEqual(len(calls), 65)
        calls.clear()
        self.assertEqual(load_previous_history(URL, START, cache, opener=opener), bundle)
        self.assertEqual(calls, [URL])
        enriched, next_bundle = build_history(feed_at(1), restored, START + timedelta(days=1))
        self.assertEqual(enriched['prices'][0]['history']['days7'], 2)

    def test_404_requires_explicit_bootstrap_and_cannot_reset_known_history(self):
        def missing(request, timeout):
            raise HTTPError(request.full_url, 404, 'missing', {}, io.BytesIO())
        with self.assertRaises(FeedError):
            load_previous_history(URL, START, opener=missing)
        self.assertIsNone(load_previous_history(URL, START, allow_bootstrap=True, opener=missing))
        cache = self.root / 'cache'
        cache.mkdir()
        (cache / 'index.json').write_text('{}')
        with self.assertRaises(FeedError):
            load_previous_history(URL, START, cache, allow_bootstrap=True, opener=missing)

    def test_remote_failure_corrupt_shard_or_wrong_host_never_restarts_history(self):
        _, bundle = self.build_days([0])
        for failure in ('network', 'corrupt', 'missing'):
            def opener(request, timeout):
                if request.full_url == URL:
                    return PublicReply(request.full_url, bundle[INDEX_PATH])
                if failure == 'network':
                    raise URLError('offline')
                if failure == 'missing':
                    raise HTTPError(request.full_url, 404, 'missing', {}, io.BytesIO())
                return PublicReply(request.full_url, b'{}')
            with self.assertRaises(FeedError):
                load_previous_history(URL, START, allow_bootstrap=True, opener=opener)
        with self.assertRaises(FeedError):
            load_previous_history('https://evil.test/history/index.json', START)

    def test_prepared_bundle_deploys_as_one_complete_version_and_partial_is_rejected(self):
        feed, bundle = self.build_days([0])
        public = self.root / 'public'
        write_public(public, feed_at())
        write_history_public(public, feed, bundle, START)
        bodies, _, hashes, _ = prepare_public(public, START)
        self.assertEqual(len(bodies), 67)
        self.assertEqual(set(hashes), set(bundle) | {'/feed.json', '/index.html'})
        # The next ingestion can prepare its feed before the history step safely.
        write_public(public, feed_at())
        with self.assertRaises(DeploymentError):
            prepare_public(public, START)
        write_history_public(public, feed, bundle, START)
        original = (public / 'feed.json').read_bytes()
        corrupt = deepcopy(bundle)
        corrupt['/history/00.json'] = b'{}'
        with self.assertRaises(FeedError):
            write_history_public(public, feed, corrupt, START)
        self.assertEqual((public / 'feed.json').read_bytes(), original)
        (public / 'history' / '00.json').unlink()
        with self.assertRaises(DeploymentError):
            prepare_public(public, START)

    def test_feed_summary_size_limit_is_checked_before_output(self):
        from unittest.mock import patch
        with patch('history_feed.MAX_FEED_BYTES', 100), self.assertRaisesRegex(FeedError, '16 MiB'):
            build_history(feed_at(), None, START)

    def test_partial_averages_or_null_summary_are_not_accepted(self):
        feed, _ = self.build_days([0])
        for value in [None, {'observedAt': feed['generatedAt'], 'days7': 1, 'days30': 1, 'avg7': 100, 'avg30': None}]:
            candidate = deepcopy(feed)
            candidate['prices'][0]['history'] = value
            with self.assertRaises(FeedError):
                validate_feed(candidate, START)


if __name__ == '__main__':
    unittest.main()
