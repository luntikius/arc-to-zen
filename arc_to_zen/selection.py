"""Selection shared by CLI, wizard, scan and transfer. No secret access here."""
from dataclasses import dataclass
from .common import MigrationError, pairs, profile_key
from .source import discover, arc_container

CATEGORIES = {
    'tabs': 'Spaces, tabs & folders', 'themes': 'Space themes', 'icons': 'Space & folder icons',
    'history': 'Browsing history', 'archive': 'Archived tabs → history', 'passwords': 'Passwords',
    'cookies': 'Cookies & sign-ins', 'bookmarks': 'Bookmarks', 'forms': 'Autofill',
    'addresses': 'Addresses', 'cards': 'Payment cards',
}
DEPENDENCIES = {'themes': 'tabs', 'icons': 'tabs', 'archive': 'history'}


def catalog(root):
    profiles = discover(root)
    spaces = []
    for key, s in pairs(arc_container(root)['spaces']):
        owner = profile_key(s['profile'])
        if s.get('id') != key or owner not in profiles:
            raise MigrationError('Unknown Space identity or profile.')
        spaces.append({'id': key, 'profile': owner, 'name': s.get('title') or 'Arc'})
    return {'profiles': [{'id': p.key, 'name': p.name} for p in profiles.values()], 'spaces': spaces}


@dataclass(frozen=True)
class Selection:
    profiles: frozenset
    spaces: frozenset
    categories: frozenset

    @classmethod
    def from_args(cls, args, data):
        profiles = frozenset(getattr(args, 'profiles', None) or [p['id'] for p in data['profiles']])
        spaces = getattr(args, 'spaces', None)
        if spaces is None:
            spaces = [s['id'] for s in data['spaces'] if s['profile'] in profiles]
        elif spaces == ['none']:
            spaces = []
        categories = frozenset(getattr(args, 'categories', None) or CATEGORIES)
        result = cls(profiles, frozenset(spaces), categories)
        result.validate(data)
        return result

    def validate(self, data):
        owners = {s['id']: s['profile'] for s in data['spaces']}
        if not self.profiles or not self.profiles <= {p['id'] for p in data['profiles']}:
            raise MigrationError('Select at least one existing profile.')
        if not self.spaces <= owners.keys() or any(owners[s] not in self.profiles for s in self.spaces):
            raise MigrationError('Selected Spaces must belong to selected profiles.')
        if not self.categories or not self.categories <= CATEGORIES.keys():
            raise MigrationError('Select at least one supported data category.')
        for child, parent in DEPENDENCIES.items():
            if child in self.categories and parent not in self.categories:
                raise MigrationError(f'{CATEGORIES[child]} requires {CATEGORIES[parent]}.')

    def document(self):
        return {k: sorted(getattr(self, k)) for k in ('profiles', 'spaces', 'categories')}
