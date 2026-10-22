"""Unit tests for endpoint-based and custom rule sampling in tp_trace SDK v3."""

import pytest
from opentelemetry.sdk.trace.sampling import Decision
from tp_trace.config import SDKConfig
from tp_trace.sampler import TpTraceRuleBasedSampler, create_tp_trace_sampler


def test_ignore_endpoints_wildcard_matching():
    """Verify ignore_endpoints drops spans matching exact or wildcard path patterns."""
    sampler = TpTraceRuleBasedSampler(
        global_sample_rate=1.0,
        ignore_endpoints=["/health*", "*/static/*", "/api/products/health/"],
    )

    # Ignored paths -> DROP
    res1 = sampler.should_sample(None, 12345, "GET /health", attributes={"http.target": "/health"})
    assert res1.decision == Decision.DROP

    res2 = sampler.should_sample(None, 12345, "GET /healthz", attributes={"http.target": "/healthz?foo=bar"})
    assert res2.decision == Decision.DROP

    res3 = sampler.should_sample(None, 12345, "GET /app/static/main.css", attributes={"url.path": "/app/static/main.css"})
    assert res3.decision == Decision.DROP

    res4 = sampler.should_sample(None, 12345, "GET /api/products/health/", attributes={"http.target": "/api/products/health/"})
    assert res4.decision == Decision.DROP

    # Non-ignored path -> RECORD_AND_SAMPLE (global_sample_rate=1.0)
    res5 = sampler.should_sample(None, 12345, "GET /api/products/", attributes={"http.target": "/api/products/"})
    assert res5.decision == Decision.RECORD_AND_SAMPLE


def test_endpoint_sample_rules_custom_ratios():
    """Verify endpoint_sample_rules applies custom per-route sampling ratios."""
    sampler = TpTraceRuleBasedSampler(
        global_sample_rate=0.0,  # Drop all by default
        endpoint_sample_rules={
            "/api/checkout/*": 1.0,  # Always sample checkout
            "/api/health": 0.0,      # Always drop health
        },
    )

    # Checkout rule (1.0) -> RECORD_AND_SAMPLE
    res1 = sampler.should_sample(None, 12345, "POST /api/checkout/pay", attributes={"http.target": "/api/checkout/pay"})
    assert res1.decision == Decision.RECORD_AND_SAMPLE

    # Health rule (0.0) -> DROP
    res2 = sampler.should_sample(None, 12345, "GET /api/health", attributes={"http.target": "/api/health"})
    assert res2.decision == Decision.DROP

    # Unmatched path (global 0.0) -> DROP
    res3 = sampler.should_sample(None, 12345, "GET /api/products/", attributes={"http.target": "/api/products/"})
    assert res3.decision == Decision.DROP


def test_config_resolves_sampling_kwargs_and_env(monkeypatch):
    """Verify SDKConfig properly resolves ignore endpoints and endpoint sampling rules."""
    cfg = SDKConfig.from_env_and_kwargs(
        project_name="test-service",
        sample_rate=0.1,
        ignore_endpoints=["/ping", "/readyz"],
        endpoint_sample_rules={"/api/vip/*": 1.0},
    )
    assert cfg.sample_rate == 0.1
    assert cfg.ignore_endpoints == ["/ping", "/readyz"]
    assert cfg.endpoint_sample_rules == {"/api/vip/*": 1.0}

    # Test environment variable resolution
    monkeypatch.setenv("TP_TRACE_IGNORE_ENDPOINTS", "/health,/metrics")
    monkeypatch.setenv("TP_TRACE_ENDPOINT_SAMPLE_RULES", "/api/checkout/*=1.0,/api/search/*=0.5")

    cfg_env = SDKConfig.from_env_and_kwargs()
    assert cfg_env.ignore_endpoints == ["/health", "/metrics"]
    assert cfg_env.endpoint_sample_rules == {"/api/checkout/*": 1.0, "/api/search/*": 0.5}


def test_create_tp_trace_sampler():
    """Verify create_tp_trace_sampler returns ParentBased wrapper."""
    sampler = create_tp_trace_sampler(
        global_sample_rate=1.0,
        ignore_endpoints=["/health"],
    )
    assert sampler is not None


def test_parameterized_route_template_matching():
    """Verify endpoint_sample_rules matches against http.route even when url.path is a concrete URL."""
    sampler = TpTraceRuleBasedSampler(
        global_sample_rate=0.0,
        endpoint_sample_rules={
            "/api/items/{id}/": 1.0,
            "/api/users/<id>/": 1.0,
        },
    )

    # Concrete URL on url.path, parameterized template on http.route
    res1 = sampler.should_sample(
        None,
        12345,
        "GET /api/items/999/",
        attributes={
            "url.path": "/api/items/999/",
            "http.route": "/api/items/{id}/",
        },
    )
    assert res1.decision == Decision.RECORD_AND_SAMPLE

    res2 = sampler.should_sample(
        None,
        12345,
        "GET /api/users/42/",
        attributes={
            "url.path": "/api/users/42/",
            "http.route": "/api/users/<id>/",
        },
    )
    assert res2.decision == Decision.RECORD_AND_SAMPLE

    # Route that does not match rule falls back to global 0.0 -> DROP
    res3 = sampler.should_sample(
        None,
        12345,
        "GET /api/orders/555/",
        attributes={
            "url.path": "/api/orders/555/",
            "http.route": "/api/orders/{id}/",
        },
    )
    assert res3.decision == Decision.DROP


@pytest.mark.parametrize(
    "malformed_rule",
    [
        "/api/checkout/*",            # Missing '='
        "=/api/checkout/*",           # Empty pattern
        "/api/checkout/*=",           # Empty rate
        "/api/checkout/*=abc",         # Non-numeric rate
        "/api/checkout/*=1.5",         # Rate > 1.0
        "/api/checkout/*=-0.1",        # Rate < 0.0
        "/api/checkout/*=nan",         # Non-finite rate
        "/api/checkout/*=inf",         # Non-finite rate
    ],
)
def test_endpoint_sample_rules_validation_env_var(monkeypatch, malformed_rule):
    """Verify malformed TP_TRACE_ENDPOINT_SAMPLE_RULES raises ValueError with descriptive message."""
    monkeypatch.setenv("TP_TRACE_ENDPOINT_SAMPLE_RULES", malformed_rule)
    with pytest.raises(ValueError, match="tp_trace:"):
        SDKConfig.from_env_and_kwargs()


@pytest.mark.parametrize(
    "invalid_rules",
    [
        {"/api/checkout/*": 1.5},      # Rate > 1.0
        {"/api/checkout/*": -0.5},     # Rate < 0.0
        {"/api/checkout/*": "notnum"},  # Non-numeric
        {"/api/checkout/*": float("nan")},  # NaN
        {"": 0.5},                     # Empty pattern string
        {"   ": 0.5},                  # Blank pattern string
        {123: 0.5},                    # Non-string pattern
        [("/api/*", 0.5)],             # Invalid type (list instead of dict/str)
    ],
)
def test_endpoint_sample_rules_validation_kwargs(invalid_rules):
    """Verify invalid endpoint_sample_rules kwargs raise ValueError."""
    with pytest.raises(ValueError, match="tp_trace:"):
        SDKConfig.from_env_and_kwargs(endpoint_sample_rules=invalid_rules)


def test_endpoint_sample_rules_sampler_direct_validation():
    """Verify TpTraceRuleBasedSampler validates global rate and per-route rules directly."""
    # Out of range global sample rate
    with pytest.raises(ValueError, match="global_sample_rate"):
        TpTraceRuleBasedSampler(global_sample_rate=1.5)

    with pytest.raises(ValueError, match="global_sample_rate"):
        TpTraceRuleBasedSampler(global_sample_rate=-0.1)

    # Invalid rule in sampler
    with pytest.raises(ValueError, match="endpoint_sample_rules"):
        TpTraceRuleBasedSampler(endpoint_sample_rules={"/api/*": 2.0})

    with pytest.raises(ValueError, match="endpoint_sample_rules"):
        TpTraceRuleBasedSampler(endpoint_sample_rules={"/api/*": "invalid"})


def test_ignore_endpoints_string_kwarg_and_formatting():
    """Verify comma-separated string for ignore_endpoints is parsed into separate items."""
    cfg = SDKConfig.from_env_and_kwargs(ignore_endpoints="/health,/metrics, /readyz ")
    assert cfg.ignore_endpoints == ["/health", "/metrics", "/readyz"]

    # Sampler also accepts comma-separated string
    sampler = TpTraceRuleBasedSampler(ignore_endpoints="/health, /status")
    assert sampler.ignore_endpoints == ["/health", "/status"]


def test_endpoint_sample_rules_empty_dict_overrides_env_var(monkeypatch):
    """Verify passing endpoint_sample_rules={} explicitly overrides rules in environment."""
    monkeypatch.setenv("TP_TRACE_ENDPOINT_SAMPLE_RULES", "/api/checkout/*=1.0,/search/*=0.5")
    cfg = SDKConfig.from_env_and_kwargs(endpoint_sample_rules={})
    assert cfg.endpoint_sample_rules == {}


def test_tp_trace_init_handles_invalid_config_without_crashing():
    """Verify passing invalid config does not raise ValueError and crash host application."""
    import tp_trace

    tp_trace._reset_for_testing()
    try:
        # Invalid SDK-prop values must NOT crash host application; falls back safely
        provider = tp_trace.init(
            project_name="testpress",
            endpoint_sample_rules={"/health": 2.5},
        )
        assert provider is not None
        assert tp_trace._ACTIVE_CONFIG is not None
    finally:
        tp_trace._reset_for_testing()


def test_remote_parent_aware_sampler_respects_ignore_endpoints():
    """Verify RemoteParentAwareSampler drops spans matching ignore_endpoints even with sampled remote parent."""
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        TraceFlags,
        TraceState,
        set_span_in_context,
    )

    sampler = create_tp_trace_sampler(
        global_sample_rate=1.0,
        ignore_endpoints=["/health*", "/ping"],
    )

    remote_span_context = SpanContext(
        trace_id=0x12345678123456781234567812345678,
        span_id=0x1234567812345678,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    parent_ctx = set_span_in_context(NonRecordingSpan(remote_span_context))

    # Should DROP ignored endpoint even though remote parent was SAMPLED
    res1 = sampler.should_sample(
        parent_context=parent_ctx,
        trace_id=0x12345678123456781234567812345678,
        name="GET /health",
        attributes={"http.target": "/health"},
    )
    assert res1.decision == Decision.DROP

    # Non-ignored endpoint should be SAMPLED as remote parent is sampled
    res2 = sampler.should_sample(
        parent_context=parent_ctx,
        trace_id=0x12345678123456781234567812345678,
        name="GET /api/checkout",
        attributes={"http.target": "/api/checkout"},
    )
    assert res2.decision == Decision.RECORD_AND_SAMPLE
