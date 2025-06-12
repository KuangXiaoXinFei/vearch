#
# Copyright 2019 The Vearch Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.

# -*- coding: UTF-8 -*-

import requests
import json
import os
import pytest
from minio import Minio
from minio.error import S3Error
from utils.vearch_utils import *
from utils.data_utils import *
import zstandard as zstd


__description__ = """ test case for module backup """


class TestBackup:
    @pytest.fixture(autouse=True)
    def setup_class(self):

        sift10k = DatasetSift10K()
        self.xb = sift10k.get_database()
        self.xq = sift10k.get_queries()
        self.gt = sift10k.get_groundtruth()
        self.db_name = db_name
        self.space_name = space_name

        self.endpoint = os.getenv("S3_ENDPOINT", "127.0.0.1:10000")
        self.access_key = os.getenv("S3_ACCESS_KEY", "minioadmin")
        self.secret_key = os.getenv("S3_SECRET_KEY", "minioadmin")
        self.use_ssl_str = os.getenv("S3_USE_SSL", "False")
        self.secure = self.use_ssl_str.lower() in ['true', '1']
        self.region = os.getenv("S3_REGION", "")
        self.cluster_name = os.getenv("CLUSTER_NAME", "vearch")

    def backup(self, router_url, command, corrupted=False, error_param=False):
        url = router_url + "/backup/dbs/" + self.db_name + "/spaces/" + self.space_name

        data = {
            "command": command,
            "backup_id": 1,
            "s3_param": {
                "access_key": os.getenv("S3_ACCESS_KEY", "minioadmin"),
                "secret_key": os.getenv("S3_SECRET_KEY", "minioadmin"),
                "bucket_name": os.getenv("S3_BUCKET_NAME", "test"),
                "endpoint": os.getenv("S3_ENDPOINT", "minio:9000"),
                "use_ssl": self.use_ssl_str.lower() in ['true', '1']
            },
        }
        if error_param:
            data["s3_param"]["access_key"] = "error_key"
            response = requests.post(url, auth=(username, password), json=data)
            assert response.status_code != 0

            data["s3_param"]["access_key"] = os.getenv("S3_ACCESS_KEY", "minioadmin")
            data["s3_param"]["secret_key"] = "error_key"
            response = requests.post(url, auth=(username, password), json=data)
            assert response.status_code != 0

            data["s3_param"]["secret_key"] = os.getenv("S3_SECRET_KEY", "minioadmin")
            data["s3_param"]["bucket_name"] = "error_bucket"
            response = requests.post(url, auth=(username, password), json=data)
            assert response.status_code != 0

            data["s3_param"]["bucket_name"] = os.getenv("S3_BUCKET_NAME", "test")
            data["s3_param"]["endpoint"] = "error_endpoint"
            response = requests.post(url, auth=(username, password), json=data)
            assert response.status_code != 0

            data["s3_param"]["endpoint"] = os.getenv("S3_ENDPOINT", "minio:9000")

            # test db not exist
            url = router_url + "/backup/dbs/" + "err_db" + "/spaces/" + self.space_name
            response = requests.post(url, auth=(username, password), json=data)
            assert response.status_code != 0

            # test space not exist
            url = router_url + "/backup/dbs/" + self.db_name + "/spaces/error_space"
            response = requests.post(url, auth=(username, password), json=data)
            assert response.status_code != 0

            data["s3_param"] = {}
            response = requests.post(url, auth=(username, password), json=data)
            assert response.status_code != 0
            return

        url = router_url + "/backup/dbs/" + self.db_name + "/spaces/" + self.space_name
        response = requests.post(url, auth=(username, password), json=data)

        assert response.json()["code"] == 0
        return response.json()["data"]["backup_id"]

    def create_db(self, router_url):
        response = create_db(router_url, self.db_name)
        logger.info(response.json())

    def create_space(self, router_url, embedding_size, store_type="MemoryOnly"):
        properties = {}
        properties["fields"] = [
            {
                "name": "field_int",
                "type": "integer",
            },
            {
                "name": "field_vector",
                "type": "vector",
                "dimension": embedding_size,
                "store_type": store_type,
                "index": {
                    "name": "gamma",
                    "type": "FLAT",
                    "params": {
                        "metric_type": "L2",
                    }
                },
            }
        ]

        space_config = {
            "name": space_name,
            "partition_num": 1,
            "replica_num": 1,
            "fields": properties["fields"]
        }

        response = create_space(router_url, self.db_name, space_config)
        logger.info(response.json())

    def query(self, parallel_on_queries, k):
        query_dict = {
            "vectors": [],
            "index_params": {
                "parallel_on_queries": parallel_on_queries
            },
            "vector_value": False,
            "fields": ["field_int"],
            "limit": k,
            "db_name": self.db_name,
            "space_name": self.space_name,
        }

        for batch in [True, False]:
            avarage, recalls = evaluate(self.xq, self.gt, k, batch, query_dict)
            result = "batch: %d, parallel_on_queries: %d, avarage time: %.2f ms, " % (
                batch, parallel_on_queries, avarage)
            for recall in recalls:
                result += "recall@%d = %.2f%% " % (recall, recalls[recall] * 100)
            logger.info(result)

            assert recalls[1] >= 0.95
            assert recalls[10] >= 1.0

    def compare_doc(self, doc1, doc2):
        logger.debug("doc1: " + json.dumps(doc1))
        logger.debug("doc2: " + json.dumps(doc2))
        return doc1["_id"] == doc2["_id"] and doc1["field_int"] == doc2["field_int"] and doc1["field_vector"] == doc2["field_vector"]

    def waiting_backup_finish(self, timewait=5):
        url = router_url + "/dbs/" + self.db_name + "/spaces/" + self.space_name
        response = requests.get(url, auth=(username, password))
        partitions = response.json()["data"]["partitions"]
        backup_status = 0
        for p in partitions:
            if p["backup_status"] != 0:
                backup_status = p["backup_status"]
        if backup_status != 0:
            time.sleep(timewait)

    def decompress_file(self, input_path, output_path):
        with open(input_path, 'rb') as input_file:
            with open(output_path, 'wb') as output_file:
                decompressor = zstd.ZstdDecompressor()
                decompressor.copy_stream(input_file, output_file)

    def download_data_files_and_upsert(self, bucket_name, backup_id="", local_directory="data"):
        client = Minio(
            self.endpoint,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=self.secure,
            region=self.region,
        )

        try:
            logger.info(f"Bucket {bucket_name} exists: {client.bucket_exists(bucket_name)} backup_id {backup_id}")
            objects = client.list_objects(bucket_name, prefix=f"{self.cluster_name}/export/ts_db/ts_space/{backup_id}", recursive=True)
            
            for obj in objects:
                logger.info(f"Object name: {obj.object_name}")
                if obj.object_name.endswith('.zst'):
                    local_file_path = f"{local_directory}/{obj.object_name.split('/')[-1]}"
                    client.fget_object(bucket_name, obj.object_name, local_file_path)
                    # uncompress the file
                    uncompressed_file_path = local_file_path.replace('.zst', '.txt')
                    self.decompress_file(local_file_path, uncompressed_file_path)
                    # read the uncompressed file and upsert data
                    with open(uncompressed_file_path, 'r') as f:
                        lines = f.readlines()
                        for line in lines:
                            data = json.loads(line)
                            doc = {
                                "db_name": self.db_name,
                                "space_name": self.space_name,
                                "documents": [data],
                            }
                            rs = requests.post(router_url + "/document/upsert", auth=(username, password), json=doc)
                            if rs.status_code != 200:
                                logger.error(f"Error occurred: {rs.json()}")
                                assert rs.status_code == 200
                            
                    # delete file
                    os.remove(local_file_path)
                    os.remove(uncompressed_file_path)
        except S3Error as err:
            logger.error(f"Error occurred: {err}")

    def remove_oss_files(self, prefix, recursive=False):
        """
        Remove an object or recursively delete all objects with a given prefix
        
        Args:
            prefix (str): Object name or prefix to delete
            recursive (bool): If True, delete all objects starting with prefix
        """
        bucket_name = os.getenv("S3_BUCKET_NAME", "test")
        client = Minio(
            self.endpoint,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=self.secure,
            region=self.region,
        )

        try:
            found = client.bucket_exists(bucket_name)
            logger.info(f"Bucket {bucket_name} exists: {found}")
            
            if recursive:
                # List all objects with the prefix
                objects = client.list_objects(bucket_name, prefix=prefix, recursive=True)
                object_names = []
                for obj in objects:
                    object_names.append(obj.object_name)
                    
                if not object_names:
                    logger.info(f"No objects found with prefix '{prefix}' in bucket '{bucket_name}'")
                    return

                for name in object_names:
                    try:
                        client.remove_object(bucket_name, name)
                        logger.debug(f"Deleted object '{name}'")
                    except Exception as e:
                        logger.error(f"Error removing object '{name}': {e}")
                
                logger.info(f"Successfully processed {len(object_names)} objects with prefix '{prefix}'")
            else:
                # Delete single object
                client.remove_object(bucket_name, prefix)
                logger.info(f"Object '{prefix}' deleted successfully")
                
        except S3Error as err:
            logger.error(f"Error occurred: {err} bucket_name {bucket_name} secure {self.secure} endpoint {self.endpoint}")

    def benchmark(self, command: str, corrupted: bool):
        embedding_size = self.xb.shape[1]
        batch_size = 100
        k = 100

        total = self.xb.shape[0]
        total_batch = int(total / batch_size)
        logger.info("dataset num: %d, total_batch: %d, dimension: %d, search num: %d, topK: %d" % (
            total, total_batch, embedding_size, self.xq.shape[0], k))

        self.create_db(router_url)
        self.create_space(router_url, embedding_size)

        add(total_batch, batch_size, self.xb, with_id=True)

        waiting_index_finish(total)

        backup_id = self.backup(router_url, command)
        time.sleep(30)

        destroy(router_url, self.db_name, self.space_name)
        time.sleep(30)
        self.create_db(router_url)

        if command == "export":
            logger.info("export data")
            self.create_space(router_url, embedding_size)
            self.download_data_files_and_upsert(os.getenv("S3_BUCKET_NAME", "test"), backup_id)
        else:
            logger.info("restore data")
            time.sleep(10)
            self.backup(router_url, "restore", corrupted)
            time.sleep(30)

        waiting_index_finish(total)

        for parallel_on_queries in [0, 1]:
            self.query(parallel_on_queries, k)

        for i in range(100):
            query_dict_partition = {
                "document_ids": [str(i)],
                "limit": 1,
                "db_name": self.db_name,
                "space_name": space_name,
                "vector_value": True,
            }
            query_url = router_url + "/document/query"
            response = requests.post(
                query_url, auth=(username, password), json=query_dict_partition
            )
            assert response.json()["data"]["total"] == 1
            doc = response.json()["data"]["documents"][0]
            origin_doc = {}
            origin_doc["_id"] = str(i)
            origin_doc["field_int"] = i
            origin_doc["field_vector"] = self.xb[i].tolist()
            assert self.compare_doc(doc, origin_doc)

        destroy(router_url, self.db_name, self.space_name)
        # remove backup files
        self.remove_oss_files(f"{self.cluster_name}/", recursive=True)

    @pytest.mark.parametrize(["command", "corrupted_data"], [
        ["export", False],
        ["export", True],
        ["create", False],
        ["create", True],
    ])
    def test_vearch_backup(self, command: str, corrupted_data: bool):
        self.benchmark(command, corrupted_data)


    def test_error_params(self):
        embedding_size = self.xb.shape[1]
        batch_size = 100
        k = 100

        total = self.xb.shape[0]
        total_batch = int(total / batch_size)
        logger.info("dataset num: %d, total_batch: %d, dimension: %d, search num: %d, topK: %d" % (
            total, total_batch, embedding_size, self.xq.shape[0], k))

        self.create_db(router_url)
        self.create_space(router_url, embedding_size)

        add(total_batch, batch_size, self.xb, with_id=True)

        waiting_index_finish(total)

        self.backup(router_url, "export", error_param=True)

        destroy(router_url, self.db_name, self.space_name)
