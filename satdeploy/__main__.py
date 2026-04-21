"""Enables ``python -m satdeploy`` as an alias for the ``satdeploy`` script.

The installed ``.venv/bin/satdeploy`` launcher has a stale shebang on some
machines (it points at an older venv python), so scripts + docs increasingly
use ``python -m satdeploy`` to invoke the CLI. This module is the glue that
makes that invocation resolve to the same ``main`` click group.
"""
from satdeploy.cli import main

if __name__ == "__main__":
    main()
