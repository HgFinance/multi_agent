"""Legacy flat-import compatibility for the canonical audit repository.

The QA API and older tests import ``repository`` after adding the QA
directory to ``sys.path``.  The implementation lives in ``audit.repository``;
keep this shim so those script-style entry points and package imports share
one implementation.
"""

from audit.repository import *  # noqa: F403 - compatibility facade for legacy imports
