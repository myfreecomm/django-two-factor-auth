from binascii import unhexlify
import os

try:
    # Try StringIO first, as Python 2.7 also includes an unicode-strict
    # io.StringIO.
    from StringIO import StringIO
except ImportError:
    from io import StringIO

try:
    from urllib.parse import urlencode, urlparse, parse_qs
except ImportError:
    from urllib import urlencode
    from urlparse import urlparse, parse_qs

try:
    import unittest2 as unittest
except ImportError:
    import unittest

try:
    from unittest.mock import patch, Mock, ANY, call
except ImportError:
    from mock import patch, Mock, ANY, call

import django
from django import forms
from django.conf import settings
from django.core.management import call_command, CommandError
from django.core.urlresolvers import reverse
from django.test.utils import override_settings
from django.utils import translation, six

try:
    from django.contrib.auth import get_user_model
except ImportError:
    from django.contrib.auth.models import User
else:
    User = get_user_model()

from django_otp import DEVICE_ID_SESSION_KEY, devices_for_user
from django_otp.oath import totp
from django_otp.util import random_hex
from django_otp.plugins.otp_totp.models import TOTPDevice
from django_otp.plugins.otp_static.models import StaticDevice

try:
    from otp_yubikey.models import ValidationService, RemoteYubikeyDevice
except ImportError:
    ValidationService = RemoteYubikeyDevice = None

import qrcode.image.svg

from two_factor.admin import patch_admin, unpatch_admin
from two_factor.gateways.fake import Fake
from two_factor.gateways.twilio.gateway import Twilio
from two_factor.models import PhoneDevice, phone_number_validator
from two_factor.utils import backup_phones, default_device, get_otpauth_url

from two_factor import PERSISTENCE_MODULE


class UserMixin(object):
    def setUp(self):
        super(UserMixin, self).setUp()
        self._passwords = {}

    def create_user(self, username='bouke@example.com',
                    password='secret', **kwargs):
        user = User.objects.create_user(username=username, email=username,
                                        password=password, **kwargs)
        self._passwords[user] = password
        return user

    def create_superuser(self, username='bouke@example.com',
                         password='secret', **kwargs):
        user = User.objects.create_superuser(username=username, email=username,
                                             password=password, **kwargs)
        self._passwords[user] = password
        return user

    def login_user(self, user=None):
        if not user:
            user = list(self._passwords.keys())[0]
        try:
            username = user.get_username()
        except AttributeError:
            username = user.username
        assert self.client.login(username=username,
                                 password=self._passwords[user])
        if default_device(user):
            session = self.client.session
            session[DEVICE_ID_SESSION_KEY] = default_device(user).persistent_id
            session.save()

    def enable_otp(self, user=None):
        if not user:
            user = list(self._passwords.keys())[0]
        return TOTPDevice.objects.create(user=user, name='default')


class LoginTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def _post(self, data=None):
        return self.client.post(reverse('two_factor:login'), data=data)

    def test_form(self):
        response = self.client.get(reverse('two_factor:login'))
        self.assertContains(response, 'Password:')

    def test_invalid_login(self):
        response = self._post({'auth-username': 'unknown',
                               'auth-password': 'secret',
                               'login_view-current_step': 'auth'})
        self.assertContains(response, 'Please enter a correct')
        self.assertContains(response, 'and password.')

    @patch('two_factor.views.core.signals.user_verified.send')
    def test_valid_login(self, mock_signal):
        self.create_user()
        response = self._post({'auth-username': 'bouke@example.com',
                               'auth-password': 'secret',
                               'login_view-current_step': 'auth'})
        self.assertRedirects(response, str(settings.LOGIN_REDIRECT_URL))

        # No signal should be fired for non-verified user logins.
        self.assertFalse(mock_signal.called)

    def test_valid_login_with_custom_redirect(self):
        redirect_url = reverse('two_factor:setup')
        self.create_user()
        response = self.client.post(
            '%s?%s' % (reverse('two_factor:login'),
                       urlencode({'next': redirect_url})),
            {'auth-username': 'bouke@example.com',
             'auth-password': 'secret',
             'login_view-current_step': 'auth'})
        self.assertRedirects(response, redirect_url)

    def test_valid_login_with_redirect_field_name(self):
        redirect_url = reverse('two_factor:setup')
        self.create_user()
        response = self.client.post(
            '%s?%s' % (reverse('custom-login'),
                       urlencode({'next_page': redirect_url})),
            {'auth-username': 'bouke@example.com',
             'auth-password': 'secret',
             'login_view-current_step': 'auth'})
        self.assertRedirects(response, redirect_url)

    @patch('two_factor.views.core.signals.user_verified.send')
    def test_with_generator(self, mock_signal):
        user = self.create_user()
        device = TOTPDevice.objects.create(
            user=user, name='default', key=random_hex().decode()
        )

        response = self._post({'auth-username': 'bouke@example.com',
                               'auth-password': 'secret',
                               'login_view-current_step': 'auth'})
        self.assertContains(response, 'Token:')

        response = self._post({'token-otp_token': '123456',
                               'login_view-current_step': 'token'})
        self.assertEqual(response.context_data['wizard']['form'].errors,
                         {'__all__': ['Please enter your OTP token']})

        response = self._post({'token-otp_token': totp(device.bin_key),
                               'login_view-current_step': 'token'})
        self.assertRedirects(response, str(settings.LOGIN_REDIRECT_URL))

        self.assertEqual(device.persistent_id,
                         self.client.session.get(DEVICE_ID_SESSION_KEY))

        # Check that the signal was fired.
        mock_signal.assert_called_with(sender=ANY, request=ANY, user=user, device=device)

    @patch('two_factor.gateways.fake.Fake')
    @patch('two_factor.views.core.signals.user_verified.send')
    @override_settings(
        TWO_FACTOR_SMS_GATEWAY='two_factor.gateways.fake.Fake',
        TWO_FACTOR_CALL_GATEWAY='two_factor.gateways.fake.Fake',
    )
    def test_with_backup_phone(self, mock_signal, fake):
        user = self.create_user()
        TOTPDevice.objects.create(user=user, name='default', key=random_hex().decode())
        device = PhoneDevice.objects.create(
            user=user, name='backup', number='00123456789',
            method='sms', key=random_hex().decode()
        )

        # Backup phones should be listed on the login form
        response = self._post({'auth-username': 'bouke@example.com',
                               'auth-password': 'secret',
                               'login_view-current_step': 'auth'})
        self.assertContains(response, 'Send text message to 001******89')

        # Ask for challenge on invalid device
        response = self._post({'auth-username': 'bouke@example.com',
                               'auth-password': 'secret',
                               'challenge_device': 'MALICIOUS/INPUT/666'})
        self.assertContains(response, 'Send text message to 001******89')

        # Ask for SMS challenge
        response = self._post({'auth-username': 'bouke@example.com',
                               'auth-password': 'secret',
                               'challenge_device': device.persistent_id})
        self.assertContains(response, 'We sent you a text message')
        fake.return_value.send_sms.assert_called_with(
            device=device, token='%06d' % totp(device.bin_key))

        # Ask for phone challenge
        device.method = 'call'
        device.save()
        response = self._post({'auth-username': 'bouke@example.com',
                               'auth-password': 'secret',
                               'challenge_device': device.persistent_id})
        self.assertContains(response, 'We are calling your phone right now')
        fake.return_value.make_call.assert_called_with(
            device=device, token='%06d' % totp(device.bin_key))

        # Valid token should be accepted.
        response = self._post({'token-otp_token': totp(device.bin_key),
                               'login_view-current_step': 'token'})
        self.assertRedirects(response, str(settings.LOGIN_REDIRECT_URL))
        self.assertEqual(device.persistent_id,
                         self.client.session.get(DEVICE_ID_SESSION_KEY))

        # Check that the signal was fired.
        mock_signal.assert_called_with(sender=ANY, request=ANY, user=user, device=device)

    @patch('two_factor.views.core.signals.user_verified.send')
    def test_with_backup_token(self, mock_signal):
        user = self.create_user()
        TOTPDevice.objects.create(user=user, name='default', key=random_hex().decode())
        device = StaticDevice.objects.create(user=user, name='backup')
        device.token_set.create(token='abcdef123')

        # Backup phones should be listed on the login form
        response = self._post({'auth-username': 'bouke@example.com',
                               'auth-password': 'secret',
                               'login_view-current_step': 'auth'})
        self.assertContains(response, 'Backup Token')

        # Should be able to go to backup tokens step in wizard
        response = self._post({'wizard_goto_step': 'backup'})
        self.assertContains(response, 'backup tokens')

        # Wrong codes should not be accepted
        response = self._post({'backup-otp_token': 'WRONG',
                               'login_view-current_step': 'backup'})
        self.assertContains(response, 'Please enter your OTP token')

        # Valid token should be accepted.
        response = self._post({'backup-otp_token': 'abcdef123',
                               'login_view-current_step': 'backup'})
        self.assertRedirects(response, str(settings.LOGIN_REDIRECT_URL))

        # Check that the signal was fired.
        mock_signal.assert_called_with(sender=ANY, request=ANY, user=user, device=device)

    def test_change_password_in_between(self):
        """
        When the password of the user is changed while trying to login, should
        not result in errors. Refs #63.
        """
        user = self.create_user()
        self.enable_otp()

        response = self._post({'auth-username': 'bouke@example.com',
                               'auth-password': 'secret',
                               'login_view-current_step': 'auth'})
        self.assertContains(response, 'Token:')

        # Now, the password is changed. When the form is submitted, the
        # credentials should be checked again. If that's the case, the
        # login form should note that the credentials are invalid.
        user.set_password('secret2')
        user.save()
        response = self._post({'login_view-current_step': 'token'})
        self.assertContains(response, 'Please enter a correct')
        self.assertContains(response, 'and password.')


class SetupTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def setUp(self):
        super(SetupTest, self).setUp()
        self.user = self.create_user()
        self.login_user()

    def test_form(self):
        response = self.client.get(reverse('two_factor:setup'))
        self.assertContains(response, 'Follow the steps in this wizard to '
                                      'enable two-factor')

    def test_setup_generator(self):
        response = self.client.post(
            reverse('two_factor:setup'),
            data={'setup_view-current_step': 'welcome'})
        self.assertContains(response, 'Method:')

        response = self.client.post(
            reverse('two_factor:setup'),
            data={'setup_view-current_step': 'method',
                  'method-method': 'generator'})
        self.assertContains(response, 'Token:')
        session = self.client.session
        self.assertIn('django_two_factor-qr_secret_key', session.keys())

        response = self.client.post(
            reverse('two_factor:setup'),
            data={'setup_view-current_step': 'generator'})
        self.assertEqual(response.context_data['wizard']['form'].errors,
                         {'token': ['This field is required.']})

        response = self.client.post(
            reverse('two_factor:setup'),
            data={'setup_view-current_step': 'generator',
                  'generator-token': '123456'})
        self.assertEqual(response.context_data['wizard']['form'].errors,
                         {'token': ['Entered token is not valid.']})

        key = response.context_data['keys'].get('generator')
        bin_key = unhexlify(key.encode())
        response = self.client.post(
            reverse('two_factor:setup'),
            data={'setup_view-current_step': 'generator',
                  'generator-token': totp(bin_key)})
        self.assertRedirects(response, reverse('two_factor:setup_complete'))
        self.assertEqual(1, TOTPDevice.objects.filter(user=self.user).count())

    def _post(self, data):
        return self.client.post(reverse('two_factor:setup'), data=data)

    def test_no_phone(self):
        with self.settings(TWO_FACTOR_CALL_GATEWAY=None):
            response = self._post(data={'setup_view-current_step': 'welcome'})
            self.assertNotContains(response, 'call')

        with self.settings(TWO_FACTOR_CALL_GATEWAY='two_factor.gateways.fake.Fake'):
            response = self._post(data={'setup_view-current_step': 'welcome'})
            self.assertContains(response, 'call')

    @patch('two_factor.gateways.fake.Fake')
    @override_settings(TWO_FACTOR_CALL_GATEWAY='two_factor.gateways.fake.Fake')
    def test_setup_phone_call(self, fake):
        response = self._post(data={'setup_view-current_step': 'welcome'})
        self.assertContains(response, 'Method:')

        response = self._post(data={'setup_view-current_step': 'method',
                                    'method-method': 'call'})
        self.assertContains(response, 'Number:')

        response = self._post(data={'setup_view-current_step': 'call',
                                    'call-number': '+123456789'})
        self.assertContains(response, 'Token:')
        self.assertContains(response, 'We are calling your phone right now')

        # assert that the token was send to the gateway
        self.assertEqual(fake.return_value.method_calls,
                         [call.make_call(device=ANY, token=ANY)])

        # assert that tokens are verified
        response = self._post(data={'setup_view-current_step': 'validation',
                                    'validation-token': '666'})
        self.assertEqual(response.context_data['wizard']['form'].errors,
                         {'token': ['Entered token is not valid.']})

        # submitting correct token should finish the setup
        token = fake.return_value.make_call.call_args[1]['token']
        response = self._post(data={'setup_view-current_step': 'validation',
                                    'validation-token': token})
        self.assertRedirects(response, reverse('two_factor:setup_complete'))

        phones = PhoneDevice.objects.filter(user=self.user).all()
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0].name, 'default')
        self.assertEqual(phones[0].number, '+123456789')
        self.assertEqual(phones[0].method, 'call')

    @patch('two_factor.gateways.fake.Fake')
    @override_settings(TWO_FACTOR_SMS_GATEWAY='two_factor.gateways.fake.Fake')
    def test_setup_phone_sms(self, fake):
        response = self._post(data={'setup_view-current_step': 'welcome'})
        self.assertContains(response, 'Method:')

        response = self._post(data={'setup_view-current_step': 'method',
                                    'method-method': 'sms'})
        self.assertContains(response, 'Number:')

        response = self._post(data={'setup_view-current_step': 'sms',
                                    'sms-number': '+123456789'})
        self.assertContains(response, 'Token:')
        self.assertContains(response, 'We sent you a text message')

        # assert that the token was send to the gateway
        self.assertEqual(fake.return_value.method_calls,
                         [call.send_sms(device=ANY, token=ANY)])

        # assert that tokens are verified
        response = self._post(data={'setup_view-current_step': 'validation',
                                    'validation-token': '666'})
        self.assertEqual(response.context_data['wizard']['form'].errors,
                         {'token': ['Entered token is not valid.']})

        # submitting correct token should finish the setup
        token = fake.return_value.send_sms.call_args[1]['token']
        response = self._post(data={'setup_view-current_step': 'validation',
                                    'validation-token': token})
        self.assertRedirects(response, reverse('two_factor:setup_complete'))

        phones = PhoneDevice.objects.filter(user=self.user).all()
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0].name, 'default')
        self.assertEqual(phones[0].number, '+123456789')
        self.assertEqual(phones[0].method, 'sms')

    def test_already_setup(self):
        self.enable_otp()
        self.login_user()
        response = self.client.get(reverse('two_factor:setup'))
        self.assertRedirects(response, reverse('two_factor:setup_complete'))

    def test_no_double_login(self):
        """
        Activating two-factor authentication for ones account, should
        automatically mark the session as being OTP verified. Refs #44.
        """
        self.test_setup_generator()
        device = TOTPDevice.objects.filter(user=self.user).all()[0]

        self.assertEqual(device.persistent_id,
                         self.client.session.get(DEVICE_ID_SESSION_KEY))

    def test_suggest_backup_number(self):
        """
        Finishing the setup wizard should suggest to add a phone number, if
        a phone method is available. Refs #49.
        """
        self.enable_otp()
        self.login_user()

        with self.settings(TWO_FACTOR_SMS_GATEWAY=None):
            response = self.client.get(reverse('two_factor:setup_complete'))
            self.assertNotContains(response, 'Add Phone Number')

        with self.settings(TWO_FACTOR_SMS_GATEWAY='two_factor.gateways.fake.Fake'):
            response = self.client.get(reverse('two_factor:setup_complete'))
            self.assertContains(response, 'Add Phone Number')


class OTPRequiredMixinTest(UserMixin, PERSISTENCE_MODULE.TestCase):

    def test_unauthenticated_redirect(self):
        url = '/secure/'
        response = self.client.get(url)
        redirect_to = '%s?%s' % (settings.LOGIN_URL, urlencode({'next': url}))
        self.assertRedirects(response, redirect_to)

    def test_unauthenticated_raise(self):
        response = self.client.get('/secure/raises/')
        self.assertEqual(response.status_code, 403)

    def test_unverified_redirect(self):
        self.create_user()
        self.login_user()
        url = '/secure/redirect_unverified/'
        response = self.client.get(url)
        redirect_to = '%s?%s' % ('/account/login/', urlencode({'next': url}))
        self.assertRedirects(response, redirect_to)

    def test_unverified_raise(self):
        self.create_user()
        self.login_user()
        response = self.client.get('/secure/raises/')
        self.assertEqual(response.status_code, 403)

    def test_unverified_explanation(self):
        self.create_user()
        self.login_user()
        response = self.client.get('/secure/')
        self.assertContains(response, 'Permission Denied', status_code=403)
        self.assertContains(response, 'Enable Two-Factor Authentication',
                            status_code=403)

    def test_unverified_need_login(self):
        self.create_user()
        self.login_user()
        self.enable_otp()  # create OTP after login, so not verified
        url = '/secure/'
        response = self.client.get(url)
        redirect_to = '%s?%s' % (settings.LOGIN_URL, urlencode({'next': url}))
        self.assertRedirects(response, redirect_to)

    def test_verified(self):
        self.create_user()
        self.enable_otp()  # create OTP before login, so verified
        self.login_user()
        response = self.client.get('/secure/')
        self.assertEqual(response.status_code, 200)


@unittest.skipIf(
    getattr(settings, 'PERSISTENCE_STRATEGY', None) == 'mongoengine_db',
    'No admin support for mongodb'
)
class AdminPatchTest(PERSISTENCE_MODULE.TestCase):
    def setUp(self):
        patch_admin()

    def tearDown(self):
        unpatch_admin()

    def test(self):
        response = self.client.get('/admin/', follow=True)
        redirect_to = '%s?%s' % (settings.LOGIN_URL,
                                 urlencode({'next': '/admin/'}))
        self.assertRedirects(response, redirect_to)


@unittest.skipIf(
    getattr(settings, 'PERSISTENCE_STRATEGY', None) == 'mongoengine_db',
    'No admin support for mongodb'
)
class AdminSiteTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def setUp(self):
        super(AdminSiteTest, self).setUp()
        self.user = self.create_superuser()
        self.login_user()

    def test_default_admin(self):
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 200)

    def test_otp_admin_without_otp(self):
        response = self.client.get('/otp_admin/', follow=True)
        redirect_to = '%s?%s' % (settings.LOGIN_URL,
                                 urlencode({'next': '/otp_admin/'}))
        self.assertRedirects(response, redirect_to)

    def test_otp_admin_with_otp(self):
        self.enable_otp()
        self.login_user()
        response = self.client.get('/otp_admin/')
        self.assertEqual(response.status_code, 200)


class BackupTokensTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def setUp(self):
        super(BackupTokensTest, self).setUp()
        self.create_user()
        self.enable_otp()
        self.login_user()

    def test_empty(self):
        response = self.client.get(reverse('two_factor:backup_tokens'))
        self.assertContains(response, 'You don\'t have any backup codes yet.')

    def test_generate(self):
        url = reverse('two_factor:backup_tokens')

        response = self.client.post(url)
        self.assertRedirects(response, url)

        response = self.client.get(url)
        first_set = set([token.token for token in
                        response.context_data['device'].token_set.all()])
        self.assertNotContains(response, 'You don\'t have any backup codes '
                                         'yet.')
        self.assertEqual(10, len(first_set))

        # Generating the tokens should give a fresh set
        self.client.post(url)
        response = self.client.get(url)
        second_set = set([token.token for token in
                         response.context_data['device'].token_set.all()])
        self.assertEqual(10, len(second_set))
        self.assertNotEqual(first_set, second_set)


class PhoneSetupTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def setUp(self):
        super(PhoneSetupTest, self).setUp()
        self.user = self.create_user()
        self.enable_otp()
        self.login_user()

    def test_form(self):
        response = self.client.get(reverse('two_factor:phone_create'))
        self.assertContains(response, 'Number:')

    def _post(self, data=None):
        return self.client.post(reverse('two_factor:phone_create'), data=data)

    @patch('two_factor.gateways.fake.Fake')
    @override_settings(
        TWO_FACTOR_SMS_GATEWAY='two_factor.gateways.fake.Fake',
        TWO_FACTOR_CALL_GATEWAY='two_factor.gateways.fake.Fake',
    )
    def test_setup(self, fake):
        response = self._post({'phone_setup_view-current_step': 'setup',
                               'setup-number': '',
                               'setup-method': ''})
        self.assertEqual(response.context_data['wizard']['form'].errors,
                         {'method': ['This field is required.'],
                          'number': ['This field is required.']})

        response = self._post({'phone_setup_view-current_step': 'setup',
                               'setup-number': '+123456789',
                               'setup-method': 'call'})
        self.assertContains(response, 'We\'ve sent a token to your phone')
        device = response.context_data['wizard']['form'].device
        fake.return_value.make_call.assert_called_with(
            device=device, token='%06d' % totp(device.bin_key))

        response = self._post({'phone_setup_view-current_step': 'validation',
                               'validation-token': '123456'})
        self.assertEqual(response.context_data['wizard']['form'].errors,
                         {'token': ['Entered token is not valid.']})

        response = self._post({'phone_setup_view-current_step': 'validation',
                               'validation-token': totp(device.bin_key)})
        self.assertRedirects(response, str(settings.LOGIN_REDIRECT_URL))
        phones = PhoneDevice.objects.filter(user=self.user).all()
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0].name, 'backup')
        self.assertEqual(phones[0].number, '+123456789')
        self.assertEqual(phones[0].key, device.key)

    @patch('two_factor.gateways.fake.Fake')
    @override_settings(
        TWO_FACTOR_SMS_GATEWAY='two_factor.gateways.fake.Fake',
        TWO_FACTOR_CALL_GATEWAY='two_factor.gateways.fake.Fake',
    )
    def test_number_validation(self, fake):
        response = self._post({'phone_setup_view-current_step': 'setup',
                               'setup-number': '123',
                               'setup-method': 'call'})
        self.assertEqual(
            response.context_data['wizard']['form'].errors,
            {'number': [six.text_type(phone_number_validator.message)]})


class PhoneDeleteTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def setUp(self):
        super(PhoneDeleteTest, self).setUp()
        self.user = self.create_user()
        self.backup = PhoneDevice.objects.create(user=self.user, name='backup', method='sms')
        self.default = PhoneDevice.objects.create(user=self.user, name='default', method='call')
        self.login_user()

    def test_delete(self):
        response = self.client.post(reverse('two_factor:phone_delete',
                                            args=[self.backup.pk]))
        self.assertRedirects(response, str(settings.LOGIN_REDIRECT_URL))
        self.assertEqual(list(backup_phones(self.user)), [])

    def test_cannot_delete_default(self):
        response = self.client.post(reverse('two_factor:phone_delete',
                                            args=[self.default.pk]))
        self.assertContains(response, 'was not found', status_code=404)


class QRTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    test_secret = 'This is a test secret for an OTP Token'
    test_img = 'This is a test string that represents a QRCode'

    def setUp(self):
        super(QRTest, self).setUp()
        self.create_user()
        self.login_user()

    def test_without_secret(self):
        response = self.client.get(reverse('two_factor:qr'))
        self.assertEquals(response.status_code, 404)

    @patch('qrcode.make')
    def test_with_secret(self, mockqrcode):
        # Setup the mock data
        def side_effect(resp):
            resp.write(self.test_img)
        mockimg = Mock()
        mockimg.save.side_effect = side_effect
        mockqrcode.return_value = mockimg

        # Setup the session
        session = self.client.session
        session['django_two_factor-qr_secret_key'] = self.test_secret
        session.save()

        # Get default image factory
        default_factory = qrcode.image.svg.SvgPathImage

        # Get the QR code
        response = self.client.get(reverse('two_factor:qr'))

        # Check things went as expected
        mockqrcode.assert_called_with(
            get_otpauth_url('testserver:bouke@example.com', self.test_secret, "testserver"),
            image_factory=default_factory)
        mockimg.save.assert_called()
        self.assertEquals(response.status_code, 200)
        self.assertEquals(response.content.decode('utf-8'), self.test_img)
        self.assertEquals(response['Content-Type'], 'image/svg+xml; charset=utf-8')


class DisableTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def setUp(self):
        super(DisableTest, self).setUp()
        self.user = self.create_user()
        self.enable_otp()
        self.login_user()

    def test(self):
        response = self.client.get(reverse('two_factor:disable'))
        self.assertContains(response, 'Yes, I am sure')

        response = self.client.post(reverse('two_factor:disable'))
        self.assertEqual(response.context_data['form'].errors,
                         {'understand': ['This field is required.']})

        response = self.client.post(reverse('two_factor:disable'),
                                    {'understand': '1'})
        self.assertRedirects(response, str(settings.LOGIN_REDIRECT_URL))
        self.assertEqual(list(devices_for_user(self.user)), [])

        # cannot disable twice
        url = reverse('two_factor:disable')
        response = self.client.get(url)
        redirect_to = '%s?next=%s' % (settings.LOGIN_URL, url)
        self.assertRedirects(response, redirect_to)

    def test_unverified_need_login(self):
        second_user = self.create_user(username='second@example.test')
        self.login_user(user=second_user)

        # OTP was enabled after login, so the user is not verified
        self.enable_otp(user=second_user)

        url = reverse('two_factor:disable')
        redirect_to = '%s?next=%s' % (settings.LOGIN_URL, url)

        response = self.client.get(url)
        self.assertRedirects(response, redirect_to)


class TwilioGatewayTest(PERSISTENCE_MODULE.TestCase):
    def test_call_app(self):
        url = reverse('two_factor:twilio_call_app', args=['123456'])
        response = self.client.get(url)
        self.assertEqual(response.content,
                         b'<?xml version="1.0" encoding="UTF-8" ?>'
                         b'<Response>'
                         b'  <Gather timeout="15" numDigits="1" finishOnKey="">'
                         b'    <Say language="en">Hi, this is testserver calling. '
                         b'Press any key to continue.</Say>'
                         b'  </Gather>'
                         b'  <Say language="en">You didn\'t press any keys. Good bye.</Say>'
                         b'</Response>')

        url = reverse('two_factor:twilio_call_app', args=['123456'])
        response = self.client.post(url)
        self.assertEqual(response.content,
                         b'<?xml version="1.0" encoding="UTF-8" ?>'
                         b'<Response>'
                         b'  <Say language="en">Your token is 1. 2. 3. 4. 5. 6. '
                         b'Repeat: 1. 2. 3. 4. 5. 6. Good bye.</Say>'
                         b'</Response>')

        # there is a en-gb voice
        response = self.client.get('%s?%s' % (url, urlencode({'locale': 'en-gb'})))
        self.assertContains(response, '<Say language="en-gb">')

        # there is no nl voice
        response = self.client.get('%s?%s' % (url, urlencode({'locale': 'nl-nl'})))
        self.assertContains(response, '<Say language="en">')

    @override_settings(
        TWILIO_ACCOUNT_SID='SID',
        TWILIO_AUTH_TOKEN='TOKEN',
        TWILIO_CALLER_ID='+456',
    )
    @patch('two_factor.gateways.twilio.gateway.TwilioRestClient')
    def test_gateway(self, client):
        twilio = Twilio()
        client.assert_called_with('SID', 'TOKEN')

        twilio.make_call(device=Mock(number='+123'), token='654321')
        client.return_value.calls.create.assert_called_with(
            from_='+456', to='+123', method='GET', if_machine='Hangup', timeout=15,
            url='http://testserver/twilio/inbound/two_factor/654321/?locale=en-us')

        twilio.send_sms(device=Mock(number='+123'), token='654321')
        client.return_value.sms.messages.create.assert_called_with(
            to='+123', body='Your authentication token is 654321', from_='+456')

        client.return_value.calls.create.reset_mock()
        with translation.override('en-gb'):
            twilio.make_call(device=Mock(number='+123'), token='654321')
            client.return_value.calls.create.assert_called_with(
                from_='+456', to='+123', method='GET', if_machine='Hangup', timeout=15,
                url='http://testserver/twilio/inbound/two_factor/654321/?locale=en-gb')

    @override_settings(
        TWILIO_ACCOUNT_SID='SID',
        TWILIO_AUTH_TOKEN='TOKEN',
        TWILIO_CALLER_ID='+456',
    )
    @patch('two_factor.gateways.twilio.gateway.TwilioRestClient')
    def test_invalid_twilio_language(self, client):
        # This test assumes an invalid twilio voice language being present in
        # the Arabic translation. Might need to create a faux translation when
        # the translation is fixed.

        url = reverse('two_factor:twilio_call_app', args=['123456'])
        with self.assertRaises(NotImplementedError):
            self.client.get('%s?%s' % (url, urlencode({'locale': 'ar'})))

        # make_call doesn't use the voice_language, but it should raise early
        # to ease debugging.
        with self.assertRaises(NotImplementedError):
            twilio = Twilio()
            with translation.override('ar'):
                twilio.make_call(device=Mock(number='+123'), token='654321')


class FakeGatewayTest(PERSISTENCE_MODULE.TestCase):
    @patch('two_factor.gateways.fake.logger')
    def test_gateway(self, logger):
        fake = Fake()

        fake.make_call(device=Mock(number='+123'), token='654321')
        logger.info.assert_called_with(
            'Fake call to %s: "Your token is: %s"', '+123', '654321')

        fake.send_sms(device=Mock(number='+123'), token='654321')
        logger.info.assert_called_with(
            'Fake SMS to %s: "Your token is: %s"', '+123', '654321')


class PhoneDeviceTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def test_verify(self):
        device = PhoneDevice(key=random_hex().decode())
        self.assertFalse(device.verify_token(-1))
        self.assertTrue(device.verify_token(totp(device.bin_key)))

    def test_verify_token_as_string(self):
        """
        The field used to read the token may be a CharField,
        so the PhoneDevice must be able to validate tokens
        read as strings
        """
        device = PhoneDevice(key=random_hex().decode())
        self.assertTrue(device.verify_token(str(totp(device.bin_key))))

    def test_unicode(self):
        device = PhoneDevice(name='unknown')
        self.assertEqual('unknown (None)', str(device))

        device.user = self.create_user()
        self.assertEqual('unknown (bouke@example.com)', str(device))


class UtilsTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def test_default_device(self):
        user = self.create_user()
        self.assertEqual(default_device(user), None)

        PhoneDevice.objects.create(user=user, name='backup')
        self.assertEqual(default_device(user), None)

        default = PhoneDevice.objects.create(user=user, name='default')
        self.assertEqual(default_device(user).pk, default.pk)

    def test_backup_phones(self):
        self.assertQuerysetEqual(list(backup_phones(None)),
                                 list(PhoneDevice.objects.none()))
        user = self.create_user()
        PhoneDevice.objects.create(user=user, name='default')
        backup = PhoneDevice.objects.create(user=user, name='backup')
        phones = backup_phones(user)

        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0].pk, backup.pk)

    def test_get_otpauth_url(self):
        self.assertEqualUrl(
            get_otpauth_url(accountname='bouke@example.com', secret='abcdef123'),
            'otpauth://totp/bouke%40example.com?secret=abcdef123')

        self.assertEqualUrl(
            get_otpauth_url(accountname='Bouke Haarsma', secret='abcdef123'),
            'otpauth://totp/Bouke%20Haarsma?secret=abcdef123')

        self.assertEqualUrl(
            get_otpauth_url(accountname='bouke@example.com', issuer='example.com',
                            secret='abcdef123'),
            'otpauth://totp/example.com%3A%20bouke%40example.com?'
            'secret=abcdef123&issuer=example.com')

        self.assertEqualUrl(
            get_otpauth_url(accountname='bouke@example.com', issuer='My Site',
                            secret='abcdef123'),
            'otpauth://totp/My%20Site%3A%20bouke%40example.com?'
            'secret=abcdef123&issuer=My+Site')

    def assertEqualUrl(self, lhs, rhs):
        """
        We're using urlencode(dict) and the order of items in a dictionary
        is not guaranteed. Now we could use an OrderedDict, but that's not
        available on Python 2.6. We actually don't care about the order of
        the query parameters, so parsing them back into a dictionary and
        comparing that is quite fine.
        """
        lhs = urlparse(lhs)
        rhs = urlparse(rhs)
        self.assertEqual(lhs.scheme, rhs.scheme)
        self.assertEqual(lhs.netloc, rhs.netloc)
        self.assertEqual(lhs.path, rhs.path)
        self.assertEqual(lhs.fragment, rhs.fragment)
        self.assertEqual(parse_qs(lhs.query), parse_qs(rhs.query))


class ValidatorsTest(PERSISTENCE_MODULE.TestCase):
    def test_phone_number_validator_on_form_valid(self):
        class TestForm(forms.Form):
            number = forms.CharField(validators=[phone_number_validator])

        form = TestForm({
            'number': '+1234567890',
        })

        self.assertTrue(form.is_valid())

    def test_phone_number_validator_on_form_invalid(self):
        class TestForm(forms.Form):
            number = forms.CharField(validators=[phone_number_validator])

        form = TestForm({
            'number': '123',
        })

        self.assertFalse(form.is_valid())
        self.assertIn('number', form.errors)
        self.assertEqual(form.errors['number'],
                         [six.text_type(phone_number_validator.message)])


class DisableCommandTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def _assert_raises(self, err_type, err_message):
        if django.VERSION >= (1, 5):
            return self.assertRaisesMessage(err_type, err_message)
        else:
            return self.assertRaises(SystemExit)

    def test_raises(self):
        with self._assert_raises(CommandError, 'User "some_username" does not exist'):
            call_command('disable', 'some_username')

        with self._assert_raises(CommandError, 'User "other_username" does not exist'):
            call_command('disable', 'other_username', 'some_username')

    def test_disable_single(self):
        user = self.create_user()
        self.enable_otp(user)
        call_command('disable', 'bouke@example.com')
        self.assertEqual(list(devices_for_user(user)), [])

    def test_happy_flow_multiple(self):
        usernames = ['user%d@example.com' % i for i in range(0, 3)]
        users = [self.create_user(username) for username in usernames]
        [self.enable_otp(user) for user in users]
        call_command('disable', *usernames[:2])
        self.assertEqual(list(devices_for_user(users[0])), [])
        self.assertEqual(list(devices_for_user(users[1])), [])
        self.assertNotEqual(list(devices_for_user(users[2])), [])


class StatusCommandTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    def _assert_raises(self, err_type, err_message):
        if django.VERSION >= (1, 5):
            return self.assertRaisesMessage(err_type, err_message)
        else:
            return self.assertRaises(SystemExit)

    def setUp(self):
        super(StatusCommandTest, self).setUp()
        os.environ['DJANGO_COLORS'] = 'nocolor'

    def test_raises(self):
        with self._assert_raises(CommandError, 'User "some_username" does not exist'):
            call_command('status', 'some_username')

        with self._assert_raises(CommandError, 'User "other_username" does not exist'):
            call_command('status', 'other_username', 'some_username')

    def test_status_single(self):
        user = self.create_user()
        stdout = StringIO()
        call_command('status', 'bouke@example.com', stdout=stdout)
        self.assertEqual(stdout.getvalue(), 'bouke@example.com: disabled\n')

        stdout = StringIO()
        self.enable_otp(user)
        call_command('status', 'bouke@example.com', stdout=stdout)
        self.assertEqual(stdout.getvalue(), 'bouke@example.com: enabled\n')

    def test_status_mutiple(self):
        users = [self.create_user(n) for n in ['user0@example.com', 'user1@example.com']]
        self.enable_otp(users[0])
        stdout = StringIO()
        call_command('status', 'user0@example.com', 'user1@example.com', stdout=stdout)
        self.assertEqual(stdout.getvalue(), 'user0@example.com: enabled\n'
                                            'user1@example.com: disabled\n')


@unittest.skipUnless(ValidationService, 'No YubiKey support')
class YubiKeyTest(UserMixin, PERSISTENCE_MODULE.TestCase):
    @patch('otp_yubikey.models.RemoteYubikeyDevice.verify_token')
    def test_setup(self, verify_token):
        user = self.create_user()
        self.login_user()
        verify_token.return_value = [True, False]  # only first try is valid

        # Should be able to select YubiKey method
        response = self.client.post(reverse('two_factor:setup'),
                                    data={'setup_view-current_step': 'welcome'})
        self.assertContains(response, 'YubiKey')

        # The default ValidationService is created automatically if it does not exist
        ValidationService.objects.create(name='default', param_sl='', param_timeout='')

        response = self.client.post(reverse('two_factor:setup'),
                                    data={'setup_view-current_step': 'method',
                                          'method-method': 'yubikey'})
        self.assertContains(response, 'YubiKey:')

        # Should call verify_token and create the device on finish
        token = 'jlvurcgekuiccfcvgdjffjldedjjgugk'
        response = self.client.post(reverse('two_factor:setup'),
                                    data={'setup_view-current_step': 'yubikey',
                                          'yubikey-token': token})
        self.assertRedirects(response, reverse('two_factor:setup_complete'))
        verify_token.assert_called_with(token)

        yubikeys = RemoteYubikeyDevice.objects.filter(user=user).all()
        self.assertEqual(len(yubikeys), 1)
        self.assertEqual(yubikeys[0].name, 'default')

    @patch('otp_yubikey.models.RemoteYubikeyDevice.verify_token')
    def test_login(self, verify_token):
        user = self.create_user()
        verify_token.return_value = [True, False]  # only first try is valid
        service = ValidationService.objects.create(name='default', param_sl='', param_timeout='')
        RemoteYubikeyDevice.objects.create(user=user, service=service, name='default')

        # Input type should be text, not numbers like other tokens
        response = self.client.post(reverse('two_factor:login'),
                                    data={'auth-username': 'bouke@example.com',
                                          'auth-password': 'secret',
                                          'login_view-current_step': 'auth'})
        self.assertContains(response, 'YubiKey:')
        self.assertIsInstance(response.context_data['wizard']['form'].fields['otp_token'],
                              forms.CharField)

        # Should call verify_token
        token = 'cjikftknbiktlitnbltbitdncgvrbgic'
        response = self.client.post(reverse('two_factor:login'),
                                    data={'token-otp_token': token,
                                          'login_view-current_step': 'token'})
        self.assertRedirects(response, str(settings.LOGIN_REDIRECT_URL))
        verify_token.assert_called_with(token)

    def test_show_correct_label(self):
        """
        The token form replaces the input field when the user's device is a
        YubiKey. However when the user decides to enter a backup token, the
        normal backup token form should be shown. Refs #50.
        """
        user = self.create_user()
        service = ValidationService.objects.create(name='default', param_sl='', param_timeout='')
        RemoteYubikeyDevice.objects.create(user=user, service=service, name='default')
        backup = StaticDevice.objects.create(user=user, name='backup')
        backup.token_set.create(token='RANDOM')

        response = self.client.post(reverse('two_factor:login'),
                                    data={'auth-username': 'bouke@example.com',
                                          'auth-password': 'secret',
                                          'login_view-current_step': 'auth'})
        self.assertContains(response, 'YubiKey:')

        response = self.client.post(reverse('two_factor:login'),
                                    data={'wizard_goto_step': 'backup'})
        self.assertNotContains(response, 'YubiKey:')
        self.assertContains(response, 'Token:')
