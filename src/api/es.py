import json
from datetime import date, datetime

from elasticsearch import Elasticsearch, RequestError, helpers

from .transform import APIMetadata, decode_raw, get_api_metadata_by_url


ES_HOST = 'localhost:9200'
ES_INDEX_NAME = 'smartapi_oas3'
ES_DOC_TYPE = 'api'


def ask(prompt, options='YN'):
    '''Prompt Yes or No,return the upper case 'Y' or 'N'.'''
    options = options.upper()
    while 1:
        s = input(prompt+'[%s]' % '|'.join(list(options))).strip().upper()
        if s in options:
            break
    return s


def get_datestamp():
    d = date.today()
    return d.strftime('%Y%m%d')


def get_es(es_host=None):
    es_host = es_host or ES_HOST
    es = Elasticsearch(es_host, timeout=120)
    return es


def create_index(index_name=None, es=None):
    index_name = index_name or ES_INDEX_NAME
    body = {}
    mapping = {
        "api": {
            "dynamic_templates": [
                {
                    "ignore_example_field": {
                        "match": "example",
                        "mapping": {
                            "index": "no"
                        }
                    }
                },
                {
                    "ignore_ref_field": {
                        "match": "$ref",
                        "mapping": {
                            "index": "no"
                        }
                    }
                },

                # this must be the last template
                {
                    "template_1": {
                        "match": "*",
                        "match_mapping_type": "string",
                        "mapping": {
                            "type": "string",
                            "index": "analyzed",
                            "fields": {
                                "raw": {
                                    "type": "string",
                                    "index": "not_analyzed"
                                }
                            }
                        }
                    }
                }
            ],
            "properties": {
                "components": {
                    "enabled": False
                },
                "~raw": {
                    "type": "binary"
                }
            }
        }
    }
    mapping = {"mappings": mapping}
    body.update(mapping)
    _es = es or get_es()
    print(_es.indices.create(index=index_name, body=body))


# def _encode_api_object_id(api_doc):
    # info_d = api_doc.get('info', {})
    # api_title, api_version = info_d.get('title', ''), info_d.get('version', '')
    # api_contact = info_d.get('contact', {})
    # api_contact = api_contact.get('name', '')
    # if not (api_title and api_version and api_contact):
    #     raise ValueError("Missing required info fields.")
    # x = json.dumps((api_title, api_version, api_contact))
    # return blake2b(x.encode('utf8'), digest_size=16).hexdigest()


def _get_hit_object(hit):
    obj = hit.get('fields', hit.get('_source', {}))
    if '_id' in hit:
        obj['_id'] = hit['_id']
    return obj


class ESQuery():
    def __init__(self, index=None, doc_type=None, es_host=None):
        self._es = get_es(es_host)
        self._index = index or ES_INDEX_NAME
        self._doc_type = doc_type or ES_DOC_TYPE

    def exists(self, api_id):
        '''return True/False if the input api_doc has existing metadata
           object in the index.
        '''
        return self._es.exists(index=self._index, doc_type=self._doc_type, id=api_id)

    def save_api(self, api_doc, overwrite=False, dryrun=False):
        metadata = APIMetadata(api_doc)
        valid = metadata.validate()
        if not valid['valid']:
            valid['success'] = False
            return valid

        api_id = metadata.encode_api_id()
        doc_exists = self.exists(api_id)
        if doc_exists and not overwrite:
            return {"success": False, "error": "API exists. Not saved."}
        _doc = metadata.convert_es()
        if dryrun:
            return {"success": True, '_id': "this is a dryrun. API is not saved.", "dryrun": True}
        try:
            self._es.index(index=self._index, doc_type=self._doc_type, body=_doc, id=api_id)
        except RequestError as e:
            return {"success": False, "error": str(e)}
        return {"success": True, '_id': api_id}

    def _get_api_doc(self, api_doc, with_meta=True):
        doc = decode_raw(api_doc.get('~raw', ''))
        if with_meta:
            doc["_meta"] = api_doc.get('_meta', {})
            doc["_id"] = api_doc["_id"]
        return doc

    def get_api(self, api_name, fields=None, with_meta=True, return_raw=False, size=None, from_=0):
        if api_name == 'all':
            query = {'query': {"match_all": {}}}
        else:
            query = {
                "query": {
                    "match": {
                        "_id": {
                            "query": api_name
                        }
                    }
                }
            }
        if fields and fields not in ["all", ["all"]]:
            query["_source"] = fields
        if size and isinstance(size, int):
            query['size'] = min(size, 100)    # set max size to 100 for now.
        if from_ and isinstance(from_, int) and from_ > 0:
            query['from'] = from_
        res = self._es.search(self._index, self._doc_type, query)
        if return_raw == '2':
            return res
        res = [_get_hit_object(d) for d in res['hits']['hits']]
        if not return_raw:
            try:
                res = [self._get_api_doc(x, with_meta=with_meta) for x in res]
            except ValueError as e:
                res = {'success': False, 'error': str(e)}
        if len(res) == 1:
            res = res[0]
        return res

    def query_api(self, q, filters=None, fields=None, return_raw=True, size=None, from_=0):
        # query = {
        #     "query":{
        #         "match" : {
        #             attr: {
        #                 "query": q
        #             }
        #         }
        #     }
        # }
        try:
            query = json.loads(q)
            assert isinstance(query, dict)
            is_raw_query = True
        except (ValueError, AssertionError):
            is_raw_query = False

        if not is_raw_query:
            if q == '__all__':
                query = {
                    "query": {
                        "match_all": {}
                    }
                }
            else:
                query = {
                    "query": {
                        "dis_max": {
                            "queries": [
                                {
                                    "term": {
                                        "info.title": q,
                                        "boost": 2
                                    }
                                },
                                {
                                    "term": {
                                        "server.url": q,
                                        "boost": 1.1
                                    }
                                },
                                {
                                    "term": {
                                        "_id": q,
                                    }
                                },
                                {
                                    "query_string": {
                                        "query": q
                                    }
                                },
                                {
                                    "query_string": {
                                        "query": q + "*",
                                        "boost": 0.8
                                    }
                                },
                            ]
                        }
                    }
                }
                # query = {
                #     "query": {
                #         "query_string": {
                #             "query": q
                #         }
                #     }
                # }

        if filters:
            query = {
                "query": {
                    "bool": {
                        "must": query["query"],
                        "filter": {
                            "terms": filters
                        }
                    }
                }
            }

        if not fields or fields == 'all':
            pass
        else:
            query['_source'] = fields
        if size and isinstance(size, int):
            query['size'] = min(size, 100)    # set max size to 100 for now.
        if from_ and isinstance(from_, int) and from_ > 0:
            query['from'] = from_
        # else:
        #     query['_source'] = ['@id', attr_input, attr_output]
        # print(query)
        res = self._es.search(self._index, self._doc_type, body=query)
        if not return_raw:
            _res = res['hits']
            _res['took'] = res['took']
            if "aggregations" in res:
                _res['aggregations'] = res['aggregations']
            for v in _res['hits']:
                del v['_type']
                del v['_index']
                for attr in ['fields', '_source']:
                    if attr in v:
                        v.update(v[attr])
                        del v[attr]
                        break
            res = _res
        return res

    def _do_aggregations(self, _field, agg_name, size):
        query = {
            "aggs": {
                agg_name: {
                    "terms": {
                        "field": _field,
                        "size": size
                    }
                }
            }
        }
        res = self._es.search(self._index, self._doc_type, query, size=0)
        res = res["aggregations"]
        return res

    def value_suggestion(self, field, size=100, use_raw=True):
        """return a list of existing values for the given field."""
        _field = field + ".raw" if use_raw else field
        agg_name = 'field_values'
        res = self._do_aggregations(_field, agg_name, size)
        if use_raw and not res[agg_name]['buckets']:
            # if *.raw does not return any buckets, try without it.
            res = self._do_aggregations(field, agg_name, size)

        return res

    def delete_api(self, id):
        """delete a saved API metadata, be careful with the deletion."""
        if ask("Are you sure to delete this API metadata?") == 'Y':
            print(self._es.delete(index=self._index, doc_type=self._doc_type, id=id))

    def fetch_all(self, as_list=False, id_list=[]):
        """return a generator of all docs from the ES index.
            return a list instead if as_list is True.
            if id_list is passed, it returns only docs from the given ids.
        """
        if id_list:
            query = {"query": {"ids": {"type": ES_DOC_TYPE, "values": id_list}}}
        else:
            query = {"query": {"match_all": {}}}
        scan_res = helpers.scan(client=self._es, query=query,
                                index=self._index, doc_type=self._doc_type)

        def _fn(x):
            x['_source'].setdefault('_id', x['_id'])
            return x['_source']
        doc_iter = (_fn(x) for x in scan_res)    # return docs only
        if as_list:
            return list(doc_iter)
        else:
            return doc_iter

    def backup_all(self, outfile=None):
        """back up all docs into a output file."""
        # get the real index name in case self._index is an alias
        alias_d = self._es.indices.get_alias(self._index)
        assert len(alias_d) == 1
        index_name = list(alias_d.keys())[0]
        outfile = outfile or "{}_backup_{}.json".format(index_name, get_datestamp())
        out_f = open(outfile, 'w')
        doc_li = self.fetch_all(as_list=True)
        json.dump(doc_li, out_f, indent=2)
        out_f.close()
        print("Backed up {} docs in \"{}\".".format(len(doc_li), outfile))

    def restore_all(self, backupfile, index_name):
        """restore all docs from the backup file to a new index.
           must restore to a new index, cannot overwrite an existing one.
        """
        if self._es.indices.exists(index_name):
            print("Error: index \"{}\" exists. Try a different index_name.".format(index_name))
            return

        print("Loading docs from \"{}\"...".format(backupfile))
        in_f = open(backupfile)
        doc_li = json.load(in_f)
        print("Done. [{}]".format(len(doc_li)))

        print("Creating index...")
        create_index(index_name, es=self._es)
        print("Done.")

        print("Indexing...")
        for _doc in doc_li:
            _id = _doc.pop('_id')
            self._es.index(index=index_name, doc_type=self._doc_type, body=_doc, id=_id)
        print("Done.")

    def refresh_all(self, id_list=[], dryrun=True, return_status=False):
        '''refresh API metadata based on the saved metadata urls.'''
        success_cnt = 0
        total_cnt = 0
        if return_status:
            status_li = []
        print("Refreshing API metadata:")
        for api_doc in self.fetch_all(id_list=id_list):
            _id = api_doc['_id']
            _meta = api_doc['_meta']
            print("\t{}...".format(_id))
            new_api_doc = get_api_metadata_by_url(_meta['url'])
            if new_api_doc and isinstance(new_api_doc, dict):
                if new_api_doc.get('success', None) is False:
                    status = new_api_doc
                else:
                    _meta['timestamp'] = datetime.now().isoformat()
                    new_api_doc['_meta'] = _meta
                    status = self.save_api(new_api_doc, overwrite=True, dryrun=dryrun)
            else:
                status = {'success': False, 'error': 'Invalid input data.'}

            if status.get('success', False):
                print("Success.")
                success_cnt += 1
            else:
                print("Failed.")
            total_cnt += 1
            if return_status:
                status_li.append((_id, status))
        print("="*25)
        print("Successfully refreshed {}/{} API meteadata.".format(success_cnt, total_cnt))
        if dryrun:
            print("This is a dryrun! No actual changes have been made.")
            print("When ready, run it again with \"dryrun=False\" to apply actual changes.")
        if return_status:
            return status_li
