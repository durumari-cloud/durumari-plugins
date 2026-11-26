"""Synthetic stores only: never read or change the user's Keychain."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from keyring.errors import PasswordSetError

from durumari_mcp import credentials, macos_credentials


class Security:
    def __init__(self):
        self.items = {}
        self.calls = []
        self.update_error = 0
        self.add_error = 0
        self.race = False

    def update(self, service, account, password):
        self.calls.append(("update", service, account))
        if self.update_error:
            return self.update_error
        item = self.items.get((service, account))
        if item is None:
            return -25300
        item["password"] = password
        return 0

    def add(self, service, account, password):
        self.calls.append(("add", service, account))
        if self.add_error:
            return self.add_error
        if self.race:
            self.items[(service, account)] = {
                "password": "other-writer", "acl": ["trusted-other"], "id": "raced"
            }
        if (service, account) in self.items:
            return -25299
        self.items[(service, account)] = {"password": password, "acl": ["creator"], "id": "new"}
        return 0


@pytest.fixture
def store(monkeypatch):
    security = Security()
    monkeypatch.setattr(macos_credentials, "_security", lambda: security)
    return macos_credentials.Keyring(), security


def test_repeated_refresh_preserves_item_identity_and_always_allowed_apps(store):
    keyring, security = store
    item = {"password": "before", "acl": ["python-a", "python-b"], "id": "original"}
    security.items[("project-service", "profile")] = item
    security.items[("project-service", "other-profile")] = deepcopy(item)
    security.items[("other-service", "profile")] = deepcopy(item)
    for value in ("rotation-1", "rotation-2", "rotation-3"):
        keyring.set_password("project-service", "profile", value)
        assert security.items[("project-service", "profile")] is item
        assert item == {"password": value, "acl": ["python-a", "python-b"], "id": "original"}
    assert [call[0] for call in security.calls] == ["update"] * 3
    assert security.items[("project-service", "other-profile")]["password"] == "before"
    assert security.items[("other-service", "profile")]["password"] == "before"


def test_missing_item_is_created_once_and_subsequent_save_updates(store):
    keyring, security = store
    keyring.set_password("service", "profile", "first")
    item = security.items[("service", "profile")]
    keyring.set_password("service", "profile", "second")
    assert security.items[("service", "profile")] is item
    assert item["password"] == "second"
    assert [call[0] for call in security.calls] == ["update", "add", "update"]


def test_concurrent_create_updates_winner_without_replacing_its_permissions(store):
    keyring, security = store
    security.race = True
    keyring.set_password("service", "profile", "replacement")
    assert security.items[("service", "profile")] == {
        "password": "replacement", "acl": ["trusted-other"], "id": "raced"
    }
    assert [call[0] for call in security.calls] == ["update", "add", "update"]


@pytest.mark.parametrize("status", [-128, -25293, -25308, -50])
def test_update_denial_or_failure_never_recreates_or_changes_item(store, status):
    keyring, security = store
    item = {"password": "before", "acl": ["python-b"], "id": "original"}
    security.items[("service", "profile")] = deepcopy(item)
    security.update_error = status
    with pytest.raises(PasswordSetError):
        keyring.set_password("service", "profile", "SYNTHETIC_NOT_FOR_LOGS")
    assert security.items[("service", "profile")] == item
    assert [call[0] for call in security.calls] == ["update"]


@pytest.mark.parametrize("status", [-128, -25293, -25308, -50])
def test_create_failure_is_not_retried_or_exposed(store, status):
    keyring, security = store
    security.add_error = status
    with pytest.raises(PasswordSetError) as error:
        keyring.set_password("service", "profile", "SYNTHETIC_NOT_FOR_LOGS")
    assert not security.items
    assert "SYNTHETIC_NOT_FOR_LOGS" not in str(error.value)
    assert [call[0] for call in security.calls] == ["update", "add"]


def test_duplicate_race_retry_is_bounded_even_if_winner_disappears(store, monkeypatch):
    keyring, security = store
    security.add_error = -25299
    with pytest.raises(PasswordSetError):
        keyring.set_password("service", "profile", "replacement")
    assert [call[0] for call in security.calls] == ["update", "add", "update"]


def test_darwin_selects_permission_preserving_backend(monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    assert isinstance(credentials.os_store(), macos_credentials.Keyring)


def test_native_update_only_changes_data_and_matches_exact_service_account(monkeypatch):
    native = object.__new__(macos_credentials._Security)
    monkeypatch.setattr(native, "_constant", lambda name: name)
    queries = []

    @contextmanager
    def dictionary(values):
        queries.append(values)
        yield values

    monkeypatch.setattr(native, "_dictionary", dictionary)
    native.security = SimpleNamespace(SecItemUpdate=lambda query, changes: 0)
    assert native.update("서비스", "계정", "synthetic\x00value") == 0
    assert queries == [
        {
            "kSecClass": "kSecClassGenericPassword",
            "kSecAttrService": "서비스",
            "kSecAttrAccount": "계정",
        },
        {"kSecValueData": b"synthetic\x00value"},
    ]


def test_native_add_does_not_expand_access_or_change_identity(monkeypatch):
    native = object.__new__(macos_credentials._Security)
    monkeypatch.setattr(native, "_constant", lambda name: name)
    queries = []

    @contextmanager
    def dictionary(values):
        queries.append(values)
        yield values

    monkeypatch.setattr(native, "_dictionary", dictionary)
    native.security = SimpleNamespace(SecItemAdd=lambda query, result: -25299)
    assert native.add("service", "account", "synthetic") == -25299
    assert queries == [{
        "kSecClass": "kSecClassGenericPassword", "kSecAttrService": "service",
        "kSecAttrAccount": "account", "kSecValueData": b"synthetic",
    }]


def test_native_exception_does_not_expose_value_or_retry(store, monkeypatch):
    keyring, security = store

    def broken(*args):
        raise RuntimeError("SYNTHETIC_NOT_FOR_LOGS")

    monkeypatch.setattr(security, "update", broken)
    with pytest.raises(PasswordSetError) as error:
        keyring.set_password("service", "profile", "replacement")
    assert str(error.value) == "keychain_write_failed"
    assert not security.items
