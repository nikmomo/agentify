"""RemoteAdapter: the adapter protocol over a shared folder (see docs/ADAPTER_CONTRACT.md)."""

from agentify.adapters.remote.client import RemoteAdapter
from agentify.adapters.remote.host import AdapterHost

__all__ = ["AdapterHost", "RemoteAdapter"]
