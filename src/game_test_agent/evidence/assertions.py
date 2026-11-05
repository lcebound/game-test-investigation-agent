"""Deterministic assertions over scoped immutable JSON, independent of providers."""
from __future__ import annotations

import json
import math
from decimal import Decimal, localcontext
from fractions import Fraction

from game_test_agent.context.access import ContextAccess
from game_test_agent.contracts.models import ContextAccessScope, EvidenceAssertion


def json_pointer(value, pointer: str):
    for token in pointer.split("/")[1:] if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not token.isascii() or not token.isdecimal() or len(token) > 1 and token.startswith("0"):
                raise ValueError("invalid JSON array index")
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise ValueError("JSON Pointer traverses a scalar")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON keys are ambiguous")
        result[key] = value
    return result


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("a finite JSON number is required")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite measurements are not supported")
    if len(result.as_tuple().digits) > 1000 or abs(result.adjusted()) > 1000:
        raise ValueError("measurement exceeds arithmetic limits")
    return Fraction(result)


def _scalar(value):
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value.encode()) > 2000:
            raise ValueError("scalar string exceeds the assertion output limit")
        return value
    return _number(value)


def _aggregate(value, operation):
    if operation == "value":
        return _scalar(value)
    if not isinstance(value, list) or len(value) > 100_000:
        raise ValueError("aggregation requires a bounded JSON array")
    if operation == "count":
        return Fraction(len(value))
    if not value:
        raise ValueError("an empty array has no numeric statistic")
    values = sorted(_number(item) for item in value)
    if operation == "min":
        return values[0]
    if operation == "max":
        return values[-1]
    if operation == "mean":
        return sum(values) / len(values)
    # Linear interpolation (R7), also used by MetricCsvAnalyzer.
    position = Fraction(len(values) - 1) * int(operation[1:]) / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _compare(actual, operator, expected):
    if operator in {"eq", "ne"}:
        equal = type(actual) is type(expected) and actual == expected
        return equal if operator == "eq" else not equal
    if not isinstance(actual, Fraction) or not isinstance(expected, Fraction):
        raise ValueError("ordered comparisons require numeric operands")
    return {"lt": actual < expected, "le": actual <= expected,
            "gt": actual > expected, "ge": actual >= expected}[operator]


def _display(value):
    if isinstance(value, Fraction):
        denominator = value.denominator
        for factor in (2, 5):
            while denominator % factor == 0:
                denominator //= factor
        if denominator != 1:
            return str(value)  # Exact rational; never round into a false equality.
        with localcontext() as context:
            context.prec = len(str(abs(value.numerator))) + len(str(value.denominator)) + 2
            return str(Decimal(value.numerator) / Decimal(value.denominator))
    return json.dumps(value, ensure_ascii=False)


class AssertionVerifier:
    """One read per artifact; the rendered claim contains no model-authored labels."""

    def __init__(self, repository, artifacts, view):
        self.access = ContextAccess(repository, artifacts)
        self.view = view
        self.documents = {}
        self.results = {}
        self.input_bytes = 0

    def verify(self, assertion: EvidenceAssertion) -> str:
        try:
            if assertion.evidence_id not in self.documents:
                _, raw = self.access.read_artifact(self.view.investigation_id, assertion.evidence_id,
                    ContextAccessScope(tenant_id=self.view.request.tenant_id, project_id=self.view.request.project_id))
                self.input_bytes += len(raw)
                if len(raw) > 4 * 1024 * 1024 or self.input_bytes > 16 * 1024 * 1024:
                    raise ValueError("structured assertion input exceeds 4 MiB")
                self.documents[assertion.evidence_id] = json.loads(raw, object_pairs_hook=_unique_object, parse_float=Decimal,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            key = (assertion.evidence_id, assertion.pointer, assertion.operation)
            if key not in self.results:
                value = json_pointer(self.documents[assertion.evidence_id], assertion.pointer)
                self.results[key] = _aggregate(value, assertion.operation)
            actual = self.results[key]
            expected = _scalar(assertion.expected)
            if not _compare(actual, assertion.operator, expected):
                raise ValueError("asserted relation is false")
        except (LookupError, ValueError, TypeError, ArithmeticError, RecursionError) as error:
            # Do not echo submitted or source values from parser exceptions.
            raise ValueError("Assertion does not match an unambiguous JSON field, statistic or relation.") from error
        pointer = json.dumps(assertion.pointer, ensure_ascii=False)
        method = "（R7 线性插值）" if assertion.operation in {"p50", "p95", "p99"} else ""
        return (f"证据 {assertion.evidence_id} 的 JSON Pointer {pointer}："
                f"{assertion.operation}{method}={_display(actual)}；满足 {assertion.operator} {_display(expected)}。")

    def render(self, assertions: list[EvidenceAssertion]) -> str:
        return "\n".join(self.verify(assertion) for assertion in assertions)
