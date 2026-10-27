"""Django integration package — re-exports DjangoIntegration.

Layout follows:
integrations/django/
  __init__.py      — re-export
  integration.py   — DjangoIntegration orchestration
  request.py       — SERVER request hooks and OTel middleware enhancement
  middleware.py    — load_middleware wrapper
  view.py          — view span
  template.py      — template span
"""

from .integration import DjangoIntegration

__all__ = ["DjangoIntegration"]
