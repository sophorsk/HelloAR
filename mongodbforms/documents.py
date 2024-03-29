import os
import itertools
from collections import Callable
from functools import reduce

from django.utils.datastructures import SortedDict
from django.forms.forms import BaseForm, get_declared_fields, NON_FIELD_ERRORS, pretty_name
from django.forms.widgets import media_property
from django.core.exceptions import FieldError
from django.core.validators import EMPTY_VALUES
from django.forms.util import ErrorList
from django.forms.formsets import BaseFormSet, formset_factory
from django.utils.translation import ugettext_lazy as _, ugettext
from django.utils.text import capfirst

from mongoengine.fields import ObjectIdField, ListField, ReferenceField, FileField, MapField
try:
    from mongoengine.base import ValidationError
except ImportError:
    from mongoengine.errors import ValidationError
from mongoengine.queryset import OperationError, Q
from mongoengine.connection import get_db, DEFAULT_CONNECTION_NAME
from gridfs import GridFS

from .documentoptions import DocumentMetaWrapper
from .util import with_metaclass, load_field_generator

_fieldgenerator = load_field_generator()

def _get_unique_filename(name, db_alias=DEFAULT_CONNECTION_NAME, collection_name='fs'):
    fs = GridFS(get_db(db_alias), collection_name)
    file_root, file_ext = os.path.splitext(name)
    count = itertools.count(1)
    while fs.exists(filename=name):
        # file_ext includes the dot.
        name = os.path.join("%s_%s%s" % (file_root, next(count), file_ext))
    return name

# The awesome Mongoengine ImageGridFsProxy wants to pull a field
# from a document to get necessary data. Trouble is that this doesn't work
# if the ImageField is stored on List or MapField. So we pass a nice fake
# document to the proxy to get saving the file done. Yeah it really is that ugly.
class FakeDocument(object):
    _fields = {}
    
    def __init__(self, key, field):
        super(FakeDocument, self).__init__()
        
        self._fields[key] = field
    
    # We don't care if anything gets marked on this
    # we do update a real field later though. That should
    # trigger the same thing on the real document.
    def _mark_as_changed(self, key):
        pass
    
def _save_iterator_file(field, uploaded_file, file_data=None):
    """
    Takes care of saving a file for a list field. Returns a Mongoengine
    fileproxy object or the file field.
    """
    fake_document = FakeDocument(field.name, field.field)
    overwrote_instance = False
    overwrote_key = False
    # for a new file we need a new proxy object
    if file_data is None:
        file_data = field.field.proxy_class(db_alias=field.field.db_alias,
                            collection_name=field.field.collection_name)
    
    if file_data.instance is None:
        file_data.instance = fake_document
        overwrote_instance = True
    if file_data.key is None:
        file_data.key = field.name
        overwrote_key = True
    
    if file_data.grid_id:
        file_data.delete()
        
    uploaded_file.seek(0)
    filename = _get_unique_filename(uploaded_file.name, field.field.db_alias, field.field.collection_name)
    file_data.put(uploaded_file, content_type=uploaded_file.content_type, filename=filename)
    file_data.close()
    
    if overwrote_instance:
        file_data.instance = None
    if overwrote_key:
        file_data.key = None
        
    return file_data

def construct_instance(form, instance, fields=None, exclude=None, ignore=None):
    """
    Constructs and returns a document instance from the bound ``form``'s
    ``cleaned_data``, but does not save the returned instance to the
    database.
    """
    cleaned_data = form.cleaned_data
    file_field_list = []
    
    # check wether object is instantiated
    if isinstance(instance, type):
        instance = instance()
        
    for f in instance._fields.values():
        if isinstance(f, ObjectIdField):
            continue
        if not f.name in cleaned_data:
            continue
        if fields is not None and f.name not in fields:
            continue
        if exclude and f.name in exclude:
            continue
        # Defer saving file-type fields until after the other fields, so a
        # callable upload_to can use the values from other fields.
        if isinstance(f, FileField) or (isinstance(f, (MapField, ListField)) and isinstance(f.field, FileField)):
            file_field_list.append(f)
        else:
            setattr(instance, f.name, cleaned_data.get(f.name))

    for f in file_field_list:
        if isinstance(f, MapField):
            map_field = getattr(instance, f.name)
            uploads = cleaned_data[f.name]
            for key, uploaded_file in uploads.items():
                if uploaded_file is None:
                    continue
                file_data = map_field.get(key, None)
                map_field[key] = _save_iterator_file(f, uploaded_file, file_data)
            setattr(instance, f.name, map_field)
        elif isinstance(f, ListField):
            list_field = getattr(instance, f.name)
            uploads = cleaned_data[f.name]
            for i, uploaded_file in enumerate(uploads):
                if uploaded_file is None:
                    continue
                try:
                    file_data = list_field[i]
                except IndexError:
                    file_data = None
                file_obj = _save_iterator_file(f, uploaded_file, file_data)
                try:
                    list_field[i] = file_obj
                except IndexError:
                    list_field.append(file_obj)
            setattr(instance, f.name, list_field)
        else:
            field = getattr(instance, f.name)
            upload = cleaned_data[f.name]
            if upload is None:
                continue
            
            try:
                upload.file.seek(0)
                # delete first to get the names right
                if field.grid_id:
                    field.delete()
                filename = _get_unique_filename(upload.name, f.db_alias, f.collection_name)
                field.put(upload, content_type=upload.content_type, filename=filename)
                setattr(instance, f.name, field)
            except AttributeError:
                # file was already uploaded and not changed during edit.
                # upload is already the gridfsproxy object we need.
                upload.get()
                setattr(instance, f.name, upload)
            
    return instance


def save_instance(form, instance, fields=None, fail_message='saved',
                  commit=True, exclude=None, construct=True):
    """
    Saves bound Form ``form``'s cleaned_data into document instance ``instance``.

    If commit=True, then the changes to ``instance`` will be saved to the
    database. Returns ``instance``.

    If construct=False, assume ``instance`` has already been constructed and
    just needs to be saved.
    """
    if construct:
        instance = construct_instance(form, instance, fields, exclude)
        
    if form.errors:
        raise ValueError("The %s could not be %s because the data didn't"
                         " validate." % (instance.__class__.__name__, fail_message))
    
    if commit and hasattr(instance, 'save'):
        # see BaseDocumentForm._post_clean for an explanation
        if hasattr(form, '_delete_before_save'):
            data = instance._data
            new_data = dict([(n, f) for n, f in data.items() if not n in form._delete_before_save])
            if hasattr(instance, '_changed_fields'):
                for field in form._delete_before_save:
                    try:
                        instance._changed_fields.remove(field)
                    except ValueError:
                        pass
            instance._data = new_data
            instance.save()
            instance._data = data
        else:
            instance.save()
        
    return instance

def document_to_dict(instance, fields=None, exclude=None):
    """
    Returns a dict containing the data in ``instance`` suitable for passing as
    a Form's ``initial`` keyword argument.

    ``fields`` is an optional list of field names. If provided, only the named
    fields will be included in the returned dict.

    ``exclude`` is an optional list of field names. If provided, the named
    fields will be excluded from the returned dict, even if they are listed in
    the ``fields`` argument.
    """
    data = {}
    for f in instance._fields.values():
        if fields and not f.name in fields:
            continue
        if exclude and f.name in exclude:
            continue
        data[f.name] = getattr(instance, f.name, '')
    return data

def fields_for_document(document, fields=None, exclude=None, widgets=None, \
                        formfield_callback=None, field_generator=_fieldgenerator):
    """
    Returns a ``SortedDict`` containing form fields for the given model.

    ``fields`` is an optional list of field names. If provided, only the named
    fields will be included in the returned fields.

    ``exclude`` is an optional list of field names. If provided, the named
    fields will be excluded from the returned fields, even if they are listed
    in the ``fields`` argument.
    """
    field_list = []
    if isinstance(field_generator, type):
        field_generator = field_generator()
        
    if formfield_callback and not isinstance(formfield_callback, Callable):
        raise TypeError('formfield_callback must be a function or callable')
    
    for name in document._fields_ordered:
        f = document._fields.get(name)
        if isinstance(f, ObjectIdField):
            continue
        if fields and not f.name in fields:
            continue
        if exclude and f.name in exclude:
            continue
        if widgets and f.name in widgets:
            kwargs = {'widget': widgets[f.name]}
        else:
            kwargs = {}

        if formfield_callback:
            formfield = formfield_callback(f, **kwargs)
        else:
            formfield = field_generator.generate(f, **kwargs)            

        if formfield:
            field_list.append((f.name, formfield))

    return SortedDict(field_list)



class ModelFormOptions(object):
    def __init__(self, options=None):
        # document class can be declared with 'document =' or 'model ='
        self.document = getattr(options, 'document', None)
        if self.document is None:
            self.document = getattr(options, 'model', None)
            
        self.model = self.document
        meta = getattr(self.document, '_meta', {})
        # set up the document meta wrapper if document meta is a dict
        if self.document is not None and not isinstance(meta, DocumentMetaWrapper):
            self.document._meta = DocumentMetaWrapper(self.document)
        self.fields = getattr(options, 'fields', None)
        self.exclude = getattr(options, 'exclude', None)
        self.widgets = getattr(options, 'widgets', None)
        self.embedded_field = getattr(options, 'embedded_field_name', None)
        self.formfield_generator = getattr(options, 'formfield_generator', _fieldgenerator)
        
        
class DocumentFormMetaclass(type):
    def __new__(cls, name, bases, attrs):
        formfield_callback = attrs.pop('formfield_callback', None)
        try:
            parents = [b for b in bases if issubclass(b, DocumentForm) or issubclass(b, EmbeddedDocumentForm)]
        except NameError:
            # We are defining DocumentForm itself.
            parents = None
        declared_fields = get_declared_fields(bases, attrs, False)
        new_class = super(DocumentFormMetaclass, cls).__new__(cls, name, bases, attrs)
        if not parents:
            return new_class

        if 'media' not in attrs:
            new_class.media = media_property(new_class)
        
        opts = new_class._meta = ModelFormOptions(getattr(new_class, 'Meta', None))
        if opts.document:
            formfield_generator = getattr(opts, 'formfield_generator', _fieldgenerator)
            
            # If a model is defined, extract form fields from it.
            fields = fields_for_document(opts.document, opts.fields,
                            opts.exclude, opts.widgets, formfield_callback, formfield_generator)
            # make sure opts.fields doesn't specify an invalid field
            none_document_fields = [k for k, v in fields.items() if not v]
            missing_fields = set(none_document_fields) - \
                             set(declared_fields.keys())
            if missing_fields:
                message = 'Unknown field(s) (%s) specified for %s'
                message = message % (', '.join(missing_fields),
                                     opts.model.__name__)
                raise FieldError(message)
            # Override default model fields with any custom declared ones
            # (plus, include all the other declared fields).
            fields.update(declared_fields)
        else:
            fields = declared_fields
            
        new_class.declared_fields = declared_fields
        new_class.base_fields = fields
        return new_class
    
    
class BaseDocumentForm(BaseForm):
    def __init__(self, data=None, files=None, auto_id='id_%s', prefix=None,
                 initial=None, error_class=ErrorList, label_suffix=':',
                 empty_permitted=False, instance=None):
        
        opts = self._meta
        
        if instance is None:
            if opts.document is None:
                raise ValueError('DocumentForm has no document class specified.')
            # if we didn't get an instance, instantiate a new one
            self.instance = opts.document
            object_data = {}
        else:
            self.instance = instance
            object_data = document_to_dict(instance, opts.fields, opts.exclude)
        
        # if initial was provided, it should override the values from instance
        if initial is not None:
            object_data.update(initial)
        
        # self._validate_unique will be set to True by BaseModelForm.clean().
        # It is False by default so overriding self.clean() and failing to call
        # super will stop validate_unique from being called.
        self._validate_unique = False
        super(BaseDocumentForm, self).__init__(data, files, auto_id, prefix, object_data,
                                            error_class, label_suffix, empty_permitted)

    def _update_errors(self, message_dict):
        for k, v in list(message_dict.items()):
            if k != NON_FIELD_ERRORS:
                self._errors.setdefault(k, self.error_class()).extend(v)
                # Remove the data from the cleaned_data dict since it was invalid
                if k in self.cleaned_data:
                    del self.cleaned_data[k]
        if NON_FIELD_ERRORS in message_dict:
            messages = message_dict[NON_FIELD_ERRORS]
            self._errors.setdefault(NON_FIELD_ERRORS, self.error_class()).extend(messages)

    def _get_validation_exclusions(self):
        """
        For backwards-compatibility, several types of fields need to be
        excluded from model validation. See the following tickets for
        details: #12507, #12521, #12553
        """
        exclude = []
        # Build up a list of fields that should be excluded from model field
        # validation and unique checks.
        for f in self.instance._fields.values():
            # Exclude fields that aren't on the form. The developer may be
            # adding these values to the model after form validation.
            if f.name not in self.fields:
                exclude.append(f.name)

            # Don't perform model validation on fields that were defined
            # manually on the form and excluded via the ModelForm's Meta
            # class. See #12901.
            elif self._meta.fields and f.name not in self._meta.fields:
                exclude.append(f.name)
            elif self._meta.exclude and f.name in self._meta.exclude:
                exclude.append(f.name)

            # Exclude fields that failed form validation. There's no need for
            # the model fields to validate them as well.
            elif f.name in list(self._errors.keys()):
                exclude.append(f.name)

            # Exclude empty fields that are not required by the form, if the
            # underlying model field is required. This keeps the model field
            # from raising a required error. Note: don't exclude the field from
            # validaton if the model field allows blanks. If it does, the blank
            # value may be included in a unique check, so cannot be excluded
            # from validation.
            else:
                field_value = self.cleaned_data.get(f.name, None)
                if not f.required and field_value in EMPTY_VALUES:
                    exclude.append(f.name)
        return exclude

    def clean(self):
        self._validate_unique = True
        return self.cleaned_data

    def _post_clean(self):
        opts = self._meta
        # Update the model instance with self.cleaned_data.
        self.instance = construct_instance(self, self.instance, opts.fields, opts.exclude)

        exclude = self._get_validation_exclusions()
        # Clean the model instance's fields.
        to_delete = []
        try:
            for f in self.instance._fields.values():
                value = getattr(self.instance, f.name)
                if f.name not in exclude:
                    f.validate(value)
                elif value in EMPTY_VALUES:
                    # mongoengine chokes on empty strings for fields
                    # that are not required. Clean them up here, though
                    # this is maybe not the right place :-)
                    to_delete.append(f.name)
        except ValidationError as e:
            err = {f.name: [e.message]}
            self._update_errors(err)
        
        # Add to_delete list to instance. It is removed in save instance
        # The reason for this is, that the field must be deleted from the 
        # instance before the instance gets saved. The changed instance gets 
        # cached and the removed field is then missing on subsequent edits.
        # To avoid that it has to be added to the instance after the instance 
        # has been saved. Kinda ugly.
        self._delete_before_save = to_delete 

        # Call the model instance's clean method.
        if hasattr(self.instance, 'clean'):
            try:
                self.instance.clean()
            except ValidationError as e:
                self._update_errors({NON_FIELD_ERRORS: e.messages})

        # Validate uniqueness if needed.
        if self._validate_unique:
            self.validate_unique()

    def validate_unique(self):
        """
        Validates unique constrains on the document.
        unique_with is supported now.
        """
        errors = []
        exclude = self._get_validation_exclusions()
        for f in self.instance._fields.values():
            if f.unique and f.name not in exclude:
                filter_kwargs = {
                    f.name: getattr(self.instance, f.name),
                    'q_obj': None,
                }
                if f.unique_with:
                    for u_with in f.unique_with:
                        u_with_field = self.instance._fields[u_with]
                        u_with_attr = getattr(self.instance, u_with)
                        # handling ListField(ReferenceField()) sucks big time
                        # What we need to do is construct a Q object that
                        # queries for the pk of every list entry and only accepts
                        # lists with the same length as the list we have
                        if isinstance(u_with_field, ListField) and \
                                isinstance(u_with_field.field, ReferenceField):
                            q = reduce(lambda x, y: x & y, [Q(**{u_with: k.pk}) for k in u_with_attr])
                            size_key = '%s__size' % u_with
                            q = q & Q(**{size_key: len(u_with_attr)})
                            filter_kwargs['q_obj'] = q & filter_kwargs['q_obj']
                        else:
                            filter_kwargs[u_with] = u_with_attr
                qs = self.instance.__class__.objects.no_dereference().filter(**filter_kwargs)
                # Exclude the current object from the query if we are editing an
                # instance (as opposed to creating a new one)
                if self.instance.pk is not None:
                    qs = qs.filter(pk__ne=self.instance.pk)
                if qs.count() > 0:
                    message = _("%(model_name)s with this %(field_label)s already exists.") %  {
                                'model_name': str(capfirst(self.instance._meta.verbose_name)),
                                'field_label': str(pretty_name(f.name))
                    }
                    err_dict = {f.name: [message]}
                    self._update_errors(err_dict)
                    errors.append(err_dict)
        
        return errors
                
    

    def save(self, commit=True):
        """
        Saves this ``form``'s cleaned_data into model instance
        ``self.instance``.

        If commit=True, then the changes to ``instance`` will be saved to the
        database. Returns ``instance``.
        """
        try:
            if self.instance.pk is None:
                fail_message = 'created'
            else:
                fail_message = 'changed'
        except (KeyError, AttributeError):
            fail_message = 'embedded document saved'
        obj = save_instance(self, self.instance, self._meta.fields,
                             fail_message, commit, construct=False)

        return obj
    save.alters_data = True

class DocumentForm(with_metaclass(DocumentFormMetaclass, BaseDocumentForm)):
    pass
    
def documentform_factory(document, form=DocumentForm, fields=None, exclude=None,
                       formfield_callback=None):
    # Build up a list of attributes that the Meta object will have.
    attrs = {'document': document, 'model': document}
    if fields is not None:
        attrs['fields'] = fields
    if exclude is not None:
        attrs['exclude'] = exclude

    # If parent form class already has an inner Meta, the Meta we're
    # creating needs to inherit from the parent's inner meta.
    parent = (object,)
    if hasattr(form, 'Meta'):
        parent = (form.Meta, object)
    Meta = type('Meta', parent, attrs)

    # Give this new form class a reasonable name.
    if isinstance(document, type):
        doc_inst = document()
    else:
        doc_inst = document
    class_name = doc_inst.__class__.__name__ + 'Form'

    # Class attributes for the new form class.
    form_class_attrs = {
        'Meta': Meta,
        'formfield_callback': formfield_callback
    }

    return DocumentFormMetaclass(class_name, (form,), form_class_attrs)


class EmbeddedDocumentForm(with_metaclass(DocumentFormMetaclass, BaseDocumentForm)):

    def __init__(self, parent_document, instance=None, position=None, *args, **kwargs):
        if self._meta.embedded_field is not None and not \
                self._meta.embedded_field in parent_document._fields:
            raise FieldError("Parent document must have field %s" % self._meta.embedded_field)
        
        if isinstance(parent_document._fields.get(self._meta.embedded_field), ListField):
            # if we received a list position of the instance and no instance
            # load the instance from the parent document and proceed as normal
            if instance is None and position is not None:
                instance = getattr(parent_document, self._meta.embedded_field)[position]
            
            # same as above only the other way around. Note: Mongoengine defines equality
            # as having the same data, so if you have 2 objects with the same data the first
            # one will be edited. That may or may not be the right one.
            if instance is not None and position is None:
                emb_list = getattr(parent_document, self._meta.embedded_field)
                position = next((i for i, obj in enumerate(emb_list) if obj == instance), None)
            
        super(EmbeddedDocumentForm, self).__init__(instance=instance, *args, **kwargs)
        self.parent_document = parent_document
        self.position = position
        
    def save(self, commit=True):
        """If commit is True the embedded document is added to the parent
        document. Otherwise the parent_document is left untouched and the
        embedded is returned as usual.
        """
        if self.errors:
            raise ValueError("The %s could not be saved because the data didn't"
                         " validate." % self.instance.__class__.__name__)
        
        if commit:
            field = self.parent_document._fields.get(self._meta.embedded_field)
            if isinstance(field, ListField) and self.position is None:
                # no position given, simply appending to ListField
                try:
                    self.parent_document.update(**{"push__" + self._meta.embedded_field: self.instance})
                except:
                    raise OperationError("The %s could not be appended." % self.instance.__class__.__name__)
            elif isinstance(field, ListField) and self.position is not None:
                # updating ListField at given position
                try:
                    self.parent_document.update(**{"__".join(("set", self._meta.embedded_field,
                                                                str(self.position))): self.instance})
                except:
                    raise OperationError("The %s could not be updated at position "
                                            "%d." % (self.instance.__class__.__name__, self.position))
            else:
                # not a listfield on parent, treat as an embedded field
                setattr(self.parent_document, self._meta.embedded_field, self.instance)
                self.parent_document.save() 
        return self.instance


class BaseDocumentFormSet(BaseFormSet):
    """
    A ``FormSet`` for editing a queryset and/or adding new objects to it.
    """

    def __init__(self, data=None, files=None, auto_id='id_%s', prefix=None,
                 queryset=None, **kwargs):
        self.queryset = queryset
        self._queryset = self.queryset
        self.initial = self.construct_initial()
        defaults = {'data': data, 'files': files, 'auto_id': auto_id, 
                    'prefix': prefix, 'initial': self.initial}
        defaults.update(kwargs)
        super(BaseDocumentFormSet, self).__init__(**defaults)

    def construct_initial(self):
        initial = []
        try:
            for d in self.get_queryset():
                initial.append(document_to_dict(d))
        except TypeError:
            pass 
        return initial

    def initial_form_count(self):
        """Returns the number of forms that are required in this FormSet."""
        if not (self.data or self.files):
            return len(self.get_queryset())
        return super(BaseDocumentFormSet, self).initial_form_count()

    def get_queryset(self):
        return self._queryset

    def save_object(self, form):
        obj = form.save(commit=False)
        return obj

    def save(self, commit=True):
        """
        Saves model instances for every form, adding and changing instances
        as necessary, and returns the list of instances.
        """ 
        saved = []
        for form in self.forms:
            if not form.has_changed() and not form in self.initial_forms:
                continue
            obj = self.save_object(form)
            if form.cleaned_data.get("DELETE", False):
                try:
                    obj.delete()
                except AttributeError:
                    # if it has no delete method it is an 
                    # embedded object. We just don't add to the list
                    # and it's gone. Cool huh?
                    continue
            if commit:
                obj.save()
            saved.append(obj)    
        return saved

    def clean(self):
        self.validate_unique()

    def validate_unique(self):
        errors = []
        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue
            errors += form.validate_unique()
            
        if errors:
            raise ValidationError(errors)
        
    def get_date_error_message(self, date_check):
        return ugettext("Please correct the duplicate data for %(field_name)s "
            "which must be unique for the %(lookup)s in %(date_field)s.") % {
            'field_name': date_check[2],
            'date_field': date_check[3],
            'lookup': str(date_check[1]),
        }

    def get_form_error(self):
        return ugettext("Please correct the duplicate values below.")

def documentformset_factory(document, form=DocumentForm, formfield_callback=None,
                         formset=BaseDocumentFormSet,
                         extra=1, can_delete=False, can_order=False,
                         max_num=None, fields=None, exclude=None):
    """
    Returns a FormSet class for the given Django model class.
    """
    form = documentform_factory(document, form=form, fields=fields, exclude=exclude,
                             formfield_callback=formfield_callback)
    FormSet = formset_factory(form, formset, extra=extra, max_num=max_num,
                              can_order=can_order, can_delete=can_delete)
    FormSet.model = document
    FormSet.document = document
    return FormSet


class BaseInlineDocumentFormSet(BaseDocumentFormSet):
    """
    A formset for child objects related to a parent.
    
    self.instance -> the document containing the inline objects
    """
    def __init__(self, data=None, files=None, instance=None,
                 save_as_new=False, prefix=None, queryset=[], **kwargs):
        self.instance = instance
        self.save_as_new = save_as_new
        
        super(BaseInlineDocumentFormSet, self).__init__(data, files, prefix=prefix, queryset=queryset, **kwargs)

    def initial_form_count(self):
        if self.save_as_new:
            return 0
        return super(BaseInlineDocumentFormSet, self).initial_form_count()

    #@classmethod
    def get_default_prefix(cls):
        return cls.document.__name__.lower()
    get_default_prefix = classmethod(get_default_prefix)
    

    def add_fields(self, form, index):
        super(BaseInlineDocumentFormSet, self).add_fields(form, index)

        # Add the generated field to form._meta.fields if it's defined to make
        # sure validation isn't skipped on that field.
        if form._meta.fields:
            if isinstance(form._meta.fields, tuple):
                form._meta.fields = list(form._meta.fields)
            #form._meta.fields.append(self.fk.name)

    def get_unique_error_message(self, unique_check):
        unique_check = [field for field in unique_check if field != self.fk.name]
        return super(BaseInlineDocumentFormSet, self).get_unique_error_message(unique_check)


def inlineformset_factory(document, form=DocumentForm,
                          formset=BaseInlineDocumentFormSet,
                          fields=None, exclude=None,
                          extra=1, can_order=False, can_delete=True, max_num=None,
                          formfield_callback=None):
    """
    Returns an ``InlineFormSet`` for the given kwargs.

    You must provide ``fk_name`` if ``model`` has more than one ``ForeignKey``
    to ``parent_model``.
    """
    kwargs = {
        'form': form,
        'formfield_callback': formfield_callback,
        'formset': formset,
        'extra': extra,
        'can_delete': can_delete,
        'can_order': can_order,
        'fields': fields,
        'exclude': exclude,
        'max_num': max_num,
    }
    FormSet = documentformset_factory(document, **kwargs)
    return FormSet


#class BaseInlineDocumentFormSet(BaseDocumentFormSet):
#    """A formset for child objects related to a parent."""
#    def __init__(self, data=None, files=None, instance=None,
#                 save_as_new=False, prefix=None, queryset=None, **kwargs):
#        if instance is None:
#            self.instance = self.rel_field.name
#        else:
#            self.instance = instance
#        self.save_as_new = save_as_new
#        if queryset is None:
#            queryset = self.model._default_manager
#        if self.instance.pk:
#            qs = queryset.filter(**{self.fk.name: self.instance})
#        else:
#            qs = queryset.none()
#        super(BaseInlineDocumentFormSet, self).__init__(data, files, prefix=prefix,
#                                                queryset=qs, **kwargs)
#
#    def initial_form_count(self):
#        if self.save_as_new:
#            return 0
#        return super(BaseInlineDocumentFormSet, self).initial_form_count()
#
#
#    def _construct_form(self, i, **kwargs):
#        form = super(BaseInlineDocumentFormSet, self)._construct_form(i, **kwargs)
#        if self.save_as_new:
#            # Remove the primary key from the form's data, we are only
#            # creating new instances
#            form.data[form.add_prefix(self._pk_field.name)] = None
#
#            # Remove the foreign key from the form's data
#            form.data[form.add_prefix(self.fk.name)] = None
#
#        # Set the fk value here so that the form can do its validation.
#        setattr(form.instance, self.fk.get_attname(), self.instance.pk)
#        return form
#
#    @classmethod
#    def get_default_prefix(cls):
#        from django.db.models.fields.related import RelatedObject
#        return RelatedObject(cls.fk.rel.to, cls.model, cls.fk).get_accessor_name().replace('+','')
#
#    def save_new(self, form, commit=True):
#        # Use commit=False so we can assign the parent key afterwards, then
#        # save the object.
#        obj = form.save(commit=False)
#        pk_value = getattr(self.instance, self.fk.rel.field_name)
#        setattr(obj, self.fk.get_attname(), getattr(pk_value, 'pk', pk_value))
#        if commit:
#            obj.save()
#        # form.save_m2m() can be called via the formset later on if commit=False
#        if commit and hasattr(form, 'save_m2m'):
#            form.save_m2m()
#        return obj
#
#    def add_fields(self, form, index):
#        super(BaseInlineDocumentFormSet, self).add_fields(form, index)
#        if self._pk_field == self.fk:
#            name = self._pk_field.name
#            kwargs = {'pk_field': True}
#        else:
#            # The foreign key field might not be on the form, so we poke at the
#            # Model field to get the label, since we need that for error messages.
#            name = self.fk.name
#            kwargs = {
#                'label': getattr(form.fields.get(name), 'label', capfirst(self.fk.verbose_name))
#            }
#            if self.fk.rel.field_name != self.fk.rel.to._meta.pk.name:
#                kwargs['to_field'] = self.fk.rel.field_name
#
#        form.fields[name] = InlineForeignKeyField(self.instance, **kwargs)
#
#        # Add the generated field to form._meta.fields if it's defined to make
#        # sure validation isn't skipped on that field.
#        if form._meta.fields:
#            if isinstance(form._meta.fields, tuple):
#                form._meta.fields = list(form._meta.fields)
#            form._meta.fields.append(self.fk.name)
#
#    def get_unique_error_message(self, unique_check):
#        unique_check = [field for field in unique_check if field != self.fk.name]
#        return super(BaseInlineDocumentFormSet, self).get_unique_error_message(unique_check)
#
#
#def _get_rel_field(parent_document, model, rel_name=None, can_fail=False):
#    """
#    Finds and returns the ForeignKey from model to parent if there is one
#    (returns None if can_fail is True and no such field exists). If fk_name is
#    provided, assume it is the name of the ForeignKey field. Unles can_fail is
#    True, an exception is raised if there is no ForeignKey from model to
#    parent_document.
#    """
#    #opts = model._meta
#    fields = model._fields
#    if rel_name:
#        if rel_name not in fields:
#            raise Exception("%s has no field named '%s'" % (model, rel_name))
#        
#        rel_model = getattr(model, rel_name, None)
#        ref_field = fields.get(rel_name)
#        if not isinstance(ref_field, ReferenceField) or \
#                rel_model != parent_document:
#            raise Exception("rel_name '%s' is not a reference to %s" % (rel_name, parent_document))    
#    else:
#        # Try to discover what the ForeignKey from model to parent_document is
#        rel_to_parent = [
#            f for f in fields
#            if isinstance(f, ReferenceField)
#            and getattr(model, f.name) == parent_document
#        ]
#        if len(rel_to_parent) == 1:
#            ref_field = rel_to_parent[0]
#        elif len(rel_to_parent) == 0:
#            if can_fail:
#                return
#            raise Exception("%s has no relation to %s" % (model, parent_document))
#        else:
#            raise Exception("%s has more than 1 relation to %s" % (model, parent_document))
#    return ref_field
#
#
#def inlineformset_factory(parent_document, model, form=ModelForm,
#                          formset=BaseInlineFormSet, fk_name=None,
#                          fields=None, exclude=None, extra=3, can_order=False,
#                          can_delete=True, max_num=None, formfield_callback=None,
#                          widgets=None, validate_max=False, localized_fields=None):
#    """
#    Returns an ``InlineFormSet`` for the given kwargs.
#
#    You must provide ``fk_name`` if ``model`` has more than one ``ForeignKey``
#    to ``parent_document``.
#    """
#    rel_field = _get_rel_field(parent_document, model, fk_name=fk_name)
#    # You can't have more then reference in a ReferenceField
#    # so max_num is always one for now (maybe 
#    # ListFields(ReferenceFields) will be supported one day).
#    max_num = 1
#    kwargs = {
#        'form': form,
#        'formfield_callback': formfield_callback,
#        'formset': formset,
#        'extra': extra,
#        'can_delete': can_delete,
#        'can_order': can_order,
#        'fields': fields,
#        'exclude': exclude,
#        'max_num': max_num,
#        'widgets': widgets,
#        'validate_max': validate_max,
#        'localized_fields': localized_fields,
#    }
#    FormSet = documentformset_factory(model, **kwargs)
#    FormSet.rel_field = rel_field
#    return FormSet

class EmbeddedDocumentFormSet(BaseDocumentFormSet):
    def __init__(self, data=None, files=None, save_as_new=False, 
                 prefix=None, queryset=[], parent_document=None, **kwargs):
        self.parent_document = parent_document
        super(EmbeddedDocumentFormSet, self).__init__(data, files, save_as_new, prefix, queryset, **kwargs)
        
    def _construct_form(self, i, **kwargs):
        defaults = {'parent_document': self.parent_document}
        
        # add position argument to the form. Otherwise we will spend
        # a huge amount of time iterating over the list field on form __init__
        emb_list = getattr(self.parent_document, self.form._meta.embedded_field)
        if emb_list is not None and len(emb_list) < i:
            defaults['position'] = i
        defaults.update(kwargs)
        
        form = super(EmbeddedDocumentFormSet, self)._construct_form(i, **defaults)
        return form

    @property
    def empty_form(self):
        form = self.form(
            self.parent_document,
            auto_id=self.auto_id,
            prefix=self.add_prefix('__prefix__'),
            empty_permitted=True,
        )
        self.add_fields(form, None)
        return form
    
    def save(self, commit=True):
        # Don't try to save the new documents. Embedded objects don't have
        # a save method anyway.
        objs = super(EmbeddedDocumentFormSet, self).save(commit=False)
        
        if commit and self.parent_document is not None:
            form = self.empty_form
            # The thing about formsets is that the base use case is to edit *all*
            # of the associated objects on a model. As written, using these FormSets this
            # way will cause the existing embedded documents to get saved along with a
            # copy of themselves plus any new ones you added.
            #
            # The only way you could do "updates" of existing embedded document fields is
            # if those embedded documents had ObjectIDs of their own, which they don't
            # by default in Mongoengine.
            #
            # In this case it makes the most sense to simply replace the embedded field
            # with the new values gathered form the formset, rather than adding the new
            # values to the existing values, because the new values will almost always
            # contain the old values (with the default use case.)
            #
            # attr_data = getattr(self.parent_document, form._meta.embedded_field, [])
            setattr(self.parent_document, form._meta.embedded_field, objs or [])
            self.parent_document.save()
        
        return objs 


def embeddedformset_factory(document, parent_document, form=EmbeddedDocumentForm,
                          formset=EmbeddedDocumentFormSet,
                          fields=None, exclude=None,
                          extra=1, can_order=False, can_delete=True, max_num=None,
                          formfield_callback=None):
    """
    Returns an ``InlineFormSet`` for the given kwargs.

    You must provide ``fk_name`` if ``model`` has more than one ``ForeignKey``
    to ``parent_model``.
    """
    kwargs = {
        'form': form,
        'formfield_callback': formfield_callback,
        'formset': formset,
        'extra': extra,
        'can_delete': can_delete,
        'can_order': can_order,
        'fields': fields,
        'exclude': exclude,
        'max_num': max_num,
    }
    FormSet = documentformset_factory(document, **kwargs)
    return FormSet
