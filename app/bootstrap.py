"""Explicit local first-administrator creation; no default credentials."""
import argparse
import getpass
import sys
import time

from .enterprise_auth import make_user, normalize_username, public_profile, valid_display, valid_password
from .storage_v2 import V2Store


def bootstrap_admin(store, username, display_name, password):
    username = normalize_username(username)
    if not username or not valid_display(display_name) or not valid_password(password):
        raise ValueError('Invalid account fields')
    user = make_user(username, display_name, password, 'admin', int(time.time()))
    def add(document):
        if any(u['role'] == 'admin' for u in document['users']):
            raise ValueError('An administrator already exists')
        if any(u['username'] == username for u in document['users']):
            raise ValueError('Username unavailable')
        document['users'].append(user)
        return public_profile(user)
    return store.transact(add)


def main():
    parser = argparse.ArgumentParser(description='Create the first administrator in an existing v2 store.')
    parser.add_argument('--store', required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        parser.error('An interactive terminal is required; credentials cannot be piped.')
    username = input('Username: ')
    display_name = input('Display name: ')
    password = getpass.getpass('Password: ')
    confirmation = getpass.getpass('Confirm password: ')
    if password != confirmation:
        parser.exit(1, 'Passwords do not match.\n')
    try:
        bootstrap_admin(V2Store(args.store), username, display_name, password)
    except (ValueError, OSError):
        parser.exit(1, 'Administrator not created. Check the store and account policy.\n')
    print('Administrator created.')


if __name__ == '__main__':
    main()
