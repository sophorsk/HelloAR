from django import forms

from mongoengine import *
from mongoengine.django.auth import User
from mongodbforms import DocumentForm,EmbeddedDocumentForm

import datetime

# store the images using GridFSStorage
from django.db import models
from django_mongodb_engine.storage import GridFSStorage
gridfs_storage = GridFSStorage()

class Item(Document):
    user = ReferenceField(User)
    text = StringField(max_length=200)
    picture = models.FileField(storage=gridfs_storage, upload_to='/')
    created = DateTimeField(default=datetime.datetime.now)

class ItemForm(DocumentForm):
    class Meta:
        document = Item
        exclude = ('user', 'created')
        
class LoginForm(forms.Form):
    username = forms.CharField(max_length=100)
    password = forms.CharField(widget=forms.PasswordInput(render_value=False),
                               max_length=100)

class UserForm(forms.Form):
    username = forms.CharField(max_length=100)
    email = forms.CharField(max_length=100)
    password = forms.CharField(widget=forms.PasswordInput(render_value=False),
                               max_length=100)
    confirm = forms.CharField(widget=forms.PasswordInput(render_value=False),
                               max_length=100)
