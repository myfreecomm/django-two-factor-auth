# -*- coding: utf-8 -*-

from django.conf import settings as django_settings
from django.test import TestCase as SimpleTestCase

try:
    import mongoengine

except ImportError:
    import sys
    message = "ERROR: 'mongoengine' is required\n"
    sys.stdout.write(message.encode('utf-8'))
    sys.exit(1)

from . import models
from . import settings

__all__ = ['models', 'settings', 'TestCase', 'TestUser']


mongoengine.connect(
    django_settings.NOSQL_DATABASES['NAME'],
    host=django_settings.NOSQL_DATABASES['HOST']
)


class TestCase(SimpleTestCase):
    """
    TestCase class that clear the collection between the tests
    """
    def __init__(self, methodName='runtest'):
        self.db = mongoengine.connection._get_db()
        super(TestCase, self).__init__(methodName)

    def _post_teardown(self):
        super(TestCase, self)._post_teardown()
        for collection in self.db.collection_names():
            if collection == 'system.indexes':
                continue
            self.db.drop_collection(collection)

    def _fixture_teardown(self, *args, **kwargs):
        pass
