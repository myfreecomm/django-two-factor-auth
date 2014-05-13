# -*- coding: utf-8 -*-
from binascii import unhexlify

from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import ugettext_lazy as _
from mongoengine import *

from django_otp import Device
from django_otp.oath import totp
from django_otp.util import hex_validator, random_hex

from two_factor.gateways import make_call, send_sms

__all__ = ['PhoneDevice', 'phone_number_validator']

phone_number_validator = RegexValidator(
    code='invalid-phone-number',
    regex='^(\+|00)',
    message=_('Please enter a valid phone number, including your country code '
              'starting with + or 00.'),
)

PHONE_METHODS = (
    ('call', _('Phone Call')),
    ('sms', _('Text Message')),
)

class PhoneDevice(Device):
    """
    Model with phone number and token seed linked to a user.
    """
    number = StringField(max_length=16, verbose_name=_('number'))
    key = StringField(max_length=40, default=lambda: random_hex(20), help_text="Hex-encoded secret key")
    method = StringField(max_length=4, choices=PHONE_METHODS, verbose_name=_('method'))

    meta = {
        'abstract': False,
    }

    def __repr__(self):
        return '<PhoneDevice(number={!r}, method={!r}>'.format(
            self.number,
            self.method,
        )

    def __eq__(self, other):
        if not isinstance(other, PhoneDevice):
            return False
        return self.number == other.number \
            and self.method == other.method \
            and self.key == other.key

    @property
    def bin_key(self):
        return unhexlify(self.key.encode())

    def verify_token(self, token):
        for drift in range(-5, 1):
            if totp(self.bin_key, drift=drift) == token:
                return True
        return False

    def generate_challenge(self):
        """
        Sends the current TOTP token to `self.number` using `self.method`.
        """
        token = '%06d' % totp(self.bin_key)
        if self.method == 'call':
            make_call(device=self, token=token)
        else:
            send_sms(device=self, token=token)

    def clean(self):
        try:
            phone_number_validator(self.number)
            hex_validator()(self.key)
        except DjangoValidationError as e:
            raise ValidationError(e)
