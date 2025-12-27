"""Output formatting utilities for CLI."""

import click

SYMBOLS = {
    "check": "▸",
    "cross": "✗",
    "arrow": "→",
    "bullet": "•",
}


def success(message: str) -> str:
    """Format a success message with green color and checkmark."""
    return click.style(f"{SYMBOLS['check']} {message}", fg="green")


def warning(message: str) -> str:
    """Format a warning message with yellow color."""
    return click.style(message, fg="yellow")


def step(current: int, total: int, message: str) -> str:
    """Format a step counter message."""
    counter = click.style(f"[{current}/{total}]", fg="bright_white")
    return f"{counter} {message}"
