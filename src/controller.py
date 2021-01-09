"""
Controllers for API doc addition
and API metadata operations
"""
import base64
import copy
import gzip
import json
import logging
import string
import sys
from abc import ABC, abstractmethod
from collections import OrderedDict
from datetime import datetime as dt

import jsonschema

if sys.version_info.major >= 3 and sys.version_info.minor >= 6:
    from hashlib import blake2b
else:
    from pyblake2 import blake2b  # pylint: disable=import-error

from elasticsearch import RequestError

from model import APIDoc
from utils.schema_download import SchemaDownloader

logger = logging.getLogger(__name__)

# *****************************************************************************
# Validation schemas, tags
# *****************************************************************************

# Official oas3 json schema for validation is still in-development.
# For now we us this updated oas3-schema from swagger-editor
OAS3_SCHEMA_URL = 'https://raw.githubusercontent.com/swagger-api/swagger-editor/v3.7.1/src/plugins/json-schema-validator/oas3-schema.yaml'
SWAGGER2_SCHEMA_URL = 'https://raw.githubusercontent.com/swagger-api/swagger-editor/v3.6.1/src/plugins/validate-json-schema/structural-validation/swagger2-schema.js'

# List of root keys that should be indexed in version 2 schema
SWAGGER2_INDEXED_ITEMS = ['info', 'tags', 'swagger', 'host', 'basePath']

# list of major versions of schema that we support
SUPPORTED_SCHEMA_VERSIONS = ['SWAGGER2', 'OAS3']

# Translator schema
TRANSLATOR_URL = 'https://raw.githubusercontent.com/NCATSTranslator/translator_extensions/main/x-translator/smartapi_x-translator_schema.json'

# *****************************************************************************
# Custom Exceptions
# *****************************************************************************

class RegistryError(Exception):
    """General API error"""

# *****************************************************************************
# API Doc Controller
# *****************************************************************************

class SmartAPI(ABC):

    def __init__(self, metadata):
        self._metadata = metadata
        self.version = ''

    def __getitem__(self, key):
        return self._metadata[key]

    @staticmethod
    def exists(_id):
        return APIDoc.exists(_id)

    # POST
    @abstractmethod
    def save(self, api_doc, user_name=None, **options):
        pass

    @staticmethod
    def from_dict(dic):
        if 'openapi' in dic:
            return V3Metadata(dic)
        elif 'swagger' in dic:
            return V2Metadata(dic)
        else:
            raise RegistryError('Version unknown')

    @staticmethod
    def get(_id):
        return APIDoc.get(_id)

    # VALIDATION
    @staticmethod
    def slug_is_available(slug):
        """
        Check if a slug is available

        Returns:
            Bool = exists
        """
        if not slug:
            raise RequestError('slug is required')
        res = APIDoc.slug_exists(slug)
        return res

    @classmethod
    def validate_slug_name(cls, slug_name):
        """
        Function that determines whether slug_name is a valid slug name
        """
        _valid_chars = string.ascii_letters + string.digits + "-_~"
        _slug = slug_name.lower()
        if _slug in ('www', 'dev', 'smart-api'):
            raise RegistryError(f"Slug name {slug_name} is reserved, please choose another")
        if not all([x in _valid_chars for x in _slug]):
            raise RegistryError(f"Slug name {slug_name} contains invalid characters")
        if APIDoc.slug_exists(slug=_slug):
            raise RegistryError(f"Slug name {slug_name} already exists")

    # GET
    @staticmethod
    def get_api_by_id(_id):
        """
        Get one doc by id
        """
        if not SmartAPI.exists(_id):
            raise RegistryError(f"API with id '{_id}' does not exist")

        doc = SmartAPI.get(_id)

        _raw = gzip.decompress(base64.urlsafe_b64decode(doc['~raw'])).decode('utf-8')
        d = json.loads(_raw)
        d2 = OrderedDict()
        for key in ['openapi', 'info', 'servers', 'externalDocs', 'tags', 'security', 'paths', 'components']:
            if key in d:
                d2[key] = d[key]
        for key in d:
            if key not in d2:
                d2[key] = d[key]
        return d2

    @classmethod
    def get_api_by_slug(cls, slug):
        """
        Get one doc by slug
        """
        search = APIDoc.search().filter('term', _meta__slug=slug)
        count = search.count()

        if search.count() != 1:
            raise RegistryError(f"No exact matches for '{slug}' found: {count} results")

        hit = next(iter(search)).to_dict(include_meta=True)
        hit.update(hit.pop('_source'))
        hit.pop('_index')
        return cls.from_dict(hit)

    @staticmethod
    def get_all(fields=[], from_=0, size=10):
        """
        Returns a list of all docs in index.
        Each document is a dsl search hit that behaves like a dictionary
        """
        search = APIDoc.search()
        search = search[from_: from_ + size]

        if fields:
            search = search.source(includes=fields)
        return list(iter(search))

    @staticmethod
    def get_tags(field=None, size=100):
        """
        perform aggregations on given field
        Used to generate list of tags and authors

        Args:
            field (str, optional): field name of doc. Defaults to None.
            size (int, optional): size returned. Defaults to 100.

        Returns:
            list of tags/authors name:occurrence
        """
        agg_name = 'field_values'

        res = APIDoc.aggregate(field=field, size=size, agg_name=agg_name)
        return res.to_dict()

    @classmethod
    def get_api_id_from_slug(cls, slug):
        """
        Get doc ID for exact match of slug
        Used for loading metadata in Swagger UI
        """
        if not slug:
            raise RequestError('slug is required')

        search = APIDoc.search()
        search = search.filter('term', _meta__slug=slug)

        count = search.count()
        if not count == 1:
            raise RegistryError(f'Query for "{slug}" has {count} results')

        hit = next(iter(search)).to_dict(include_meta=True)
        hit.update(hit.pop('_source'))
        hit.pop('_index')
        return cls.from_dict(hit)["_id"]

    # PUT
    @classmethod
    def update_slug(cls, _id, slug_name=''):
        """
        Update API doc registered slug name
        """
        cls.validate_slug_name(slug_name)

        api_doc = APIDoc.get(_id)
        api_doc.update(refresh=True, _meta={"slug": slug_name.lower()})
        return slug_name.lower()

    @staticmethod
    def refresh_api(_id):
        """
        refresh the given API document object based on its saved metadata url
        """
        api_doc = APIDoc.get(_id)

        _meta = api_doc.to_dict().get('_meta', {})

        res = SchemaDownloader.download(_meta['url'])

        _meta['timestamp'] = dt.now().isoformat()
        res['_meta'] = _meta

        api_doc.update(refresh=True, **res)
        return f"API with ID {_id} was refreshed"

    # DELETE
    @staticmethod
    def delete(_id, user):
        """
        delete api with ID
        """
        doc = APIDoc.get(id=_id)
        _user = doc['_meta']['github_username']

        if user.get('login', None) != _user:
            raise RegistryError("User '{}' is not the owner of API '{}'".format(user.get('login', None), _id))

        doc.delete()
        return _id

class V3Metadata(SmartAPI):

    def __init__(self, metadata):
        """
        OAS3 V3 Metadata
        """
        super().__init__(metadata)
        self.version = 'v3'
        assert metadata['openapi'].split('.')[0] == "3"

    def encode_api_id(self, url):
        if not url:
            raise ValueError("Missing required _meta.url field.")
        return blake2b(url.encode('utf8'), digest_size=16).hexdigest()

    def convert_es(self):
        '''convert API metadata for ES indexing.'''
        _d = copy.copy(self._metadata)
        _d['_meta'] = self._meta
        # convert paths to a list of each path item
        _paths = []
        for path in _d.get('paths', []):
            _paths.append({
                "path": path,
                "pathitem": _d['paths'][path]
            })
        if _paths:
            _d['paths'] = _paths
        # include compressed binary raw metadata as "~raw"
        _raw = json.dumps(self._metadata).encode('utf-8')
        _raw = base64.urlsafe_b64encode(gzip.compress(_raw)).decode('utf-8')
        _d["~raw"] = _raw
        return _d

    def create_meta(self, url, user_name):
        """
        Document metadata added on convertion
        """
        self._meta = {
            "github_username": user_name,
            'url': url,
            'timestamp': dt.now().isoformat()
        }
        etag = SchemaDownloader.get_etag(self._meta['url'])
        if etag:
            self._meta['ETag'] = etag

    def validate(self):
        downloader = SchemaDownloader()

        downloader.register('openapi_v3', OAS3_SCHEMA_URL)
        downloader.register('x-translator', TRANSLATOR_URL)

        schemas = downloader.get_schemas()

        for name, schema in schemas.items():
            try:
                jsonschema.validate(self._metadata, schema)
            except jsonschema.ValidationError as e:
                err_msg = "Validation Error [{}]: '{}': {}".format(name, '.'.join([str(x) for x in e.path]), e.message)
                raise RegistryError(err_msg)
            except Exception as e:
                err_msg = "Unexpected Validation Error [{}]: {} - {}".format(name, type(e).__name__, e)
                raise RegistryError(err_msg)

    def save(self, api_doc, user_name=None, **options):
        """
        Save an OpenAPI V3 document

        Returns saved API ID
        """
        overwrite = options.get('overwrite')
        url = options.get('url')

        self.validate()

        api_id = self.encode_api_id(url)
        if self.exists(api_id) and not overwrite:
            raise RegistryError('API Exists')

        self.create_meta(url, user_name)
        doc = APIDoc(meta={'id': api_id}, ** self.convert_es())
        doc.save()
        return api_id

class V2Metadata(SmartAPI):

    def __init__(self, metadata):
        """
        Swagger V2 Metadata
        """
        super().__init__(metadata)
        self.version = 'v2'
        assert metadata['swagger'].split('.')[0] == "2"

    def encode_api_id(self, url):
        if not url:
            raise ValueError("Missing required _meta.url field.")
        return blake2b(url.encode('utf8'), digest_size=16).hexdigest()

    def validate(self):
        downloader = SchemaDownloader()

        downloader.register('openapi_v3', SWAGGER2_SCHEMA_URL)

        schemas = downloader.get_schemas()

        for name, schema in schemas.items():
            try:
                jsonschema.validate(self._metadata, schema)
            except jsonschema.ValidationError as e:
                err_msg = "Validation Error [{}]: '{}': {}".format(name, '.'.join([str(x) for x in e.path]), e.message)
                raise RegistryError(err_msg)
            except Exception as e:
                err_msg = "Unexpected Validation Error [{}]: {} - {}".format(name, type(e).__name__, e)
                raise RegistryError(err_msg)

    def convert_es(self):
        '''index Swagger compatible items'''
        # swagger 2 or other, only index limited fields
        _d = {"_meta": self._meta}
        for key in SWAGGER2_INDEXED_ITEMS:
            if key in self._metadata:
                _d[key] = self._metadata[key]
        # include compressed binary raw metadata as "~raw"
        _raw = json.dumps(self._metadata).encode('utf-8')
        _raw = base64.urlsafe_b64encode(gzip.compress(_raw)).decode('utf-8')
        _d["~raw"] = _raw
        return _d

    def create_meta(self, url, user_name):
        """
        Document metadata added on convertion
        """
        self._meta = {
            "github_username": user_name,
            'url': url,
            'timestamp': dt.now().isoformat(),
            'swagger_v2': True
        }
        etag = SchemaDownloader.get_etag(self._meta['url'])
        if etag:
            self._meta['ETag'] = etag

    def save(self, api_doc, user_name=None, **options):
        """
        Save a Swagger V2 document

        Returns saved API ID
        """
        save_v2 = options.get('save_v2')
        overwrite = options.get('overwrite')
        url = options.get('url')

        validation = self.validate()

        if not save_v2:
            raise RegistryError('API is Swagger V2 which is not fully suppported, set save_v2 = True')

        api_id = self.encode_api_id(url)

        if self.exists(api_id) and not overwrite:
            raise RegistryError('API Exists')

        self.create_meta(url, user_name)
        doc = APIDoc(meta={'id': api_id}, ** self.convert_es())
        doc.save()
        return api_id
