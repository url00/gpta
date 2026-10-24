"""Exceptions shared by the runner, scheduler, service, and HTTP layer."""


class NotFound(Exception):
    """Unknown module or output."""


class AlreadyRunning(Exception):
    """The module already has an active run."""


class ModuleTimeout(Exception):
    """A module fetch exceeded the configured run timeout."""


class ModuleStuck(Exception):
    """The abandoned worker for a timed-out run is still executing."""
