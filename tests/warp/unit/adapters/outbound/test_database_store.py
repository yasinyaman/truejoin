"""Added databases on disk: passwords encrypted, the file the server's alone."""

import stat

import pytest
from cryptography.fernet import Fernet

from truejoin.adapters.outbound.database_store.encrypted_json import (
    DatabaseStoreKeyError,
    EncryptedJsonDatabaseStore,
)
from truejoin.domain.gateway.managed import ManagedDatabase

SALES = ManagedDatabase(
    name="sales",
    type="postgresql",
    database="sales",
    username="app",
    password="s3cret-pass",
    readonly_username="ro",
    readonly_password="ro-pass",
)


def test_passwords_are_encrypted_and_read_back(tmp_path):
    key = Fernet.generate_key()
    path = tmp_path / "databases.json"
    EncryptedJsonDatabaseStore(path, key).save([SALES])
    text = path.read_text()
    assert "s3cret-pass" not in text and "ro-pass" not in text
    assert '"username": "app"' in text  # the rest stays readable
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert EncryptedJsonDatabaseStore(path, key).load() == [SALES]


def test_another_key_cannot_read_them(tmp_path):
    path = tmp_path / "databases.json"
    EncryptedJsonDatabaseStore(path, Fernet.generate_key()).save([SALES])
    with pytest.raises(DatabaseStoreKeyError, match="was the key changed"):
        EncryptedJsonDatabaseStore(path, Fernet.generate_key()).load()


@pytest.mark.parametrize("key", ["", "not-a-key"])
def test_a_missing_or_malformed_key_is_refused(tmp_path, key):
    with pytest.raises(DatabaseStoreKeyError):
        EncryptedJsonDatabaseStore(tmp_path / "d.json", key)


def test_no_file_is_no_databases(tmp_path):
    store = EncryptedJsonDatabaseStore(tmp_path / "d.json", Fernet.generate_key())
    assert store.load() == [] and store.version() is None
