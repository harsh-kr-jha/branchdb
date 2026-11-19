"""Exercise the actual Django ORM in independent application processes."""
import os
import sys
import uuid
os.environ.setdefault('DJANGO_SETTINGS_MODULE','settings')
import django
django.setup()
from django.db import IntegrityError,transaction
from tasks.models import User,Project,Task,Comment
mode=sys.argv[1]
if mode=='seed':
    user=User.objects.create(email='django@example.test',name='Inherited')
    project=Project.objects.create(name='Build',owner=user)
    task=Task.objects.create(title='Ship',project=project)
    Comment.objects.create(body='First',task=task)
    assert task.rank==0 and task.status=='todo'
    print('PASS: Django nested application seed and defaults')
elif mode=='branch':
    user=User.objects.prefetch_related('projects__tasks__comments').get(email='django@example.test')
    assert user.name=='Inherited'
    assert user.projects.get().tasks.get().comments.get().body=='First'
    updated,created=User.objects.update_or_create(email=user.email,defaults={'name':os.environ['BRANCH_NAME']})
    assert not created and updated.pk==user.pk
    try:
        with transaction.atomic():Project.objects.create(name='Orphan',owner_id=uuid.uuid4())
        raise AssertionError('deferred FK should reject orphan at commit')
    except IntegrityError:pass
    user.delete()
    assert not Project.objects.exists() and not Task.objects.exists() and not Comment.objects.exists()
    print('PASS: Django inherited rows, update_or_create, deferred FK and cascade')
else:raise ValueError(mode)
