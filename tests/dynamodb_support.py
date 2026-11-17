"""A local, stateful DynamoDB table for tests.

The table is served in process by moto, which stores items and evaluates key
conditions, condition expressions, and transactions. No AWS account, network
access, or real credentials are involved.
"""

from __future__ import annotations

try:
    import boto3
    from botocore.exceptions import ClientError
    from moto import mock_aws
except ImportError:  # pragma: no cover - exercised only without the extras
    boto3 = None

AVAILABLE = boto3 is not None
SKIP_REASON = 'DynamoDB tests need pip install -e ".[dynamodb,dev]"'
TABLE_NAME = "asof-test"
REGION = "us-east-1"


def client_error(code, operation, message="request failed"):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class LocalDynamoDB:
    """Start an empty table with the key schema the store expects."""

    def start(self):
        self._mock = mock_aws()
        self._mock.start()
        self.client = boto3.client(
            "dynamodb",
            region_name=REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        self.client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        return self

    def stop(self):
        self.client.close()
        self._mock.stop()

    def items(self):
        """Return every stored item. Only tests read the table this way."""

        found = []
        request = {"TableName": TABLE_NAME, "ConsistentRead": True}
        while True:
            response = self.client.scan(**request)
            found.extend(response["Items"])
            if "LastEvaluatedKey" not in response:
                return found
            request["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def manifests(self):
        return [item for item in self.items() if item["sk"]["S"].startswith("OBS#")]

    def cleanup_records(self):
        return [item for item in self.items() if item["sk"]["S"].startswith("RETIRE#")]

    def vintages(self, generation_key=None):
        return [
            item
            for item in self.items()
            if item["sk"]["S"].startswith("VINTAGE#")
            and generation_key in (None, item["pk"]["S"])
        ]


class InterceptingClient:
    """Pass requests to the local table, recording them and injecting faults.

    Requests still reach the real local implementation unless a hook raises,
    so tests observe stored state rather than a scripted response.
    """

    def __init__(self, client):
        self._client = client
        self.calls = []
        self.page_limit = None
        self.process_at_most = None
        self._hooks = {}
        self._replies = {}

    def before(self, operation, hook):
        """Run ``hook(parameters)`` ahead of every later call to ``operation``."""

        self._hooks.setdefault(operation, []).append(hook)

    def reply_once(self, operation, response):
        """Answer the next call with a response the local table cannot produce."""

        self._replies.setdefault(operation, []).append(response)

    def operations(self):
        return [operation for operation, _ in self.calls]

    def parameters(self, operation):
        return [parameters for name, parameters in self.calls if name == operation]

    def __getattr__(self, operation):
        target = getattr(self._client, operation)
        if not callable(target):
            return target

        def call(**parameters):
            self.calls.append((operation, parameters))
            for hook in tuple(self._hooks.get(operation, ())):
                hook(parameters)
            if self._replies.get(operation):
                return self._replies[operation].pop(0)
            if operation == "query" and self.page_limit and "Limit" not in parameters:
                parameters = {**parameters, "Limit": self.page_limit}
            if operation == "batch_write_item" and self.process_at_most is not None:
                return self._partial_batch_write(target, parameters)
            return target(**parameters)

        return call

    def _partial_batch_write(self, target, parameters):
        ((table, requests),) = parameters["RequestItems"].items()
        accepted = requests[: self.process_at_most]
        deferred = requests[self.process_at_most :]
        if accepted:
            target(RequestItems={table: accepted})
        return {"UnprocessedItems": {table: deferred} if deferred else {}}
