"""隐私不变量的纯断言。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


_FORBIDDEN_KEYS = frozenset(
    {
        "child",
        "children",
        "child_count",
        "elderly",
        "elderly_count",
        "infant",
        "infants",
        "infant_count",
        "passenger_count",
        "cabin_allocation",
        "business_seats",
        "economy_seats",
        "cabin_business_types",
    }
)
_FORBIDDEN_PASSENGER_TYPES = frozenset({"child", "elderly", "infant"})


def _normalized_key(value) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def assert_no_passenger_composition(payload, *, source: str = "unknown") -> None:
    """递归确认外发参数不含订阅乘客构成或分舱结构。"""

    def visit(value, path: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = _normalized_key(key)
                current = (*path, normalized)
                if normalized in _FORBIDDEN_KEYS:
                    raise AssertionError(
                        f"{source} 外发参数含乘客构成字段:{'.'.join(current)}"
                    )
                if (
                    normalized == "type"
                    and str(item or "").strip().lower() in _FORBIDDEN_PASSENGER_TYPES
                ):
                    raise AssertionError(
                        f"{source} 外发参数含非成人乘客类型:{'.'.join(current)}"
                    )
                visit(item, current)
            return
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for index, item in enumerate(value):
                visit(item, (*path, str(index)))

    visit(payload, ())
