from copy import deepcopy
import json
import unittest
from market_feed import FeedError, instant
from verify_public_feed import verify_body

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
