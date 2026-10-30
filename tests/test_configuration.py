import unittest
from configure_hosting import validate_environment

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            'FIREBASE_PROJECT': 'pokeinvest-23904',
            'FIREBASE_SITE': 'pokeinvest-market-data',
            'WIF_PROVIDER': 'projects/123456789/locations/global/workloadIdentityPools/feed/providers/github',
            'WIF_SERVICE_ACCOUNT': 'feed-deploy@pokeinvest-23904.iam.gserviceaccount.com',
        }

    def test_dedicated_destination(self):
        self.assertEqual(validate_environment(self.env), 'pokeinvest-market-data')

    def test_default_app_site_is_rejected(self):
        self.env['FIREBASE_SITE'] = 'pokeinvest-23904'
        with self.assertRaises(ValueError):
            validate_environment(self.env)

    def test_different_project_or_missing_identity_is_rejected(self):
        for field, value in [('FIREBASE_PROJECT', 'other-project'), ('WIF_PROVIDER', ''), ('WIF_SERVICE_ACCOUNT', 'wrong@other.iam.gserviceaccount.com')]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_environment({**self.env, field: value})
