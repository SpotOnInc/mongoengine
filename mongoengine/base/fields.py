import operator
import warnings
import weakref

from bson import DBRef, ObjectId

from mongoengine.common import _import_class
from mongoengine.errors import ValidationError

from mongoengine.base.common import ALLOW_INHERITANCE
from mongoengine.base.datastructures import BaseDict, BaseList

__all__ = ("BaseField", "ComplexBaseField", "ObjectIdField")


class BaseField(object):
    """A base class for fields in a MongoDB document. Instances of this class
    may be added to subclasses of `Document` to define a document's schema.

    .. versionchanged:: 0.5 - added verbose and help text
    """

    name = None
    _geo_index = False
    _auto_gen = False  # Call `generate` to generate a value
    _auto_dereference = True

    # These track each time a Field instance is created. Used to retain order.
    # The auto_creation_counter is used for fields that MongoEngine implicitly
    # creates, creation_counter is used for all user-specified fields.
    creation_counter = 0
    auto_creation_counter = -1

    def __init__(self, db_field=None, name=None, required=False, default=None,
                 unique=False, unique_with=None, primary_key=False,
                 validation=None, choices=None, verbose_name=None,
                 help_text=None):
        self.db_field = (db_field or name) if not primary_key else '_id'
        if name:
            msg = "Fields' 'name' attribute deprecated in favour of 'db_field'"
            warnings.warn(msg, DeprecationWarning)
        self.required = required or primary_key
        self.default = default
        self.unique = bool(unique or unique_with)
        self.unique_with = unique_with
        self.primary_key = primary_key
        self.validation = validation
        self.choices = choices
        self.verbose_name = verbose_name
        self.help_text = help_text

        # Adjust the appropriate creation counter, and save our local copy.
        if self.db_field == '_id':
            self.creation_counter = BaseField.auto_creation_counter
            BaseField.auto_creation_counter -= 1
        else:
            self.creation_counter = BaseField.creation_counter
            BaseField.creation_counter += 1

    def __get__(self, instance, owner):
        """Descriptor for retrieving a value from a field in a document. Do
        any necessary conversion between Python and MongoDB types.
        """
        if instance is None:
            # Document class being used rather than a document object
            return self
        # Get value from document instance if available, if not use default
        value = instance._data.get(self.name)

        if value is None:
            value = self.default
            # Allow callable default values
            if callable(value):
                value = value()

        EmbeddedDocument = _import_class('EmbeddedDocument')
        if isinstance(value, EmbeddedDocument) and value._instance is None:
            value._instance = weakref.proxy(instance)
        return value

    def __set__(self, instance, value):
        """Descriptor for assigning a value to a field in a document.
        """
        changed = False
        if (self.name not in instance._data or
           instance._data[self.name] != value):
            changed = True
            instance._data[self.name] = value
        if changed and instance._initialised:
            instance._mark_as_changed(self.name)

    def error(self, message="", errors=None, field_name=None):
        """Raises a ValidationError.
        """
        field_name = field_name if field_name else self.name
        raise ValidationError(message, errors=errors, field_name=field_name)

    def to_python(self, value):
        """Convert a MongoDB-compatible type to a Python type.
        """
        return value

    def to_mongo(self, value):
        """Convert a Python type to a MongoDB-compatible type.
        """
        return self.to_python(value)

    def prepare_query_value(self, op, value):
        """Prepare a value that is being used in a query for PyMongo.
        """
        return value

    def validate(self, value, clean=True):
        """Perform validation on a value.
        """
        pass

    def _validate(self, value, **kwargs):
        Document = _import_class('Document')
        EmbeddedDocument = _import_class('EmbeddedDocument')
        # check choices
        if self.choices:
            is_cls = isinstance(value, (Document, EmbeddedDocument))
            value_to_check = value.__class__ if is_cls else value
            err_msg = 'an instance' if is_cls else 'one'
            if isinstance(self.choices[0], (list, tuple)):
                option_keys = [k for k, v in self.choices]
                if value_to_check not in option_keys:
                    msg = ('Value must be %s of %s (is %s)' %
                           (err_msg, unicode(option_keys), unicode(value)))
                    self.error(msg)
            elif value_to_check not in self.choices:
                msg = ('Value must be %s of %s (is %s)' %
                       (err_msg, unicode(self.choices), unicode(value)))
                self.error(msg)

        # check validation argument
        if self.validation is not None:
            if callable(self.validation):
                if not self.validation(value):
                    self.error('Value does not match custom validation method')
            else:
                raise ValueError('validation argument for "%s" must be a '
                                 'callable.' % self.name)

        self.validate(value, **kwargs)


class ComplexBaseField(BaseField):
    """Handles complex fields, such as lists / dictionaries.

    Allows for nesting of embedded documents inside complex types.
    Handles the lazy dereferencing of a queryset by lazily dereferencing all
    items in a list / dict rather than one at a time.

    .. versionadded:: 0.5
    """

    field = None
    __dereference = False

    def __get__(self, instance, owner):
        """Descriptor to automatically dereference references.
        """
        if instance is None:
            # Document class being used rather than a document object
            return self

        ReferenceField = _import_class('ReferenceField')
        GenericReferenceField = _import_class('GenericReferenceField')
        dereference = (self._auto_dereference and
                       (self.field is None or isinstance(self.field,
                        (GenericReferenceField, ReferenceField))))

        self._auto_dereference = instance._fields[self.name]._auto_dereference
        if not self.__dereference and instance._initialised and dereference:
            instance._data[self.name] = self._dereference(
                instance._data.get(self.name), max_depth=1, instance=instance,
                name=self.name
            )

        value = super(ComplexBaseField, self).__get__(instance, owner)

        # Convert lists / values so we can watch for any changes on them
        if (isinstance(value, (list, tuple)) and
            not isinstance(value, BaseList)):
            value = BaseList(value, instance, self.name)
            instance._data[self.name] = value
        elif isinstance(value, dict) and not isinstance(value, BaseDict):
            value = BaseDict(value, instance, self.name)
            instance._data[self.name] = value

        if (self._auto_dereference and instance._initialised and
            isinstance(value, (BaseList, BaseDict))
            and not value._dereferenced):
            value = self._dereference(
                value, max_depth=1, instance=instance, name=self.name
            )
            value._dereferenced = True
            instance._data[self.name] = value

        return value

    def __set__(self, instance, value):
        """Descriptor for assigning a value to a field in a document.
        """
        instance._data[self.name] = value
        instance._mark_as_changed(self.name)

    def to_python(self, value):
        """Convert a MongoDB-compatible type to a Python type.
        """
        Document = _import_class('Document')

        if isinstance(value, basestring):
            return value

        if hasattr(value, 'to_python'):
            return value.to_python()

        is_list = False
        if not hasattr(value, 'items'):
            try:
                is_list = True
                value = dict([(k, v) for k, v in enumerate(value)])
            except TypeError:  # Not iterable return the value
                return value

        if self.field:
            value_dict = dict([(key, self.field.to_python(item))
                                for key, item in value.items()])
        else:
            value_dict = {}
            for k, v in value.items():
                if isinstance(v, Document):
                    # We need the id from the saved object to create the DBRef
                    if v.pk is None:
                        self.error('You can only reference documents once they'
                                   ' have been saved to the database')
                    collection = v._get_collection_name()
                    value_dict[k] = DBRef(collection, v.pk)
                elif hasattr(v, 'to_python'):
                    value_dict[k] = v.to_python()
                else:
                    value_dict[k] = self.to_python(v)

        if is_list:  # Convert back to a list
            return [v for k, v in sorted(value_dict.items(),
                                         key=operator.itemgetter(0))]
        return value_dict

    def to_mongo(self, value):
        """Convert a Python type to a MongoDB-compatible type.
        """
        Document = _import_class("Document")
        EmbeddedDocument = _import_class("EmbeddedDocument")
        GenericReferenceField = _import_class("GenericReferenceField")

        if isinstance(value, basestring):
            return value

        if hasattr(value, 'to_mongo'):
            if isinstance(value, Document):
                return GenericReferenceField().to_mongo(value)
            cls = value.__class__
            val = value.to_mongo()
            # If we its a document thats not inherited add _cls
            if (isinstance(value, EmbeddedDocument)):
                val['_cls'] = cls.__name__
            return val

        is_list = False
        if not hasattr(value, 'items'):
            try:
                is_list = True
                value = dict([(k, v) for k, v in enumerate(value)])
            except TypeError:  # Not iterable return the value
                return value

        if self.field:
            value_dict = dict([(key, self.field.to_mongo(item))
                                for key, item in value.iteritems()])
        else:
            value_dict = {}
            for k, v in value.iteritems():
                if isinstance(v, Document):
                    # We need the id from the saved object to create the DBRef
                    if v.pk is None:
                        self.error('You can only reference documents once they'
                                   ' have been saved to the database')

                    # If its a document that is not inheritable it won't have
                    # any _cls data so make it a generic reference allows
                    # us to dereference
                    meta = getattr(v, '_meta', {})
                    allow_inheritance = (
                        meta.get('allow_inheritance', ALLOW_INHERITANCE)
                        == True)
                    if not allow_inheritance and not self.field:
                        value_dict[k] = GenericReferenceField().to_mongo(v)
                    else:
                        collection = v._get_collection_name()
                        value_dict[k] = DBRef(collection, v.pk)
                elif hasattr(v, 'to_mongo'):
                    cls = v.__class__
                    val = v.to_mongo()
                    # If we its a document thats not inherited add _cls
                    if (isinstance(v, (Document, EmbeddedDocument))):
                        val['_cls'] = cls.__name__
                    value_dict[k] = val
                else:
                    value_dict[k] = self.to_mongo(v)

        if is_list:  # Convert back to a list
            return [v for k, v in sorted(value_dict.items(),
                                         key=operator.itemgetter(0))]
        return value_dict

    def validate(self, value):
        """If field is provided ensure the value is valid.
        """
        errors = {}
        if self.field:
            if hasattr(value, 'iteritems') or hasattr(value, 'items'):
                sequence = value.iteritems()
            else:
                sequence = enumerate(value)
            for k, v in sequence:
                try:
                    self.field._validate(v)
                except ValidationError, error:
                    errors[k] = error.errors or error
                except (ValueError, AssertionError), error:
                    errors[k] = error

            if errors:
                field_class = self.field.__class__.__name__
                self.error('Invalid %s item (%s)' % (field_class, value),
                           errors=errors)
        # Don't allow empty values if required
        if self.required and not value:
            self.error('Field is required and cannot be empty')

    def prepare_query_value(self, op, value):
        return self.to_mongo(value)

    def lookup_member(self, member_name):
        if self.field:
            return self.field.lookup_member(member_name)
        return None

    def _set_owner_document(self, owner_document):
        if self.field:
            self.field.owner_document = owner_document
        self._owner_document = owner_document

    def _get_owner_document(self, owner_document):
        self._owner_document = owner_document

    owner_document = property(_get_owner_document, _set_owner_document)

    @property
    def _dereference(self,):
        if not self.__dereference:
            DeReference = _import_class("DeReference")
            self.__dereference = DeReference()  # Cached
        return self.__dereference


class ObjectIdField(BaseField):
    """A field wrapper around MongoDB's ObjectIds.
    """

    def to_python(self, value):
        if not isinstance(value, ObjectId):
            value = ObjectId(value)
        return value

    def to_mongo(self, value):
        if not isinstance(value, ObjectId):
            try:
                return ObjectId(unicode(value))
            except Exception, e:
                # e.message attribute has been deprecated since Python 2.6
                self.error(unicode(e))
        return value

    def prepare_query_value(self, op, value):
        return self.to_mongo(value)

    def validate(self, value):
        try:
            ObjectId(unicode(value))
        except:
            self.error('Invalid Object ID')
