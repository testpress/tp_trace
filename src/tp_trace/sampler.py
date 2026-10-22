"""Custom rule-based sampler for tp_trace SDK v3 supporting endpoint ignoring and custom per-route sampling rates."""

import fnmatch
from typing import Any, Dict, List, Mapping, Optional, Sequence
from opentelemetry.context import Context
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import Link, SpanKind, TraceState

from tp_trace.config import _parse_endpoint_sample_rules, _parse_rate


class TpTraceRuleBasedSampler(Sampler):
    """
    Custom Sampler supporting:
    1. ignore_endpoints: Wildcard path patterns (e.g. ["/health*", "*/static/*"]) that return Decision.DROP.
    2. endpoint_sample_rules: Dictionary of path patterns to sample rates (e.g. {"/api/checkout/*": 1.0}).
    3. Global fallback ratio-based sampling.
    """

    def __init__(
        self,
        global_sample_rate: float = 1.0,
        ignore_endpoints: Optional[List[str]] = None,
        endpoint_sample_rules: Optional[Dict[str, float]] = None,
    ):
        self.global_sample_rate = _parse_rate(global_sample_rate, "global_sample_rate")
        self.ignore_endpoints: List[str]
        if isinstance(ignore_endpoints, str):
            self.ignore_endpoints = [p.strip() for p in ignore_endpoints.split(",") if p.strip()]
        else:
            self.ignore_endpoints = [p.strip() for p in (ignore_endpoints or []) if isinstance(p, str) and p.strip()]
        self.endpoint_sample_rules = _parse_endpoint_sample_rules(endpoint_sample_rules)

        # Pre-instantiate ratio samplers for fast lookup
        self._global_ratio_sampler = TraceIdRatioBased(self.global_sample_rate)
        self._rule_ratio_samplers: Dict[str, TraceIdRatioBased] = {
            pattern: TraceIdRatioBased(rate)
            for pattern, rate in self.endpoint_sample_rules.items()
        }

    def should_sample(
        self,
        parent_context: Optional[Context],
        trace_id: int,
        name: str,
        kind: Optional[SpanKind] = None,
        attributes: Optional[Mapping[str, Any]] = None,
        links: Optional[Sequence[Link]] = None,
        trace_state: Optional[TraceState] = None,
    ) -> SamplingResult:
        attr_dict = dict(attributes) if attributes else {}

        # Extract candidate target request paths from OpenTelemetry attribute conventions and span name
        candidates = []
        for key in ("http.route", "url.path", "http.target"):
            val = attr_dict.get(key)
            if isinstance(val, str) and val.strip():
                clean = val.split("?")[0].strip()
                if clean and clean not in candidates:
                    candidates.append(clean)
        if name and isinstance(name, str) and name.strip():
            clean_name = name.split("?")[0].strip()
            if clean_name and clean_name not in candidates:
                candidates.append(clean_name)

        if candidates:
            # 1. Check ignore patterns
            for candidate in candidates:
                for pattern in self.ignore_endpoints:
                    if fnmatch.fnmatch(candidate, pattern):
                        return SamplingResult(Decision.DROP)

            # 2. Check custom endpoint sampling rules
            for candidate in candidates:
                for pattern, ratio_sampler in self._rule_ratio_samplers.items():
                    if fnmatch.fnmatch(candidate, pattern):
                        return ratio_sampler.should_sample(
                            parent_context=parent_context,
                            trace_id=trace_id,
                            name=name,
                            kind=kind,
                            attributes=attributes,
                            links=links,
                            trace_state=trace_state,
                        )

        # 3. Fallback to global sample rate
        return self._global_ratio_sampler.should_sample(
            parent_context=parent_context,
            trace_id=trace_id,
            name=name,
            kind=kind,
            attributes=attributes,
            links=links,
            trace_state=trace_state,
        )

    def get_description(self) -> str:
        return f"TpTraceRuleBasedSampler(global={self.global_sample_rate}, ignores={len(self.ignore_endpoints)}, rules={len(self.endpoint_sample_rules)})"


class RemoteParentAwareSampler(Sampler):
    """Evaluates ignore_endpoints for remote parents before accepting upstream sampling."""

    def __init__(self, root_sampler: TpTraceRuleBasedSampler):
        self.root_sampler = root_sampler

    def should_sample(
        self,
        parent_context: Optional[Context],
        trace_id: int,
        name: str,
        kind: Optional[SpanKind] = None,
        attributes: Optional[Mapping[str, Any]] = None,
        links: Optional[Sequence[Link]] = None,
        trace_state: Optional[TraceState] = None,
    ) -> SamplingResult:
        res = self.root_sampler.should_sample(
            parent_context=parent_context,
            trace_id=trace_id,
            name=name,
            kind=kind,
            attributes=attributes,
            links=links,
            trace_state=trace_state,
        )
        if res.decision == Decision.DROP:
            return res
        return SamplingResult(Decision.RECORD_AND_SAMPLE)

    def get_description(self) -> str:
        return f"RemoteParentAwareSampler({self.root_sampler.get_description()})"


def create_tp_trace_sampler(
    global_sample_rate: float = 1.0,
    ignore_endpoints: Optional[List[str]] = None,
    endpoint_sample_rules: Optional[Dict[str, float]] = None,
) -> Sampler:
    """Creates a ParentBased sampler wrapping TpTraceRuleBasedSampler with remote parent awareness."""
    root_sampler = TpTraceRuleBasedSampler(
        global_sample_rate=global_sample_rate,
        ignore_endpoints=ignore_endpoints,
        endpoint_sample_rules=endpoint_sample_rules,
    )
    remote_sampler = RemoteParentAwareSampler(root_sampler=root_sampler)
    return ParentBased(
        root=root_sampler,
        remote_parent_sampled=remote_sampler,
    )
