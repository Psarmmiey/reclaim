"""Reclaim - keep an eye on what is eating your Mac's disk."""
__version__ = "0.2.0"  # x-release-please-version
APP_NAME = "Reclaim"

try:                                  # written by build-app.sh
    from ._build import BUILD, SOURCE
except ImportError:
    BUILD, SOURCE = "dev", ""


def version_string():
    return "%s (build %s)" % (__version__, BUILD)
