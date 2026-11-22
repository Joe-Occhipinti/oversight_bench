"""Full-screen terminal interface (needs the `tui` extra: prompt_toolkit).

Imported only when an interactive session starts with `tui.enabled = true`.
"""

from meltzer.tui.app import FullScreenUI

__all__ = ["FullScreenUI"]
