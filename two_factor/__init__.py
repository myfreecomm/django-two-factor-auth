# -*- coding: utf-8 -*-
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

module_name = 'two_factor.%s' % getattr(settings, 'PERSISTENCE_STRATEGY', 'django_db')

try:
    PERSISTENCE_MODULE = __import__(
        module_name,
        fromlist=['two_factor']
    )
except ImportError:
    raise ImproperlyConfigured(
        'settings.PERSISTENCE_MODULE: %s could not be imported' % module_name
    )

__all__ = ['PERSISTENCE_MODULE', ]
