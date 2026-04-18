from __future__ import annotations

from .base import Adapter, TestArtifact
from .javascript import JavaScriptAdapter
from .python import PythonAdapter

_ADAPTERS: list[Adapter] = [PythonAdapter(), JavaScriptAdapter()]


def for_file(source_rel: str) -> Adapter:
    for a in _ADAPTERS:
        if a.detect(source_rel):
            return a
    raise ValueError(f"no adapter for file: {source_rel}")


__all__ = ["Adapter", "TestArtifact", "PythonAdapter", "JavaScriptAdapter", "for_file"]
