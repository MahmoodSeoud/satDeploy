"""Output formatting utilities for CLI."""

import click

SYMBOLS = {
    "check": "✓",
    "cross": "✗",
    "arrow": "→",
    "bullet": "•",
}


def success(message: str) -> str:
    """Format a success message with green color and checkmark."""
    return click.style(f"{SYMBOLS['check']} {message}", fg="green")


def error(message: str) -> str:
    """Format an error message with red color and cross."""
    return click.style(f"{SYMBOLS['cross']} {message}", fg="red")


def warning(message: str) -> str:
    """Format a warning message with yellow color."""
    return click.style(message, fg="yellow")


def info(message: str) -> str:
    """Format an info message."""
    return message


def step(current: int, total: int, message: str) -> str:
    """Format a step counter message."""
    counter = click.style(f"[{current}/{total}]", fg="cyan")
    return f"{counter} {message}"
