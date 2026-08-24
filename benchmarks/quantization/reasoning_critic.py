"""Compatibility import for the archived AWQ critic boundary."""

from __future__ import annotations

from ._archived_compat import load_archived

_ARCHIVED = load_archived("reasoning_critic")
critic_prompt = _ARCHIVED.critic_prompt
request = _ARCHIVED.request


def rewrite(*args, **kwargs):
    """Delegate while preserving the historical patchable request boundary."""

    _ARCHIVED.request = request
    return _ARCHIVED.rewrite(*args, **kwargs)


__all__ = ("critic_prompt", "request", "rewrite")
