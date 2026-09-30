"""The credential-provider seam. Engine adapters never read secrets straight
from the settings store — they ask a CredentialProvider. Today the install is
single-user and credentials are global (GlobalCredentialProvider reads the one
settings table); a future multi-user layer implements the same seam keyed by
user and no adapter changes."""

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # settings_store reaches into the engine catalog, which reaches back here
    from opus.settings_store import RuntimeConfig


class CredentialProvider(abc.ABC):
    @abc.abstractmethod
    def get(self, engine: str, key: str) -> str | None:
        ...

    def has(self, engine: str, key: str) -> bool:
        return bool(self.get(engine, key))


class GlobalCredentialProvider(CredentialProvider):
    def __init__(self, runtime: "RuntimeConfig"):
        self._runtime = runtime

    def get(self, engine: str, key: str) -> str | None:
        return self._runtime.get(f"{engine}_{key}") or None
