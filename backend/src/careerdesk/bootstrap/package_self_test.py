"""Frozen-package checks for runtime assets that imports alone cannot verify."""

import sqlite3


def run_package_self_test() -> None:
    """Load Magika's model and sqlite-vec from the active installation."""
    from magika import Magika
    import sqlite_vec

    result = Magika().identify_bytes(b"CareerDesk package self test")
    if not result.output.label:
        raise RuntimeError("Magika returned no content label")

    connection = sqlite3.connect(":memory:")
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        version = connection.execute("SELECT vec_version()").fetchone()
        if not version or not version[0]:
            raise RuntimeError("sqlite-vec returned no version")
    finally:
        connection.close()
