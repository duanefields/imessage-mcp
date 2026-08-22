import sqlite3

import pytest

from imessage_mcp.contacts import ContactResolver, load_contacts, normalize_handle


@pytest.mark.parametrize(
    "handle",
    [
        "+15125550101",
        "15125550101",
        "5125550101",
        "(512) 555-0101",
        "512-555-0101",
        "512.555.0101",
        " +1 (512) 555-0101 ",
    ],
)
def test_phone_shapes_normalize_together(handle):
    """The same person reaches you in whatever shape their device recorded.

    All of these must land on one key or the contact resolves for some messages
    and not others, in the same conversation.
    """
    assert normalize_handle(handle) == "5125550101"


def test_email_normalizes_by_case_only():
    assert normalize_handle("Dana@Example.com") == "dana@example.com"
    assert normalize_handle(" dana@example.com ") == "dana@example.com"


def test_short_numbers_are_kept_whole():
    """Short codes are shorter than the match window and must not be padded."""
    assert normalize_handle("262966") == "262966"


def test_unparseable_handles_do_not_raise():
    assert normalize_handle("") == ""
    assert normalize_handle("no digits here") == "no digits here"


def test_resolver_matches_across_shapes(resolver):
    assert resolver.name_for("+15125550101") == "Alice Example"
    assert resolver.name_for("(512) 555-0101") == "Alice Example"
    assert resolver.name_for("Dana@Example.com") == "Dana Example"


def test_resolver_falls_back_to_the_handle(resolver):
    assert resolver.name_for("+15125550199") is None
    assert resolver.label_for("+15125550199") == "+15125550199"
    assert resolver.label_for(None) is None


def _address_book(root, name, rows):
    """Build a minimal AddressBook source database."""
    source = root / "Sources" / name
    source.mkdir(parents=True)
    conn = sqlite3.connect(source / "AddressBook-v22.abcddb")
    conn.executescript(
        """
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT,
            ZLASTNAME TEXT, ZORGANIZATION TEXT);
        CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
            ZFULLNUMBER TEXT);
        CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
            ZADDRESSNORMALIZED TEXT);
        """
    )
    for pk, first, last, org, phone, email in rows:
        conn.execute(
            "INSERT INTO ZABCDRECORD (Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION)"
            " VALUES (?, ?, ?, ?)",
            (pk, first, last, org),
        )
        if phone:
            conn.execute(
                "INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER) VALUES (?, ?)",
                (pk, phone),
            )
        if email:
            conn.execute(
                "INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESSNORMALIZED)"
                " VALUES (?, ?)",
                (pk, email),
            )
    conn.commit()
    conn.close()


def test_load_contacts_merges_every_source(tmp_path):
    """There is one address book per account, not one overall.

    On the reference machine four existed and only one held most of the
    contacts, so reading a single file finds almost nothing.
    """
    _address_book(tmp_path, "AAA", [(1, "Alice", "Example", None, "+1 512-555-0101", None)])
    _address_book(tmp_path, "BBB", [(1, "Bob", "Example", None, "5125550102", None)])

    contacts = load_contacts(tmp_path)

    assert contacts["5125550101"] == "Alice Example"
    assert contacts["5125550102"] == "Bob Example"


def test_load_contacts_uses_organization_when_there_is_no_name(tmp_path):
    _address_book(tmp_path, "AAA", [(1, None, None, "Example Dental", "5125550104", None)])
    assert load_contacts(tmp_path)["5125550104"] == "Example Dental"


def test_load_contacts_reads_email_addresses(tmp_path):
    _address_book(tmp_path, "AAA", [(1, "Dana", "Example", None, None, "dana@example.com")])
    assert load_contacts(tmp_path)["dana@example.com"] == "Dana Example"


def test_load_contacts_survives_an_unreadable_source(tmp_path):
    """A sync in progress must not take the message tools down with it."""
    _address_book(tmp_path, "AAA", [(1, "Alice", "Example", None, "5125550101", None)])
    broken = tmp_path / "Sources" / "BBB"
    broken.mkdir(parents=True)
    (broken / "AddressBook-v22.abcddb").write_text("this is not a database")

    contacts = load_contacts(tmp_path)

    assert contacts["5125550101"] == "Alice Example"


def test_load_contacts_with_no_address_book_at_all(tmp_path):
    assert load_contacts(tmp_path) == {}
    assert ContactResolver({}).name_for("+15125550101") is None
