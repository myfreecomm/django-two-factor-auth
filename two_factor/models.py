# -*- coding: utf-8 -*-
import logging

from django.conf import settings
from django.utils.translation import ugettext_lazy as _

from two_factor import PERSISTENCE_MODULE

__all__ = [
    'PhoneDevice', 'phone_number_validator',
    'get_available_methods', 'logger'
]

try:
    import yubiotp
except ImportError:
    yubiotp = None


def get_available_phone_methods():
    methods = []
    if getattr(settings, 'TWO_FACTOR_CALL_GATEWAY', None):
        methods.append(('call', _('Phone call')))
    if getattr(settings, 'TWO_FACTOR_SMS_GATEWAY', None):
        methods.append(('sms', _('Text message')))
    return methods


def get_available_yubikey_methods():
    methods = []
    if yubiotp and 'otp_yubikey' in settings.INSTALLED_APPS:
        methods.append(('yubikey', _('YubiKey')))
    return methods


def get_available_methods():
    methods = [('generator', _('Token generator'))]
    methods.extend(get_available_phone_methods())
    methods.extend(get_available_yubikey_methods())
    return methods


logger = logging.getLogger(__name__)


PhoneDevice = PERSISTENCE_MODULE.models.PhoneDevice
phone_number_validator = PERSISTENCE_MODULE.models.phone_number_validator
