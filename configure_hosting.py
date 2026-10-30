"""Resolve the dedicated Hosting site from validated workflow variables."""
import json
import os
from pathlib import Path
import re


def validate_environment(env):
    project = env.get('FIREBASE_PROJECT', '')
    site = env.get('FIREBASE_SITE', '')
    provider = env.get('WIF_PROVIDER', '')
    account = env.get('WIF_SERVICE_ACCOUNT', '')
    if project != 'pokeinvest-23904':
        raise ValueError('FIREBASE_PROJECT must be the existing pokeinvest-23904 project')
    if not re.fullmatch(r'pokeinvest-[a-z0-9-]{3,50}', site) or site == project:
        raise ValueError('FIREBASE_SITE must identify a separate, dedicated PokeInvest feed site')
    if not re.fullmatch(r'projects/[0-9]+/locations/global/workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+', provider):
        raise ValueError('WIF_PROVIDER is missing or malformed')
    if not re.fullmatch(r'[a-z0-9-]+@pokeinvest-23904\.iam\.gserviceaccount\.com', account):
        raise ValueError('WIF_SERVICE_ACCOUNT must belong to the selected project')
    return site


def main():
    site = validate_environment(os.environ)
    path = Path('firebase.json')
    config = json.loads(path.read_text())
    if set(config) != {'hosting'} or config['hosting'].get('public') != 'public':
        raise ValueError('Only the public feed Hosting configuration may be deployed')
    config['hosting']['site'] = site
    path.write_text(json.dumps(config, indent=2) + '\n')
    print('Dedicated Firebase Hosting configuration prepared.')


if __name__ == '__main__':
    main()
