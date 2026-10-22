"""Integrations module for tp_trace."""

from tp_trace.integrations.base import BaseIntegration
from tp_trace.integrations.django import DjangoIntegration
from tp_trace.integrations.manager import IntegrationManager, get_integration_manager
from tp_trace.integrations.postgres import PostgresIntegration
from tp_trace.integrations.redis import RedisIntegration
from tp_trace.integrations.requests import RequestsIntegration

__all__ = [
    "BaseIntegration",
    "DjangoIntegration",
    "PostgresIntegration",
    "RedisIntegration",
    "RequestsIntegration",
    "IntegrationManager",
    "get_integration_manager",
]
