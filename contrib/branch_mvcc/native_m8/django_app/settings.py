import os
from urllib.parse import parse_qs, unquote, urlsplit
SECRET_KEY='disposable-native-branch-framework-fixture'
INSTALLED_APPS=['tasks']
DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
USE_TZ=True
TIME_ZONE='UTC'
parts=urlsplit(os.environ['DATABASE_URL'])
query=parse_qs(parts.query)
DATABASES={'default':{
 'ENGINE':'django.db.backends.postgresql',
 'NAME':parts.path.lstrip('/'),
 'USER':unquote(parts.username or query.get('user',[''])[0]),
 'PASSWORD':unquote(parts.password or ''),
 'HOST':query.get('host',[parts.hostname or ''])[0],
 'PORT':str(parts.port or 58445),
 'OPTIONS':{'options':query.get('options',['-c branch=main'])[0]},
}}
