"""A destination owns its authentication, upload format, and completion semantics."""

from ..common import Delivery


class Destination:
    name = ""
    key_variable = ""

    def __init__(self, key, transport, settings):
        self.key = key
        self.http = transport
        self.settings = settings
        self.account = ""

    async def connect(self):
        """Resolve a stable recipient identity so changing accounts does not reuse receipts."""
        raise NotImplementedError

    async def send(self, row, content, filename):
        raise NotImplementedError

    async def poll(self, receipt):
        # Synchronous providers finish in send(); asynchronous providers override this method.
        return Delivery("sent", receipt=receipt)
