import os
import requests
import json
import elasticsearch.helpers
from elasticsearch import Elasticsearch

from client.base_client import BaseClient


class ElasticResp:
    def __init__(self, resp):
        self.status_code = 400
        if 'acknowledged' in resp and resp['acknowledged']:
            print("request acknowledged!")
            self.status_code = 200
        else:
            self.status_code = resp['status']
            self.text = json.dumps(resp, indent=2)


class BulkResp():
    def __init__(self, resp):
        self.status_code = 400
        if resp[0] > 0:
            self.status_code = 201


class SearchResp():
    def __init__(self, resp):
        self.status_code = 400
        if 'hits' in resp:
            self.status_code = 200
        else:
            self.status_code = resp['status']
            self.text = json.dumps(resp, indent=2)


class ElasticClient(BaseClient):
    """ Note on the Elastic client,
        Elastic LTR is not bound to an index like Solr LTR
        so many calls take an index but do not use it

        In the future, we may wish to isolate an Index's feature
        store to a feature store of the same name of the index
    """
    def __init__(self, host=None, configs_dir='.', https=False):
        self.docker = os.environ.get('LTR_DOCKER') != None
        self.configs_dir = configs_dir #location of elastic configs

        # respect host if it is set
        if host is not None:
            self.host = host
        else:
            if self.docker:
                self.host = 'elastic'
            else:
                self.host = 'localhost'

        if https:
            self.protocol = "https"
        else:
            self.protocol = "http"

        self.elastic_ep = '{}://{}:9200/_ltr'.format(self.protocol, self.host)
        self.es = Elasticsearch('{}://{}:9200'.format(self.protocol, self.host))

    def get_host(self):
        return self.host

    def name(self):
        return "elastic"

    def resp_msg(self, msg, resp, throw=True):
        print('{} [Status: {}]'.format(msg, resp.status_code))
        if resp.status_code >= 400:
            print(resp.text)
            if throw:
                raise RuntimeError(resp.text)

    def delete_index(self, index):
        resp = self.es.indices.delete(index=index, ignore=[400, 404], ignore_unavailable=True)
        self.resp_msg("Deleted index {}".format(index), ElasticResp(resp))

    def create_index(self, index_name, index_spec):
        """ Take the local config files for Elasticsearch for index, reload them into ES"""
        cfg_json_path = os.path.join(self.configs_dir, "%s_settings.json" % index_spec)
        with open(cfg_json_path) as src:
            settings = json.load(src)
            resp = self.es.indices.create(index_name, body=settings)
            print("create_index: resp={}".format(resp))
            self.resp_msg("Created index {}".format(index_name), ElasticResp(resp))

    def index_documents(self, index, doc_src):

        def bulkDocs(doc_src):
            for doc in doc_src:
                if 'id' not in doc:
                    raise ValueError("Expecting docs to have field 'id' that uniquely identifies document")
                addCmd = {"_index": index,
                          "_id": doc['id'],
                          "_source": doc}
                yield addCmd

        resp = elasticsearch.helpers.bulk(self.es, bulkDocs(doc_src), chunk_size=100)
        self.es.indices.refresh(index=index)
        self.resp_msg(msg="Streaming Bulk index DONE {}".format(index), resp=BulkResp(resp))

    def reset_ltr(self):
        resp = requests.delete(self.elastic_ep)
        self.resp_msg(msg="Removed Default LTR feature store".format(), resp=resp, throw=False)
        resp = requests.put(self.elastic_ep)
        self.resp_msg(msg="Initialize Default LTR feature store".format(), resp=resp)

    def create_featureset(self, name, ftr_config):
        resp = requests.post('{}/_featureset/{}'.format(self.elastic_ep, name), json=ftr_config)
        self.resp_msg(msg="Create {} feature set".format(name), resp=resp)

    def log_query(self, index, featureset, ids, params={}):
        params = {
            "query": {
                "bool": {
                    "filter": [
                        {
                            "sltr": {
                                "_name": "logged_features",
                                "featureset": featureset,
                                "params": params
                            }
                        }
                    ]
                }
            },
            "ext": {
                "ltr_log": {
                    "log_specs": {
                        "name": "ltr_features",
                        "named_query": "logged_features"
                    }
                }
            },
            "size": 1000
        }

        terms_query = [
            {
                "terms": {
                    "_id": ids
                }
            }
        ]

        if ids is not None:
            params["query"]["bool"]["must"] = terms_query

        resp = self.es.search(index=index, body=params)
        self.resp_msg(msg="Searching {} - {}".format(index, str(terms_query)[:20]), resp=SearchResp(resp))

        matches = []
        for hit in resp['hits']['hits']:
            hit['_source']['ltr_features'] = []

            for feature in hit['fields']['_ltrlog'][0]['ltr_features']:
                value = 0.0
                if 'value' in feature:
                    value = feature['value']

                hit['_source']['ltr_features'].append(value)

            matches.append(hit['_source'])

        return matches

    def submit_model(self, featureset, model_name, model_payload):
        model_ep = "{}/_model/".format(self.elastic_ep)
        create_ep = "{}/_featureset/{}/_createmodel".format(self.elastic_ep, featureset)

        resp = requests.delete('{}{}'.format(model_ep, model_name))
        print('Delete model {}: {}'.format(model_name, resp.status_code))

        resp = requests.post(create_ep, json=model_payload)
        self.resp_msg(msg="Created Model {}".format(model_name), resp=resp)

    def submit_ranklib_model(self, featureset, index, model_name, model_payload):
        params = {
            'model': {
                'name': model_name,
                'model': {
                    'type': 'model/ranklib',
                    'definition': model_payload
                }
            }
        }
        self.submit_model(featureset, index, model_name, params)

    def model_query(self, index, model, model_params, query):
        params = {
            "query": query,
            "rescore": {
                "window_size": 1000,
                "query": {
                    "rescore_query": {
                        "sltr": {
                            "params": model_params,
                            "model": model
                        }
                    }
                }
            },
            "size": 1000
        }

        resp = self.es.search(index=index, body=params)
        self.resp_msg(msg="Searching {} - {}".format(index, str(query)[:20]), resp=SearchResp(resp))

        # Transform to consistent format between ES/Solr
        matches = []
        for hit in resp['hits']['hits']:
            matches.append(hit['_source'])

        return matches

    def query(self, index, query):
        print("query:{}".format(query))
        resp = self.es.search(index=index, body=query)
        self.resp_msg(msg="Searching {} - {}".format(index, str(query)[:20]), resp=SearchResp(resp))

        #print("================ ES response")
        print(resp)

        # Transform to consistent format between ES/Solr
        matches = []
        for hit in resp['hits']['hits']:
            hit['_source']['_score'] = hit['_score']
            matches.append(hit['_source'])



        return matches, resp['took'], resp['hits']['total']['value']

    def feature_set(self, index, name):
        resp = requests.get('{}/_featureset/{}'.format(self.elastic_ep,
                                                      name))

        jsonResp = resp.json()
        if not jsonResp['found']:
            raise RuntimeError("Unable to find {}".format(name))

        self.resp_msg(msg="Fetched FeatureSet {}".format(name), resp=resp)

        rawFeatureSet = jsonResp['_source']['featureset']['features']

        mapping = []
        for feature in rawFeatureSet:
            mapping.append({'name': feature['name']})

        return mapping, rawFeatureSet

    def get_doc(self, doc_id, index):
        resp = self.es.get(index=index, id=doc_id)
        #self.resp_msg(msg="Fetched Doc".format(docId), resp=ElasticResp(resp), throw=False)
        return resp['_source']
