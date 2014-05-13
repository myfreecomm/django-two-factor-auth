from two_factor.models import PhoneDevice

from django_otp import devices_for_user

__all__ = ['default_device', 'backup_phones', 'get_otpauth_url', 'monkeypatch_method']


def default_device(user):
    if not user or user.is_anonymous():
        return
    for device in devices_for_user(user):
        if device.name == 'default':
            return device


def backup_phones(user):
    if not user or user.is_anonymous():
        return PhoneDevice.objects.none()
    return PhoneDevice.objects.filter(user=user, name='backup')


def get_otpauth_url(alias, key):
    return 'otpauth://totp/%s?secret=%s' % (alias, key)


# from http://mail.python.org/pipermail/python-dev/2008-January/076194.html
def monkeypatch_method(cls):
    def decorator(func):
        setattr(cls, func.__name__, func)
        return func
    return decorator
