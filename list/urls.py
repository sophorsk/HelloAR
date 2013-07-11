from django.conf.urls.defaults import *
from augmented_reality.settings import ROOT

urlpatterns = patterns('list.views',
                       url(r'^item/$', 'index'),
                       url(r'^item/add/$', 'add'),
                       url(r'^item/(?P<id>\w+)/$', 'item'),
                       url(r'^item/edit/(?P<id>\w+)/$', 'edit'),
                       url(r'^item/delete/(?P<id>\w+)/$', 'delete'),
                       url(r'^auth/login/$', 'login'),
                       url(r'^auth/logout/$', 'logout'),
                       url(r'^auth/create/$', 'create'),
                       )

urlpatterns += patterns('',
                       (r'^static/(?P<path>.*)$',
                        'django.views.static.serve',
                        {'document_root': ROOT('list/static/')})
                       )
