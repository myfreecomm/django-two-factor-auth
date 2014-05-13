# -*- coding: utf-8 -*-

from settings import *  # NOQA

PERSISTENCE_STRATEGY = 'mongoengine_db'

NOSQL_DATABASES = {
    'NAME': 'django_otp',
    'HOST': 'localhost',
}

SESSION_SERIALIZER = 'mongoengine.django.sessions.BSONSerializer'
INSTALLED_APPS += ['mongoengine.django.mongo_auth']
AUTHENTICATION_BACKENDS = ('mongoengine.django.auth.MongoEngineBackend',)
AUTH_USER_MODEL = 'mongo_auth.MongoUser'
MONGOENGINE_USER_DOCUMENT = 'django_otp.mongoengine_db.TestUser'
