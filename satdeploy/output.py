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
    """Format a warning message with yellow color and [WARNING] prefix."""
    return click.style(f"[WARNING] {message}", fg="yellow")


def error(message: str) -> str:
    """Format an error message with red color and [ERROR] prefix."""
    return click.style(f"[ERROR] {message}", fg="red")


def step(current: int, total: int, message: str) -> str:
    """Format a step counter message."""
    counter = click.style(f"[{current}/{total}]", fg="bright_white")
    return f"{counter} {message}"


class SatDeployError(click.ClickException):
    """Custom exception that displays error messages in red."""

    def format_message(self) -> str:
        """Format the error message with red color and cross symbol."""
        return error(self.message)

    def show(self, file=None):
        """Show the error message without the 'Error:' prefix."""
        if file is None:
            file = click.get_text_stream("stderr")
        click.echo(self.format_message(), file=file)


def _style_exception(e: click.ClickException) -> None:
    """Apply red styling to a Click exception."""
    if isinstance(e, SatDeployError):
        return  # Already styled

    # Capture original format_message
    original_format = e.format_message

    # Override format_message to add styling
    e.format_message = lambda: error(original_format())

    # Override show to remove "Error:" prefix
    def custom_show(file=None):
        if file is None:
            file = click.get_text_stream("stderr")
        click.echo(e.format_message(), file=file)

    e.show = custom_show


class ColoredGroup(click.Group):
    """Custom Click group that displays all errors in red."""

    def invoke(self, ctx):
        """Override invoke to catch and color all Click exceptions."""
        try:
            return super().invoke(ctx)
        except click.ClickException as e:
            _style_exception(e)
            raise

    def main(self, *args, **kwargs):
        """Override main to catch and color all Click exceptions."""
        try:
            return super().main(*args, **kwargs)
        except click.ClickException as e:
            _style_exception(e)
            raise
