from copy import deepcopy
from datetime import timedelta
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from market_feed import FeedError, HttpClient, Ingestor, Response, USER_AGENT, catalog, fresh, instant, iso, load_previous, publication_date, validate_feed, validate_groups, write_public
NOW = instant('2026-10-30T14:47:00Z')
BUILD = '2026-10-29T20:06:01+0000'
GROUP = {'groupId': 648, 'name': 'Southern Islands', 'categoryId': 3, 'modifiedOn': '2026-07-11T00:04:03.753'}
FIXTURES = Path(__file__).parent / 'fixtures'

def fixture(kind):
    value = json.loads((FIXTURES / f'{kind}.json').read_text())
    return Response(200, {k.lower(): v for k, v in value['headers'].items()}, json.dumps(value['data']).encode())

def collection(rows):
    return json.dumps({'success': True, 'errors': [], 'results': rows}).encode()

class FakeClient:

    def __init__(self, overrides=None):
        self.responses = {'/last-updated.txt': Response(200, {}, BUILD.encode()), '/tcgplayer/3/groups': Response(200, {}, collection([GROUP])), '/tcgplayer/3/648/prices': fixture('prices'), '/tcgplayer/3/648/products': fixture('products')}
        self.responses.update(overrides or {})
        self.calls = []
        self.requests = 0

    def get(self, path, cache=None):
        self.calls.append((path, deepcopy(cache)))
        self.requests += 1
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

class IngestTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / '.state'
        self.output = self.root / 'public'

    def ingest(self, client=None, now=NOW):
        client = client or FakeClient()
        return (Ingestor(client, self.state, self.output, now), client)

    def test_real_public_fixture_matches_mew(self):
        ingestor, client = self.ingest()
        result = ingestor.run()
        feed = json.loads((self.output / 'feed.json').read_text())
        mew = next((row for row in feed['prices'] if row['productId'] == 46466))
        self.assertEqual(mew, {'productId': 46466, 'subTypeName': 'Reverse Holofoil', 'marketPrice': 649.13, 'sourcePublishedAt': '2026-10-29T20:03:47Z'})
        self.assertEqual(feed['sourceBuildAt'], '2026-10-29T20:06:01Z')
        self.assertTrue(result['changed'])
        self.assertEqual(client.requests, 4)
        self.assertEqual({p.name for p in self.output.iterdir()}, {'index.html', 'feed.json'})
        self.assertNotIn('updatedAt', mew)

    def test_same_build_only_checks_marker_preserving_timestamps(self):
        self.ingest()[0].run()
        old = (self.output / 'feed.json').read_bytes()
        ingestor, client = self.ingest(now=NOW + timedelta(hours=1))
        result = ingestor.run()
        self.assertFalse(result['changed'])
        self.assertEqual(client.requests, 1)
        self.assertEqual(old, (self.output / 'feed.json').read_bytes())

    def test_same_build_revalidates_publication_age_without_refreshing_it(self):
        self.ingest()[0].run()
        original = (self.output / 'feed.json').read_bytes()
        # A source file may cross the age threshold before its later build marker.
        now = instant('2026-11-01T20:04:00Z')
        ingestor, client = self.ingest(now=now)
        with self.assertRaisesRegex(FeedError, 'source files older than 72 hours'):
            ingestor.run()
        self.assertEqual(client.requests, 1)
        self.assertEqual(original, (self.output / 'feed.json').read_bytes())

    def test_old_future_or_timezone_less_build_stops_before_groups(self):
        for marker in ['2026-10-20T00:00:00Z', '2026-11-01T00:00:00Z', '2026-10-29T20:06:01']:
            with self.subTest(marker=marker):
                client = FakeClient({'/last-updated.txt': Response(200, {}, marker.encode())})
                ingestor, _ = self.ingest(client)
                with self.assertRaises(FeedError):
                    ingestor.run()
                self.assertEqual(client.requests, 1)
                self.assertFalse((self.output / 'feed.json').exists())

    def test_missing_future_or_old_publication_never_uses_http_date(self):
        for i, stamp in enumerate([None, 'bad', 'Fri, 30 Oct 2026 22:00:00 GMT', 'Mon, 26 Oct 2026 20:03:47 GMT']):
            with self.subTest(stamp=stamp):
                response = fixture('prices')
                response.headers = {'date': 'Fri, 30 Oct 2026 14:47:00 GMT'}
                if stamp is not None:
                    response.headers['last-modified'] = stamp
                ingestor = Ingestor(FakeClient({'/tcgplayer/3/648/prices': response}), self.state / str(i), self.output, NOW)
                with self.assertRaises(FeedError):
                    ingestor.run()
                self.assertFalse((self.output / 'feed.json').exists())
                self.assertNotIn('/tcgplayer/3/648/products', [url for url, _ in ingestor.client.calls])

    def test_source_build_in_progress_fails_complete_publish(self):
        response = fixture('prices')
        response.headers['last-modified'] = 'Thu, 29 Oct 2026 21:03:47 GMT'
        with self.assertRaisesRegex(FeedError, 'build may be in progress'):
            self.ingest(FakeClient({'/tcgplayer/3/648/prices': response}))[0].run()
        self.assertFalse((self.output / 'feed.json').exists())

    def test_later_group_http_failure_preserves_output_and_prevents_repeat(self):
        self.ingest()[0].run()
        old = (self.output / 'feed.json').read_bytes()
        previous = json.loads((self.state / 'state.json').read_text())
        groups = [GROUP, {**GROUP, 'groupId': 649, 'name': 'Another set'}]
        client = FakeClient({'/last-updated.txt': Response(200, {}, b'2026-10-30T14:00:00Z'), '/tcgplayer/3/groups': Response(200, {}, collection(groups)), '/tcgplayer/3/649/prices': FeedError('HTTP 503')})
        with self.assertRaisesRegex(FeedError, 'HTTP 503'):
            self.ingest(client)[0].run()
        self.assertEqual(old, (self.output / 'feed.json').read_bytes())
        state = json.loads((self.state / 'state.json').read_text())
        self.assertEqual(state['lastFeed'], previous['lastFeed'])
        self.assertEqual(state['lastSuccessfulBuildAt'], previous['lastSuccessfulBuildAt'])
        retry = FakeClient({'/last-updated.txt': Response(200, {}, b'2026-10-30T14:00:00Z')})
        with self.assertRaisesRegex(FeedError, 'already failed'):
            self.ingest(retry)[0].run()
        self.assertEqual(retry.requests, 1)

    def test_304_reuses_original_publication_and_identities(self):
        self.ingest()[0].run()
        client = FakeClient({'/last-updated.txt': Response(200, {}, b'2026-10-30T14:00:00Z'), '/tcgplayer/3/648/prices': Response(304, {'last-modified': 'Fri, 30 Oct 2026 14:30:00 GMT'}, b'')})
        self.ingest(client)[0].run()
        self.assertNotIn('/tcgplayer/3/648/products', [url for url, _ in client.calls])
        self.assertEqual(client.calls[-1][1]['lastModified'], 'Thu, 29 Oct 2026 20:03:47 GMT')
        feed = json.loads((self.output / 'feed.json').read_text())
        self.assertEqual({row['sourcePublishedAt'] for row in feed['prices']}, {'2026-10-29T20:03:47Z'})

    def test_invalid_rows_and_unknown_products_are_filtered(self):
        response = fixture('prices')
        data = json.loads(response.body)
        data['results'] += [{'productId': 46466, 'subTypeName': 'Unknown Finish', 'marketPrice': 10}, {'productId': 46466, 'subTypeName': 'Normal', 'marketPrice': 0}, {'productId': 46466, 'subTypeName': 'Holofoil', 'marketPrice': '10'}, {'productId': 999999999, 'subTypeName': 'Normal', 'marketPrice': 5}]
        response.body = json.dumps(data).encode()
        ingestor, _ = self.ingest(FakeClient({'/tcgplayer/3/648/prices': response}))
        ingestor.run()
        feed = json.loads((self.output / 'feed.json').read_text())
        self.assertEqual(len([r for r in feed['prices'] if r['productId'] == 46466]), 1)
        self.assertEqual(ingestor.stats['invalidOrUnsupportedPriceRows'], 3)
        self.assertGreaterEqual(ingestor.stats['nonSingleOrUnknownProductRows'], 1)

    def test_duplicate_conflicting_prices_abort(self):
        response = fixture('prices')
        data = json.loads(response.body)
        original = next((r for r in data['results'] if r['productId'] == 46466))
        data['results'].append({**original, 'marketPrice': 100})
        response.body = json.dumps(data).encode()
        with self.assertRaisesRegex(FeedError, 'Conflicting duplicate'):
            self.ingest(FakeClient({'/tcgplayer/3/648/prices': response}))[0].run()
        self.assertFalse((self.output / 'feed.json').exists())

    def test_product_category_mismatch_aborts(self):
        response = fixture('products')
        data = json.loads(response.body)
        data['results'][0]['categoryId'] = 1
        response.body = json.dumps(data).encode()
        with self.assertRaisesRegex(FeedError, 'identity mismatch'):
            self.ingest(FakeClient({'/tcgplayer/3/648/products': response}))[0].run()

    def test_remote_previous_same_build_skips_groups(self):
        self.ingest()[0].run()
        previous = json.loads((self.output / 'feed.json').read_text())
        other = self.root / 'other'
        client = FakeClient()
        with patch('market_feed.load_previous', return_value=previous):
            result = Ingestor(client, other / 'state', other / 'public', NOW).run('https://test.web.app/feed.json')
        self.assertFalse(result['changed'])
        self.assertEqual(client.requests, 1)
        self.assertEqual(previous, json.loads((other / 'public' / 'feed.json').read_text()))

    def test_oversized_feed_keeps_previous_output(self):
        self.ingest()[0].run()
        prior = (self.output / 'feed.json').read_bytes()
        feed = json.loads(prior)
        with patch('market_feed.MAX_FEED_BYTES', len(prior) - 1):
            with self.assertRaisesRegex(FeedError, 'exceeds Android limits'):
                write_public(self.output, feed)
        self.assertEqual(prior, (self.output / 'feed.json').read_bytes())
        with patch('market_feed.MAX_FEED_BYTES', len(prior)):
            write_public(self.output, feed)
        self.assertEqual(prior, (self.output / 'feed.json').read_bytes())

    def test_feed_over_100000_prices_is_rejected_before_output(self):
        feed = {'schemaVersion': 1, 'currency': 'USD', 'sourceBuildAt': '2026-10-29T20:06:01Z',
                'generatedAt': iso(NOW), 'prices': [{}] * 100001}
        with self.assertRaisesRegex(FeedError, 'Invalid previous feed prices'):
            validate_feed(feed, NOW)
        with self.assertRaisesRegex(FeedError, 'exceeds Android limits'):
            write_public(self.output, feed)
        self.assertFalse((self.output / 'feed.json').exists())

    def test_unexpected_public_files_block_preparation(self):
        self.output.mkdir()
        (self.output / 'secret.txt').write_text('not for deployment')
        with self.assertRaisesRegex(FeedError, 'unexpected files'):
            self.ingest()[0].run()
        self.assertFalse((self.output / 'feed.json').exists())

class ParserTests(unittest.TestCase):

    def test_timezone_and_age_boundaries(self):
        self.assertEqual(instant(BUILD), instant('2026-10-29T20:06:01Z'))
        with self.assertRaises(FeedError):
            instant('2026-10-29T20:06:01')
        self.assertIsNone(publication_date('bad'))
        self.assertFalse(fresh(NOW + timedelta(seconds=1), NOW))
        self.assertTrue(fresh(NOW - timedelta(hours=72), NOW))
        self.assertFalse(fresh(NOW - timedelta(hours=72, seconds=1), NOW))

    def test_groups_require_pokemon_and_unique_identity(self):
        for rows in [[{**GROUP, 'categoryId': 1}], [{**GROUP, 'groupId': True}], [GROUP, {**GROUP, 'name': 'different'}], []]:
            with self.assertRaises(FeedError):
                validate_groups(rows)

    def test_single_detection_requires_number_excludes_sealed_codes(self):
        rows = json.loads(fixture('products').body)['results']
        selected = catalog(rows, 648)
        self.assertIn(46466, selected['singleIds'])
        self.assertLess(len(selected['singleIds']), len(selected['allIds']))
        product = deepcopy(rows[0])
        product['extendedData'] = [{'name': 'Number', 'value': '001'}, {'name': 'Rarity', 'value': 'Code Card'}]
        self.assertEqual(catalog([product], 648)['singleIds'], [])

    def test_previous_feed_rejects_invalid_future_conflicting_values(self):
        base = {'schemaVersion': 1, 'currency': 'USD', 'sourceBuildAt': '2026-10-29T20:06:01Z', 'generatedAt': iso(NOW), 'prices': [{'productId': 46466, 'subTypeName': 'Reverse Holofoil', 'marketPrice': 649.13, 'sourcePublishedAt': '2026-10-29T20:03:47Z'}]}
        for price in [float('nan'), float('inf'), -1, 0, True, '12']:
            feed = deepcopy(base)
            feed['prices'][0]['marketPrice'] = price
            with self.assertRaises(FeedError):
                validate_feed(feed, NOW)
        feed = deepcopy(base)
        feed['prices'][0]['sourcePublishedAt'] = '2026-10-30T20:00:00Z'
        with self.assertRaises(FeedError):
            validate_feed(feed, NOW)
        feed = deepcopy(base)
        feed['prices'].append({**feed['prices'][0], 'marketPrice': 3})
        with self.assertRaises(FeedError):
            validate_feed(feed, NOW)

class NetworkTests(unittest.TestCase):

    def response(self, body=b'{}', headers=None):
        handle = io.BytesIO(body)
        handle.status = 200
        handle.headers = headers or {}
        handle.geturl = lambda: 'https://tcgcsv.com/last-updated.txt'
        return handle

    def test_custom_useragent_timeout_spacing_conditional_headers(self):
        calls = []
        sleeps = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return self.response()
        client = HttpClient(opener=opener, sleep=sleeps.append, monotonic=lambda: 0)
        client.get('/last-updated.txt')
        client.get('/tcgplayer/3/groups', {'lastModified': 'Thu, 29 Oct 2026 20:00:00 GMT', 'etag': '"a"'})
        self.assertEqual(calls[0][0].get_header('User-agent'), USER_AGENT)
        self.assertEqual(calls[0][1], 20)
        self.assertEqual(sleeps, [0.11])
        self.assertEqual(calls[1][0].get_header('If-modified-since'), 'Thu, 29 Oct 2026 20:00:00 GMT')
        self.assertEqual(calls[1][0].get_header('If-none-match'), '"a"')

    def test_429_and_network_failures_never_retried(self):
        for error in [HTTPError('https://tcgcsv.com', 429, 'rate limited', Message(), None), URLError('offline')]:
            with patch('market_feed.urlopen', side_effect=error) as opened:
                with self.assertRaises(FeedError):
                    HttpClient(opener=opened).get('/last-updated.txt')
                self.assertEqual(opened.call_count, 1)

    def test_304_requires_cache_and_never_invents_date(self):

        def opener(request, timeout):
            raise HTTPError(request.full_url, 304, 'Not modified', Message(), None)
        client = HttpClient(opener=opener, sleep=lambda n: None)
        response = client.get('/tcgplayer/3/648/prices', {'lastModified': 'Thu, 29 Oct 2026 20:03:47 GMT'})
        self.assertEqual(response.status, 304)
        self.assertIsNone(response.last_modified)
        with self.assertRaises(FeedError):
            client.get('/last-updated.txt')

    def test_allowlists_prevent_unexpected_network_destinations(self):
        client = HttpClient(opener=lambda *a, **k: self.fail('No network expected'))
        for path in ['/tcgplayer/1/groups', 'https://example.com', '/tcgplayer/3/../prices']:
            with self.assertRaises(FeedError):
                client.get(path)
        for url in ['https://example.com/feed.json', 'http://x.web.app/feed.json', 'https://x.web.app/feed.json?key=secret']:
            with self.assertRaises(FeedError):
                load_previous(url, NOW)
if __name__ == '__main__':
    unittest.main()
