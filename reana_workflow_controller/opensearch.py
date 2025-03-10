# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2024 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""OpenSearch client and log fetcher."""

import logging
from opensearchpy import OpenSearch

from reana_commons.utils import get_dask_component_name
from reana_workflow_controller.config import (
    REANA_OPENSEARCH_CA_CERTS,
    REANA_OPENSEARCH_HOST,
    REANA_OPENSEARCH_PASSWORD,
    REANA_OPENSEARCH_PORT,
    REANA_OPENSEARCH_URL_PREFIX,
    REANA_OPENSEARCH_USE_SSL,
    REANA_OPENSEARCH_USER,
    REANA_OPENSEARCH_ENABLED,
)


def build_opensearch_client(
    host: str = REANA_OPENSEARCH_HOST,
    port: str = REANA_OPENSEARCH_PORT,
    url_prefix: str = REANA_OPENSEARCH_URL_PREFIX,
    http_auth: tuple | None = (REANA_OPENSEARCH_USER, REANA_OPENSEARCH_PASSWORD),
    use_ssl: bool = REANA_OPENSEARCH_USE_SSL,
    ca_certs: str | None = REANA_OPENSEARCH_CA_CERTS,
) -> OpenSearch:
    """
    Build an OpenSearch client object.

    :param host: OpenSearch host.
    :param port: OpenSearch port.
    :param url_prefix: URL prefix.
    :param http_auth: HTTP authentication credentials.
    :param use_ssl: Use SSL/TLS for connection.
    :param ca_certs: Path to CA certificates.

    :return: OpenSearch client object.
    """
    opensearch_client = OpenSearch(
        hosts=f"{host}:{port}",
        http_compress=True,  # enables gzip compression for request bodies
        http_auth=http_auth,
        use_ssl=use_ssl,
        ca_certs=ca_certs,
        url_prefix=url_prefix,
        verify_certs=True,
    )
    return opensearch_client


class OpenSearchLogFetcher(object):
    """Retrieves job and workflow logs from OpenSearch API."""

    def __init__(
        self,
        os_client: OpenSearch | None = None,
        job_index: str = "fluentbit-job_log",
        workflow_index: str = "fluentbit-workflow_log",
        dask_index: str = "fluentbit-dask_log",
        max_rows: int = 5000,
        log_key: str = "log",
        order: str = "asc",
        job_log_matcher: str = "kubernetes.labels.job-name.keyword",
        workflow_log_matcher: str = "kubernetes.labels.reana-run-batch-workflow-uuid.keyword",
        dask_log_matcher: str = "kubernetes.labels.dask.org/cluster-name.keyword",
        timeout: int = 5,
    ) -> None:
        """
        Initialize the OpenSearchLogFetcher object.

        :param os_client: OpenSearch client object.
        :param job_index: Index name for job logs.
        :param workflow_index: Index name for workflow logs.
        :param max_rows: Maximum number of rows to fetch.
        :param log_key: Key for log message in the response.
        :param order: Order of logs (asc/desc).
        :param job_log_matcher: Job log matcher.
        :param workflow_log_matcher: Workflow log matcher.
        :param timeout: Timeout for OpenSearch queries.

        :return: None
        """
        if os_client is None:
            os_client = build_opensearch_client()

        self.os_client = os_client
        self.job_index = job_index
        self.workflow_index = workflow_index
        self.dask_index = dask_index
        self.max_rows = max_rows
        self.log_key = log_key
        self.order = order
        self.job_log_matcher = job_log_matcher
        self.workflow_log_matcher = workflow_log_matcher
        self.dask_log_matcher = dask_log_matcher
        self.timeout = timeout

    def fetch_logs(
        self, id: str, index: str, match: str = None, matches: dict | None = None
    ) -> str | None:
        """
        Fetch logs of a specific job, workflow or Dask cluster.

        :param id: Job or workflow ID.
        :param index: Index name for logs.
        :param match: Single matcher for logs (mutually exclusive with `matches`).
        :param matches: Dictionary of field-to-value pairs for multiple match conditions.

        :return: Job, workflow or Dask cluster logs matching the conditions.
        """
        if matches:
            # Build a bool query with multiple conditions
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"match": {field: value}}
                            for field, value in matches.items()
                        ]
                    }
                },
                "sort": [{"@timestamp": {"order": self.order}}],
            }
        elif match:
            # Build a simple single-match query
            query = {
                "query": {"match": {match: id}},
                "sort": [{"@timestamp": {"order": self.order}}],
            }
        else:
            logging.error("Either `match` or `matches` must be provided.")
            return None

        try:
            response = self.os_client.search(
                index=index, body=query, size=self.max_rows, timeout=self.timeout
            )
        except Exception as e:
            logging.error(f"Failed to fetch logs for {id}: {e}")
            return None

        return self._concat_rows(response["hits"]["hits"])

    def fetch_job_logs(self, backend_job_id: str) -> str:
        """
        Fetch logs of a specific job.

        :param backend_job_id: Job ID.

        :return: Job logs.
        """
        return self.fetch_logs(
            backend_job_id,
            self.job_index,
            self.job_log_matcher,
        )

    def fetch_workflow_logs(self, workflow_id: str) -> str | None:
        """
        Fetch logs of a specific workflow.

        :param workflow_id: Workflow ID.

        :return: Workflow logs.
        """
        return self.fetch_logs(
            workflow_id,
            self.workflow_index,
            self.workflow_log_matcher,
        )

    def fetch_dask_scheduler_logs(self, workflow_id: str) -> str | None:
        """
        Fetch logs of the scheduler of a Dask cluster.

        :param workflow_id: Workflow ID.

        :return: Dask cluster scheduler logs.
        """
        return self.fetch_logs(
            id=None,
            index=self.dask_index,
            matches={
                self.dask_log_matcher: get_dask_component_name(workflow_id, "cluster"),
                "kubernetes.labels.dask.org/component": "scheduler",
            },
        )

    def fetch_dask_worker_logs(self, workflow_id: str) -> str | None:
        """
        Fetch logs of the workers of a Dask cluster.

        :param workflow_id: Workflow ID.

        :return: Dask cluster worker logs.
        """
        return self.fetch_logs(
            id=None,
            index=self.dask_index,
            matches={
                self.dask_log_matcher: get_dask_component_name(workflow_id, "cluster"),
                "kubernetes.labels.dask.org/component": "worker",
            },
        )

    def _concat_rows(self, rows: list) -> str | None:
        """
        Concatenate log messages from rows.

        :param rows: List of rows.

        :return: Concatenated log messages.
        """
        logs = ""

        for hit in rows:
            logs += hit["_source"][self.log_key] + "\n"

        return logs


def build_opensearch_log_fetcher() -> OpenSearchLogFetcher | None:
    """
    Build OpenSearchLogFetcher object.

    :return: OpenSearchLogFetcher object.
    """
    return OpenSearchLogFetcher() if REANA_OPENSEARCH_ENABLED else None
