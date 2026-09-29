from io import StringIO
import os
import sys
from types import SimpleNamespace

import pytest

from gepa_engine import cli
from gepa_engine.config import default_settings, make_connection, save_settings, with_connection
from gepa_engine.providers import ProviderError, SecretStore
from gepa_engine import credential_ui
from gepa_engine.config import ConfigError
from gepa_engine.doctor import _credential_step


def setup_connection(tmp_path):
    settings = with_connection(default_settings(tmp_path), make_connection(identifier="router", provider="openrouter", model="chosen/model"))
    save_settings(settings)
    return settings


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows only")
def test_ui_dispatch_saves_encrypted_without_reading_stdin(tmp_path, monkeypatch):
    settings = setup_connection(tmp_path)
    fake_key = "fake-credential-only-for-test"
    def capture(connection, save):
        assert connection.id == "router"
        save(fake_key)
        return True
    monkeypatch.setattr(cli, "capture_credential", capture, raising=False)
    out, err = StringIO(), StringIO()
    assert cli.main(["--home", str(tmp_path), "setup", "connection", "set-key", "router", "--ui"], stdout=out, stderr=err, stdin=StringIO("unused")) == 0
    assert SecretStore(settings.secrets_path).get("router") == fake_key
    assert fake_key not in settings.secrets_path.read_text()
    assert fake_key not in settings.config_path.read_text() + out.getvalue() + err.getvalue()


def test_nonpersistent_store_rejected_before_capture_or_stdin(tmp_path, monkeypatch):
    setup_connection(tmp_path)
    monkeypatch.setattr(cli, "open_secret_store", lambda settings: type("MemoryStore", (), {"persistent": False})())
    monkeypatch.setattr(cli, "capture_credential", lambda *args: pytest.fail("must not capture"), raising=False)
    out, err = StringIO(), StringIO()
    assert cli.main(["--home", str(tmp_path), "setup", "connection", "set-key", "router", "--ui"], stdout=out, stderr=err, stdin=StringIO()) == 2
    assert "--api-key-env" in err.getvalue()


def test_cancel_does_not_save(tmp_path, monkeypatch):
    settings = setup_connection(tmp_path)
    monkeypatch.setattr(cli, "open_secret_store", lambda settings: SimpleNamespace(persistent=True, set=lambda *args: pytest.fail("must not save")))
    monkeypatch.setattr(cli, "capture_credential", lambda *args: False, raising=False)
    out, err = StringIO(), StringIO()
    assert cli.main(["--home", str(tmp_path), "setup", "connection", "set-key", "router", "--ui"], stdout=out, stderr=err) == 1
    assert not settings.secrets_path.exists()
    assert "cancelada" in out.getvalue()


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows only")
def test_stdin_remains_compatible(tmp_path):
    settings = setup_connection(tmp_path)
    out, err = StringIO(), StringIO()
    assert cli.main(["--home", str(tmp_path), "setup", "connection", "set-key", "router"], stdout=out, stderr=err, stdin=StringIO("fake-test-key\n")) == 0
    assert SecretStore(settings.secrets_path).get("router") == "fake-test-key"
    assert "fake-test-key" not in out.getvalue() + err.getvalue()


def test_tk_missing_has_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "tkinter", None)
    connection = make_connection(identifier="router", provider="openrouter", model="chosen/model")
    with pytest.raises(ConfigError, match="Tcl/Tk"):
        credential_ui.capture_credential(connection, lambda key: pytest.fail("must not save"))


def test_no_display_has_actionable_error(monkeypatch):
    class DisplayError(Exception):
        pass
    def unavailable():
        raise DisplayError("fake-display-error")
    monkeypatch.setitem(sys.modules, "tkinter", SimpleNamespace(Tk=unavailable, TclError=DisplayError, ttk=object()))
    connection = make_connection(identifier="router", provider="openrouter", model="chosen/model")
    with pytest.raises(ConfigError, match="sesión de escritorio"):
        credential_ui.capture_credential(connection, lambda key: pytest.fail("must not save"))


def test_doctor_recommends_ui_only_when_persistent():
    connection = make_connection(identifier="router", provider="openrouter", model="chosen/model")
    assert "set-key router --ui" in _credential_step(connection, True)
    assert "--ui" not in _credential_step(connection, False)


class FakeTk:
    """Run the production event callbacks without creating any OS window."""
    def __init__(self, monkeypatch, interact):
        self.widgets, self.buttons, self.bindings, self.protocols = [], {}, {}, {}
        self.exists = True
        self.interact = interact
        harness = self

        class Widget:
            def __init__(self, parent, **options):
                self.options = options
                harness.widgets.append(self)
            def pack(self, **options):
                pass
            def configure(self, **options):
                self.options.update(options)

        class Entry(Widget):
            value = ""
            def get(self):
                return self.value
            def delete(self, start, end):
                self.value = ""
            def focus_force(self):
                pass

        class Button(Widget):
            def __init__(self, parent, **options):
                super().__init__(parent, **options)
                harness.buttons[options["text"]] = options["command"]

        ttk = SimpleNamespace(Frame=Widget, Label=Widget, Entry=Entry, Button=Button)
        monkeypatch.setitem(sys.modules, "tkinter", SimpleNamespace(Tk=lambda: self, TclError=RuntimeError, ttk=ttk, END="end"))
        self.entry_type = Entry

    @property
    def entry(self):
        return next(widget for widget in self.widgets if isinstance(widget, self.entry_type))

    def title(self, title):
        pass
    def resizable(self, *args):
        pass
    def protocol(self, name, callback):
        self.protocols[name] = callback
    def bind(self, name, callback):
        self.bindings[name] = callback
    def after(self, delay, callback):
        callback()
    def mainloop(self):
        self.interact(self)
    def destroy(self):
        self.exists = False
    def winfo_exists(self):
        return self.exists


def test_real_ui_submit_masks_clears_and_saves(monkeypatch):
    def interact(window):
        assert window.entry.options["show"] == "*"
        window.entry.value = " fake-ui-test-key "
        window.bindings["<Return>"]()
    window = FakeTk(monkeypatch, interact)
    values = []
    connection = make_connection(identifier="router", provider="openrouter", model="chosen/model")
    assert credential_ui.capture_credential(connection, values.append)
    assert values == ["fake-ui-test-key"]
    assert window.entry.value == ""
    assert not window.exists


@pytest.mark.parametrize("cancel", ["button", "escape", "window-close"])
def test_real_ui_cancel_clears_without_save(monkeypatch, cancel):
    def interact(window):
        window.entry.value = "fake-cancel-key"
        if cancel == "button":
            window.buttons["Cancelar"]()
        elif cancel == "escape":
            window.bindings["<Escape>"](None)
        else:
            window.protocols["WM_DELETE_WINDOW"]()
    window = FakeTk(monkeypatch, interact)
    connection = make_connection(identifier="router", provider="openrouter", model="chosen/model")
    assert not credential_ui.capture_credential(connection, lambda key: pytest.fail("must not save"))
    assert window.entry.value == ""
    assert not window.exists


def test_real_ui_error_allows_retry_without_exposing_key(monkeypatch, capsys):
    attempts = []
    def save(key):
        attempts.append(key)
        if len(attempts) == 1:
            raise ProviderError("fake-sensitive-error-key")
    def interact(window):
        window.buttons["Guardar"]()
        assert not attempts
        window.entry.value = "fake-first-key"
        window.buttons["Guardar"]()
        assert window.exists
        assert "No se pudo guardar" in str([widget.options for widget in window.widgets])
        assert "fake-first-key" not in str([widget.options for widget in window.widgets])
        assert "fake-sensitive-error-key" not in str([widget.options for widget in window.widgets])
        window.entry.value = "fake-second-key"
        window.buttons["Guardar"]()
    window = FakeTk(monkeypatch, interact)
    connection = make_connection(identifier="router", provider="openrouter", model="chosen/model")
    assert credential_ui.capture_credential(connection, save)
    assert attempts == ["fake-first-key", "fake-second-key"]
    assert window.entry.value == ""
    assert capsys.readouterr() == ("", "")
