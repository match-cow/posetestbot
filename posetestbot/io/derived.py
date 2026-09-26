"""Keep replaceable derived directories separate from retained input evidence."""

from pathlib import Path
from typing import Iterable

from posetestbot.io.manifest import sensor_type_from_folder_name


def validate_derived_output(
    run_root: Path, destination: Path, *, sources: Iterable[Path] = ()
) -> None:
    """Reject overlap with inputs, raw sensor folders, or run-level artifacts.

    Explicit external output directories remain supported. Resolve aliases before
    comparing so a symlink cannot turn a derived write into a raw-data overwrite.
    Check before creating staging directories or changing a run manifest.
    """

    root = run_root.resolve()
    output = destination.resolve()
    if root.is_relative_to(output):
        raise ValueError("Derived output must not replace the run root or its ancestors")
    protected = [*sources]
    if root.is_dir():
        protected.extend(
            path
            for path in root.iterdir()
            if path.is_file() or sensor_type_from_folder_name(path.name) is not None
        )
    for path in protected:
        source = path.resolve()
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError(f"Derived output overlaps retained input: {path}")
