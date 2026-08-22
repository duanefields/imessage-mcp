import pathlib
import sqlite3

import pytest

from .support import synthetic_db


@pytest.fixture(scope="session")
def chat_db_path(tmp_path_factory) -> pathlib.Path:
    """A synthetic chat.db, built once for the session."""
    path = tmp_path_factory.mktemp("imessage") / "chat.db"
    synthetic_db.build(path).close()
    return path


@pytest.fixture
def conn(chat_db_path) -> sqlite3.Connection:
    from imessage_mcp.db import connect

    connection = connect(chat_db_path)
    yield connection
    connection.close()


@pytest.fixture
def resolver():
    """A resolver over invented contacts, with no address book involved."""
    from imessage_mcp.contacts import ContactResolver

    return ContactResolver(
        {
            "5125550101": "Alice Example",
            "5125550102": "Bob Example",
            "dana@example.com": "Dana Example",
        }
    )
