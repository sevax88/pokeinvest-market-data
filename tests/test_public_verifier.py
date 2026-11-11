from copy import deepcopy
from email.message import Message
from io import BytesIO
import hashlib
import json
import unittest
from unittest.mock import Mock, call
from urllib.response import addinfourl

from market_feed import FeedError, instant
from verify_public_feed import RETRY_DELAYS, verify_body, verify_public_url


PUBLIC_URL = 'https://pokeinvest-market-data.web.app/feed.json'


def response(body, content_type='application/json'):
    headers = Message()
    headers['Content-Type'] = content_type
    return addinfourl(BytesIO(body), headers, PUBLIC_URL, code=200)

class PublicVerifierTests(unittest.TestCase):
    def setUp(self):
        self.now = instant('2026-10-30T16:38:00Z')
        self.feed = {'schemaVersion': 1, 'currency': 'USD', 'sourceBuildAt': '2026-10-29T20:06:01Z', 'generatedAt': '2026-10-30T15:20:33Z', 'prices': [{'productId': 46466, 'subTypeName': 'Reverse Holofoil', 'marketPrice': 649.13, 'sourcePublishedAt': '2026-10-29T20:03:47Z'}]}

    def test_expected_file_identity(self):
        body = json.dumps(self.feed).encode()
        self.assertTrue(verify_body(body, self.now, body)['expectedInitialFileMatches'])
        with self.assertRaisesRegex(FeedError, 'does not match'):
            verify_body(body, self.now, b'other file')

    def test_recent_build_does_not_hide_old_source_file(self):
        feed = deepcopy(self.feed)
        feed['prices'][0]['sourcePublishedAt'] = '2026-10-20T20:03:47Z'
        with self.assertRaisesRegex(FeedError, 'older than 72 hours'):
            verify_body(json.dumps(feed).encode(), self.now)

    def test_html_fallback_and_wrong_currency_are_rejected(self):
        with self.assertRaises(FeedError):
            verify_body(b'<!doctype html><title>Welcome to Firebase</title>', self.now)
        feed = deepcopy(self.feed)
        feed['currency'] = 'EUR'
        with self.assertRaises(FeedError):
            verify_body(json.dumps(feed).encode(), self.now)

    def test_older_cached_price_file_then_expected_release(self):
        previous = deepcopy(self.feed)
        previous['sourceBuildAt'] = '2026-10-27T20:06:01Z'
        previous['generatedAt'] = '2026-10-28T01:20:33Z'
        previous['prices'][0]['sourcePublishedAt'] = '2026-10-27T09:03:47Z'
        expected = json.dumps(self.feed).encode()
        opener = Mock(side_effect=[response(json.dumps(previous).encode()), response(expected)])
        sleep = Mock()

        result = verify_public_url(PUBLIC_URL, expected, opener=opener, sleep=sleep, clock=lambda: self.now)

        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['sha256'], hashlib.sha256(expected).hexdigest())
        sleep.assert_called_once()
        for request_call in opener.call_args_list:
            (request,) = request_call.args
            self.assertEqual(request.full_url, PUBLIC_URL)
            self.assertEqual(request_call.kwargs['timeout'], 20)
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertIn('no-cache', headers['cache-control'])
            self.assertEqual(headers['accept-encoding'], 'identity')

    def test_permanent_mismatch_reports_expected_and_last_received_hash(self):
        expected = json.dumps(self.feed).encode()
        # A different serialization has the same prices, dates and schema.
        different_bytes = json.dumps(self.feed, indent=2).encode()
        opener = Mock(side_effect=lambda *args, **kwargs: response(different_bytes))
        sleep = Mock()

        with self.assertRaises(FeedError) as error:
            verify_public_url(PUBLIC_URL, expected, opener=opener, sleep=sleep, clock=lambda: self.now)

        self.assertEqual(opener.call_count, len(RETRY_DELAYS) + 1)
        self.assertEqual(sleep.call_args_list, [call(delay) for delay in RETRY_DELAYS])
        self.assertGreaterEqual(sum(RETRY_DELAYS), 300)
        self.assertIn(hashlib.sha256(expected).hexdigest(), str(error.exception))
        self.assertIn(hashlib.sha256(different_bytes).hexdigest(), str(error.exception))

    def test_matching_expired_price_is_rejected_without_waiting(self):
        expired = deepcopy(self.feed)
        expired['prices'][0]['sourcePublishedAt'] = '2026-10-20T20:03:47Z'
        body = json.dumps(expired).encode()
        opener = Mock(return_value=response(body))
        sleep = Mock()

        with self.assertRaisesRegex(FeedError, 'older than 72 hours'):
            verify_public_url(PUBLIC_URL, body, opener=opener, sleep=sleep, clock=lambda: self.now)

        sleep.assert_not_called()
        self.assertLessEqual(opener.call_count, 1)

    def test_html_content_type_does_not_trigger_a_digest_retry(self):
        expected = json.dumps(self.feed).encode()
        opener = Mock(return_value=response(b'<html>Hosting fallback</html>', 'text/html'))
        sleep = Mock()

        with self.assertRaises(FeedError):
            verify_public_url(PUBLIC_URL, expected, opener=opener, sleep=sleep, clock=lambda: self.now)

        opener.assert_called_once()
        sleep.assert_not_called()

    def test_read_without_an_expected_file_checks_freshness_in_one_request(self):
        body = json.dumps(self.feed).encode()
        opener = Mock(return_value=response(body))
        sleep = Mock()

        result = verify_public_url(PUBLIC_URL, opener=opener, sleep=sleep, clock=lambda: self.now)

        self.assertFalse(result['expectedInitialFileMatches'])
        self.assertEqual(result['attempts'], 1)
        opener.assert_called_once()
        sleep.assert_not_called()
