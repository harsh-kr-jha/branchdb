import uuid
from django.db import models

class User(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4)
    email=models.CharField(max_length=200,unique=True)
    name=models.CharField(max_length=200)
    created_at=models.DateTimeField(auto_now_add=True)

class Project(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4)
    name=models.CharField(max_length=200)
    owner=models.ForeignKey(User,on_delete=models.CASCADE,related_name='projects')

class Task(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4)
    title=models.CharField(max_length=200)
    rank=models.IntegerField(default=0,db_default=0)
    status=models.CharField(max_length=20,default='todo',db_default='todo')
    project=models.ForeignKey(Project,on_delete=models.CASCADE,related_name='tasks')

    class Meta:
        indexes=[models.Index(fields=['rank'],name='tasks_rank_idx')]

class Comment(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4)
    body=models.TextField()
    task=models.ForeignKey(Task,on_delete=models.CASCADE,related_name='comments')
