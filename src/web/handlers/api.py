
import json
import yaml

from biothings.web.handlers import BaseAPIHandler
from biothings.web.handlers.exceptions import BadRequest
from tornado.httpclient import HTTPError

from utils.slack_notification import send_slack_msg
from ..api.controller import (APIDocController, get_api_metadata_by_url, RegistryError)

def github_authenticated(func):
    '''
    RegistryHandler Decorator
    '''

    def _(self, *args, **kwargs):

        if not self.current_user:
            self.send_error(
                message='You must log in first.',
                status_code=401)
            return
        return func(self, *args, **kwargs)

    return _

class BaseHandler(BaseAPIHandler):

    def get_current_user(self):
        user_json = self.get_secure_cookie("user")
        if not user_json:
            return None
        return json.loads(user_json.decode('utf-8'))


class ValidateHandler(BaseHandler):

    kwargs = {
        'POST': {
            'url': {'type': str, 'default': None},
        },
    }
    name = "validator"

    def post(self):

        url = self.get_body_argument('url', None)
        data = None

        if url:
            try:
                data = get_api_metadata_by_url(url)
            except RegistryError as err:
                raise BadRequest(details=str(err))

        elif self.request.headers.get('Content-Type', '').startswith('application/json'):
            try:
                data = self.args_json
            except json.JSONDecodeError:
                raise BadRequest(details="Invalid JSON body")

        elif self.request.headers.get('Content-Type', '').startswith('application/yaml'):
            try:
                data = yaml.load(self.request.body, Loader=yaml.SafeLoader)
            except (yaml.scanner.ScannerError, yaml.parser.ParserError):
                raise BadRequest(details="The input request body does not contain valid API metadata")
        else:
            raise BadRequest(details="Need to provide data in the request body first")

        try:
            valid = APIDocController.validate(data)
        except RegistryError as err:
            raise BadRequest(details=str(err))
        else:
            self.finish(valid)


class APIHandler(BaseHandler):

    kwargs = {
        'GET': {
            'fields': {'type': list, 'default': []},
            'format': {'type': str, 'default': 'json'},
            'raw': {'type': bool, 'default': False},
            'meta': {'type': bool, 'default': False},
            '_from': {'type': int, 'default': 0},
            'size': {'type': int, 'default': 0},
        },
        'PUT': {
            'slug': {'type': str, 'default': ''},
            'refresh': {'type': bool, 'default': False},
        },
        'POST': {
            'url': {'type': str, 'default': None, 'required': True},
            'overwrite': {'type': bool, 'default': False},
            'dryrun': {'type': bool, 'default': False},
            'save_v2': {'type': bool, 'default': False},
        },
    }

    name = "api_handler"

    def get(self, api_name=None):
        """
        Get one API by ID or all

        Args:
            api_name (str): name of API doc requested

        Returns:
            JSON or YAML of doc requested
        """
        if api_name is None:
            res = APIDocController.get_all(
                fields=self.args.fields,
                from_=self.args._from,
                size=self.args.size)
        else:
            res = APIDocController.get_api(
                api_name=api_name,
                fields=self.args.fields,
                with_meta=self.args.meta,
                return_raw=self.args.raw,
                from_=self.args._from)

        self.format = self.args.out_format
        self.finish(res)

    @github_authenticated
    def post(self):
        """
        Add an API metadata doc
        """
        user = self.current_user
        url = self.args.url
        data = None

        try:
            data = get_api_metadata_by_url(url)
        except RegistryError as err:
            raise BadRequest(details=str(err))

        try:
            res = APIDocController.add(
                api_doc=data,
                user_name=user['login'],
                **self.args)

        except RegistryError as err:
            raise BadRequest(details=str(err))
        else:
            self.finish({'success': True, 'details': res})
            send_slack_msg(data, res, user['login'])

    @github_authenticated
    def put(self, _id):
        """
        Update/remove a slug if empty
        OR
        refresh document using url

        Args:
            slug: update with value or empty if 'deleting'
            refresh: determine operation / update doc by url
        """
        if not APIDocController.exists(_id):
            raise HTTPError(404, response='API does not exist')

        doc = APIDocController(_id=_id)

        if self.args.refresh is False:
            try:
                res = doc.update_slug(_id=_id, user=self.current_user, slug_name=self.args.slug)
            except RegistryError as err:
                raise BadRequest(details=str(err))
        else:
            try:
                res = doc.refresh_api(_id=_id, user=self.current_user, test=False)
            except RegistryError as err:
                raise BadRequest(details=str(err))

        self.finish({'success': True, 'details': res})

    @github_authenticated
    def delete(self, _id):
        """
        Delete API

        Args:
            _id: API id to be deleted permanently
        """
        if not APIDocController.exists(_id):
            raise HTTPError(404, response='API does not exist')

        doc = APIDocController(_id=_id)
        try:
            res = doc.delete(_id=_id, user=self.current_user)
        except RegistryError as err:
            raise BadRequest(details=str(err))

        self.finish({'success': True, 'details': res})


class ValueSuggestionHandler(BaseHandler):

    kwargs = {
        'GET': {
            'field': {'type': str, 'default': None, 'required': True},
            'size': {'type': int, 'default': 100},
        },
    }

    name = 'value_suggestion'

    def get(self):
        """
        /api/suggestion?field=
        Returns aggregations for any field provided
        Used for tag:count on registry
        """
        res = APIDocController.get_tags(self.args.field, self.args.size)
        self.finish(res)
