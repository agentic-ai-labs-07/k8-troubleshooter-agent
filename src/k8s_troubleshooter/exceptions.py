"""Errors shared across the package."""


class SafetyError(Exception):
    """The name or the change is outside the allowed list."""


class CommandError(RuntimeError):
    """kubectl failed or timed out."""
