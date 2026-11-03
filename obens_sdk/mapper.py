from __future__ import annotations

from typing import Any, Callable, Dict


class _Constant:
    def __init__(self, value: Any):
        self.value = value


def constant(value: Any) -> Any:
    """A fixed value for every record. Plain strings in a spec are paths into the record."""
    return _Constant(value)


def _read(record: Any, path: str) -> Any:
    value = record
    for key in path.split("."):
        if value is None:
            return None
        value = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
    return value


def create_mapper(spec: Dict[str, Any]) -> Callable[[Any], Dict[str, Any]]:
    """Builds a mapper (your record -> what the platform expects) from a spec.

    Each entry is a string (a dotted path into your record, dict key or attribute), a callable computed from the
    record, ``constant(x)`` for a fixed value, or a number / bool taken as is. Dotted keys build nested dicts and
    ``None`` results are left out, so an update never blanks a field.
    """

    def mapper(record: Any) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, source in spec.items():
            if isinstance(source, _Constant):
                value = source.value
            elif callable(source):
                value = source(record)
            elif isinstance(source, str):
                value = _read(record, source)
            else:
                value = source
            if value is None:
                continue
            *parents, last = key.split(".")
            target = out
            for part in parents:
                target = target.setdefault(part, {})
            target[last] = value
        return out

    return mapper
