import bz2
#from bert_serving.client import BertClient
from client.elastic_client import ElasticClient
import time
from data_utils import parse_dbpedia_data, SearchEngine, EmbeddingModel, enrich_doc_with_vectors, \
    parse_gsi_and_dbpedia_data

USE_PRECOMPUTED_VECTORS = True
bc = None

#if not USE_PRECOMPUTED_VECTORS:
#    print("Initializing BERT client")
#    bc = BertClient()

print("Initializing Elastic client")
ec = ElasticClient(configs_dir='es_conf')

# Read the input compressed file as is, without decompressing.
# Though disks are cheap, isn't it great to save them?
input_file = 'data/dbpedia/long_abstracts_en.ttl.bz2'
print("Reading BZ2 file")
source_file = bz2.BZ2File(input_file, "r")

# Change this constant to vary the number of indexed abstracts
# set to -1 to index all
MAX_DOCS = 1000000

if __name__ == '__main__':
    print("parsing and indexing abstracts with BERT based vectors...")
    start_time = time.time()
    index_spec = "vector"
    index_name = index_spec + '_' + str(MAX_DOCS)
    ec.delete_index(index_name)
    ec.create_index(index_name, index_spec)

    if USE_PRECOMPUTED_VECTORS:
        print("Using precomputed vectors")
        docs_iter = parse_gsi_and_dbpedia_data(
            source_file,
            '/home/ubuntu/data/1000000_EmbeddingModel.HUGGING_FACE_SENTENCE_vectors.npy',
            '/home/ubuntu/data/1000000_EmbeddingModel.HUGGING_FACE_SENTENCE_vectors_docids.pkl',
            MAX_DOCS
        )
    else:
        print("Computing vectors from scratch")
        docs_iter = parse_dbpedia_data(source_file, MAX_DOCS)
        docs_iter = enrich_doc_with_vectors(docs_iter, EmbeddingModel.BERT_UNCASED_768, bc, SearchEngine.ELASTICSEARCH)

    ec.index_documents(index_name, docs_iter)
    end_time = time.time()
    print("All done. Took: {} seconds".format(end_time-start_time))
