import logging
import time
from contextlib import contextmanager
from typing import Iterable, Type

from opensearchpy import OpenSearch, helpers

from .config import AWSOpenSearchConfig, AWSOpenSearchIndexConfig, AWSOS_Engine
from ..api import VectorDB, IndexType, MetricType

log = logging.getLogger(__name__)


class AWSOpenSearch(VectorDB):
    def __init__(
            self,
            dim: int,
            db_config: dict,
            db_case_config: AWSOpenSearchIndexConfig,
            index_name: str = "vdb_bench_index",  # must be lowercase
            id_col_name: str = "id",
            vector_col_name: str = "embedding",
            drop_old: bool = False,
            **kwargs,
    ):
        self.client: OpenSearch | None = None
        self.dim = dim
        self.db_config = db_config
        self.case_config = db_case_config
        self.index_name = index_name
        self.id_col_name = id_col_name
        self.category_col_names = [
            f"scalar-{categoryCount}" for categoryCount in [2, 5, 10, 100, 1000]
        ]
        self.vector_col_name = vector_col_name

        log.info(f"AWS_OpenSearch client config: {self.db_config}")
        client = OpenSearch(**self.db_config)
        if drop_old:
            log.info(f"AWS_OpenSearch client drop old index: {self.index_name}")
            is_existed = client.indices.exists(index=self.index_name)
            if is_existed:
                client.indices.delete(index=self.index_name)
            self._create_index(client)

    @classmethod
    def config_cls(cls) -> Type[AWSOpenSearchConfig]:
        return AWSOpenSearchConfig

    @classmethod
    def case_config_cls(
            cls, index_type: IndexType | None = None
    ) -> Type[AWSOpenSearchIndexConfig]:
        return AWSOpenSearchIndexConfig

    def _create_index(self, client: OpenSearch):
        number_of_shards = 5

        settings = {
            "index": {
                "knn": True,
                "refresh_interval": "-1",
                "number_of_replicas": 0,
                "number_of_shards": number_of_shards,
            }
        }
        mappings = {
            "properties": {
                self.id_col_name: {"type": "integer"},
                **{
                    categoryCol: {"type": "keyword"}
                    for categoryCol in self.category_col_names
                },
                self.vector_col_name: {
                    "type": "knn_vector",
                    "dimension": self.dim,
                    "method": self.case_config.index_param()
                },
            }
        }
        try:
            client.indices.create(
                index=self.index_name, body=dict(settings=settings, mappings=mappings)
            )
        except Exception as e:
            log.warning(f"Failed to create index: {self.index_name} error: {str(e)}")
            raise e from None

    @contextmanager
    def init(self) -> None:
        """connect to elasticsearch"""
        self.client = OpenSearch(**self.db_config)

        yield
        # self.client.transport.close()
        self.client = None
        del self.client

    def insert_embeddings(
            self,
            embeddings: Iterable[list[float]],
            metadata: list[int],
            **kwargs,
    ) -> tuple[int, Exception | None]:
        """Insert the embeddings to the elasticsearch."""
        assert self.client is not None, "should self.init() first"

        insert_data = []
        count = 0

        for i, v in enumerate(embeddings):
            insert_data.append({"_index": self.index_name, "_id": metadata[i], self.vector_col_name: v})
            count += 1

        try:
            log.info(f"AWS_OpenSearch adding {count} documents")

            succeeded = []
            failed = []
            for success, item in helpers.parallel_bulk(self.client, actions=insert_data, thread_count=8, queue_size=16):
                if success:
                    succeeded.append(item)
                else:
                    failed.append(item)

            log.info(f"AWS_OpenSearch added documents: {len(succeeded)}")
            resp = self.client.indices.stats(self.index_name)
            log.info(f"Total document count in index: {resp['_all']['primaries']['indexing']['index_total']}")
            return count, None
        except Exception as e:
            log.warning(f"Failed to insert data: {self.index_name} error: {str(e)}")
            time.sleep(10)
            return self.insert_embeddings(embeddings, metadata)

    def search_embedding(
            self,
            query: list[float],
            k: int = 100,
            filters: dict | None = None,
    ) -> list[int]:
        """Get k most similar embeddings to query vector.

        Args:
            query(list[float]): query embedding to look up documents similar to.
            k(int): Number of most similar embeddings to return. Defaults to 100.
            filters(dict, optional): filtering expression to filter the data while searching.

        Returns:
            list[tuple[int, float]]: list of k most similar embeddings in (id, score) tuple to the query embedding.
        """
        assert self.client is not None, "should self.init() first"

        body = {
            "size": k,
            "query": {"knn": {self.vector_col_name: {"vector": query, "k": k}}},
            "_source": {
                "exclude": self.vector_col_name
            }
        }
        try:
            resp = self.client.search(index=self.index_name, body=body)
            log.info(f'Search took: {resp["took"]}')
            log.info(f'Search shards: {resp["_shards"]}')
            log.info(f'Search hits total: {resp["hits"]["total"]}')
            result = [int(d["_id"]) for d in resp["hits"]["hits"]]
            # log.info(f'success! length={len(res)}')

            return result
        except Exception as e:
            log.warning(f"Failed to search: {self.index_name} error: {str(e)}")
            raise e from None

    def optimize(self):
        """optimize will be called between insertion and search in performance cases."""
        assert self.client is not None, "should self.init() first"

        # Force merge get reduce number of segments and reduce latency.
        # WARNING: This is slow and the performance test may time out.
        #self.client.transport.perform_request("POST", f"/{self.index_name}/_forcemerge?max_num_segments=1")

        # Enable Concurrent Segment Search if supported
        #self.client.cluster.put_settings(body={"persistent": {"search.concurrent_segment_search.enabled": True}})

        #
        self.client.transport.perform_request(
            "PUT", f"/{self.index_name}/_settings",
            body={"index": {"refresh_interval": "60s", "number_of_replicas": 1}}
        )

        self.client.transport.perform_request("POST", f"/{self.index_name}/_refresh")

        # Warm up the index
        self.client.transport.perform_request(
            "GET", f"/_plugins/_knn/warmup/{self.index_name}")

    def ready_to_load(self):
        """ready_to_load will be called before load in load cases."""
        pass

    def need_normalize_cosine(self) -> bool:
        engine = self.case_config.engine
        metric = self.case_config.metric_type
        if engine == AWSOS_Engine.faiss and metric == MetricType.COSINE:
            return True
        else:
            return False
