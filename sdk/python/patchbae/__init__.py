"""Local stand-in for the `patchbae` module a component imports.

Inside Patchbae the runner provides the real module. This copy has the same
surface and keeps what a component did in memory, so a component can be run
and unit-tested on a laptop:

    PYTHONPATH=sdk/python pytest components/aws-transcribe

It is not shipped with a component and never runs in production.
"""

import json as _json


class ComponentFailed(Exception):
    """Raised by fail(): the node fails with this message and no traceback."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


class _Task:
    """Identity of the run this component is executing in.

    `id` is stable across redeliveries of the same task, which makes it the
    right seed for an idempotency token or a job name.
    """

    def __init__(self):
        self.id = "00000000-0000-0000-0000-000000000001"
        self.run_id = "00000000-0000-0000-0000-000000000002"
        self.workflow_id = "00000000-0000-0000-0000-000000000003"
        self.node_id = "component_1"
        self.org_id = "00000000-0000-0000-0000-000000000004"


task = _Task()

# Labelled media references produced by earlier nodes: assets["Proxy"]["locator"].
assets = {}

# What the component did, for assertions in tests.
logged = []
emitted_assets = []


def log(message, **fields):
    """Write one structured line to the node's log."""
    logged.append({"level": "info", "message": str(message), **fields})
    print(_json.dumps({"level": "info", "message": str(message), **fields}))


def warn(message, **fields):
    """Like log(), flagged as a warning. Does not fail the node."""
    logged.append({"level": "warn", "message": str(message), **fields})
    print(_json.dumps({"level": "warn", "message": str(message), **fields}))


def fail(message, *, details=None):
    """Fail the node on purpose with a clean message."""
    raise ComponentFailed(str(message), details)


def emit_asset(label, locator, media_type=None, **metadata):
    """Add a labelled media reference (never file content) to the node's output."""
    if not label or not locator:
        raise ValueError("emit_asset requires both a label and a locator")
    ref = {"label": str(label), "locator": str(locator)}
    if media_type:
        ref["mediaType"] = str(media_type)
    if metadata:
        ref["metadata"] = metadata
    emitted_assets.append(ref)
    return ref


def reset():
    """Clear recorded logs, emitted assets and incoming assets between tests."""
    del logged[:]
    del emitted_assets[:]
    assets.clear()
