"""Unit tests use model stand-ins and local Qdrant; collection must stay offline."""
from qdrant_client.qdrant_remote import QdrantRemote


def pytest_configure(config):
    # The production client is replaced by test fixtures, but its constructor's
    # version check would otherwise start an HTTP thread during test collection.
    QdrantRemote._check_compatibility = lambda *args, **kwargs: None
