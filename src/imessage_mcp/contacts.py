"""Resolve iMessage handles to contact names.

Handles arrive as phone numbers in whatever shape the sender's device recorded
(``+15125550100``, ``5125550100``, ``(512) 555-0100``) or as email addresses.
The AddressBook stores them in equally varied shapes, so matching is done on a
normalized form rather than on the literal text.

Contacts are read straight out of the AddressBook sqlite files. That needs only
Full Disk Access, which is already required to read the message database --
verified on macOS 26.4.1, where a process holding Full Disk Access and no
Contacts grant read the address book without a prompt. Going through the
Contacts API instead would add a second permission, and permission prompts
cannot be answered on an unattended host.
"""

import logging
import pathlib
import sqlite3

logger = logging.getLogger(__name__)

ADDRESS_BOOK_ROOT = (
    pathlib.Path.home() / "Library" / "Application Support" / "AddressBook"
)

# Enough digits to identify a number, few enough to survive differences in
# country code and trunk prefix between how a contact is stored and how the
# handle arrived.
_MATCH_DIGITS = 10


def normalize_handle(handle: str) -> str:
    """Reduce a handle to a comparable key.

    Email addresses fold to lowercase. Phone numbers reduce to their last ten
    digits, which is what makes ``+15125550100``, ``15125550100`` and
    ``(512) 555-0100`` compare equal.
    """
    handle = handle.strip()
    if "@" in handle:
        return handle.casefold()

    digits = "".join(character for character in handle if character.isdigit())
    if not digits:
        return handle.casefold()
    return digits[-_MATCH_DIGITS:]


def _source_databases(root: pathlib.Path) -> list[pathlib.Path]:
    """Every AddressBook database under ``root``.

    There is one per account, plus a top-level one. On the reference machine
    four existed: one held 917 records, one held 100, and two were empty stubs.
    Reading only the top-level file finds almost nothing.
    """
    found = sorted(root.glob("Sources/*/AddressBook-v22.abcddb"))
    top_level = root / "AddressBook-v22.abcddb"
    if top_level.exists():
        found.append(top_level)
    return found


_QUERY = """
    SELECT r.ZFIRSTNAME AS first, r.ZLASTNAME AS last, r.ZORGANIZATION AS org,
           v.ZFULLNUMBER AS value
      FROM ZABCDPHONENUMBER v
      JOIN ZABCDRECORD r ON r.Z_PK = v.ZOWNER
     WHERE v.ZFULLNUMBER IS NOT NULL
    UNION ALL
    SELECT r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, e.ZADDRESSNORMALIZED
      FROM ZABCDEMAILADDRESS e
      JOIN ZABCDRECORD r ON r.Z_PK = e.ZOWNER
     WHERE e.ZADDRESSNORMALIZED IS NOT NULL
"""


def _display_name(first: str | None, last: str | None, org: str | None) -> str | None:
    name = " ".join(part for part in (first, last) if part)
    return name or org or None


def load_contacts(root: pathlib.Path | None = None) -> dict[str, str]:
    """Build a mapping of normalized handle to contact name.

    A source that cannot be read is skipped rather than fatal. An address book
    locked by a sync in progress must not take the message tools down with it.
    """
    root = root if root is not None else ADDRESS_BOOK_ROOT
    resolved: dict[str, str] = {}

    for database in _source_databases(root):
        try:
            conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        except sqlite3.Error:
            logger.debug("could not open address book %s", database.name, exc_info=True)
            continue
        try:
            for first, last, org, value in conn.execute(_QUERY):
                name = _display_name(first, last, org)
                if not name or not value:
                    continue
                resolved.setdefault(normalize_handle(value), name)
        except sqlite3.Error:
            logger.debug("could not read address book %s", database.name, exc_info=True)
        finally:
            conn.close()

    return resolved


class ContactResolver:
    """Resolves handles to names, loading the address book once."""

    def __init__(self, contacts: dict[str, str] | None = None) -> None:
        self._contacts = contacts if contacts is not None else load_contacts()

    def name_for(self, handle: str | None) -> str | None:
        if not handle:
            return None
        return self._contacts.get(normalize_handle(handle))

    def label_for(self, handle: str | None) -> str | None:
        """The name if known, otherwise the handle itself."""
        if not handle:
            return None
        return self.name_for(handle) or handle
