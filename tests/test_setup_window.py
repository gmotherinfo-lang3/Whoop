"""The first-run window's logic.

The window itself is a few widgets; what matters is what it writes. These
cover the parts that touch the config file and decide whether setup is needed
at all, because getting those wrong means someone's tuned config is clobbered
or the window never appears when it should.
"""
import pytest

from tray.setup_config import (existing_server, needs_setup, normalise_server,
                               set_config_value)

CONFIG = '''# a comment worth keeping
[device]
address = ""
live_hr = true

[forward]
forward_url = "https://strap.example.com/ingest"
forward_token = ""
'''


def write(tmp_path, text=CONFIG):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


# --- server address normalising ---------------------------------------------
@pytest.mark.parametrize("typed,expected", [
    ("strap.example.com", "https://strap.example.com"),
    ("https://strap.example.com", "https://strap.example.com"),
    ("https://strap.example.com/", "https://strap.example.com"),
    ("  strap.example.com/ingest  ", "https://strap.example.com"),
    ("http://192.168.1.9:8000", "http://192.168.1.9:8000"),
])
def test_however_the_address_is_typed(typed, expected):
    assert normalise_server(typed) == expected


def test_the_server_is_prefilled_from_the_download(tmp_path):
    assert existing_server(write(tmp_path)) == "https://strap.example.com"


def test_a_missing_or_broken_config_prefills_nothing(tmp_path):
    assert existing_server(tmp_path / "nope.toml") == ""
    assert existing_server(write(tmp_path, "this is not toml {{{")) == ""


# --- writing one value ------------------------------------------------------
def test_the_strap_address_is_written(tmp_path):
    path = write(tmp_path)
    set_config_value(path, "device", "address", "AA:BB:CC:DD:EE:FF")
    assert 'address = "AA:BB:CC:DD:EE:FF"' in path.read_text()


def test_everything_else_survives(tmp_path):
    """The file stays readable and hand-editable, so it is edited, not rebuilt."""
    path = write(tmp_path)
    set_config_value(path, "device", "address", "AA:BB:CC:DD:EE:FF")
    text = path.read_text()
    assert "# a comment worth keeping" in text
    assert "live_hr = true" in text
    assert 'forward_url = "https://strap.example.com/ingest"' in text


def test_the_right_section_is_written(tmp_path):
    """Both sections could have a key of the same name."""
    path = write(tmp_path, '[device]\nname = "old"\n\n[forward]\nname = "other"\n')
    set_config_value(path, "forward", "name", "new")
    text = path.read_text()
    assert 'name = "old"' in text and 'name = "new"' in text


def test_a_missing_key_is_added_to_its_section(tmp_path):
    path = write(tmp_path, '[device]\nlive_hr = true\n\n[forward]\nforward_url = ""\n')
    set_config_value(path, "device", "address", "AA:BB")
    from whoop_bridge.config import Config
    assert Config.load(path).address == "AA:BB"


def test_a_missing_section_is_created(tmp_path):
    path = write(tmp_path, "[forward]\nforward_url = \"\"\n")
    set_config_value(path, "device", "address", "AA:BB")
    from whoop_bridge.config import Config
    assert Config.load(path).address == "AA:BB"


def test_the_result_is_still_valid_toml(tmp_path):
    import tomllib
    path = write(tmp_path)
    set_config_value(path, "device", "address", "AA:BB:CC:DD:EE:FF")
    with path.open("rb") as fh:
        tomllib.load(fh)


# --- when the window should appear ------------------------------------------
def test_setup_is_needed_when_there_is_no_config(tmp_path):
    assert needs_setup(tmp_path / "nothing.toml")


def test_setup_is_needed_before_pairing(tmp_path):
    assert needs_setup(write(tmp_path))


def test_setup_is_needed_when_paired_but_no_strap_chosen(tmp_path):
    path = write(tmp_path)
    set_config_value(path, "forward", "forward_token", "a-key")
    assert needs_setup(path)


def test_setup_is_done_once_both_are_set(tmp_path):
    path = write(tmp_path)
    set_config_value(path, "forward", "forward_token", "a-key")
    set_config_value(path, "device", "address", "AA:BB:CC:DD:EE:FF")
    assert not needs_setup(path)


def test_a_corrupt_config_asks_for_setup_rather_than_crashing(tmp_path):
    assert needs_setup(write(tmp_path, "not toml at all {{{"))


# --- pairing codes ----------------------------------------------------------
# Read off one screen and typed into another, so however it arrives it should
# work -- and it is normalised before it leaves the laptop, so an older server
# that does not tidy it up still accepts it.
@pytest.mark.parametrize("typed", [
    "K7M2-9QX4", "k7m2-9qx4", "K7M29QX4", " k7m2 9qx4 ", "k7m2_9qx4", "K7M2 - 9QX4",
])
def test_a_code_is_accepted_however_it_is_typed(typed):
    from whoop_bridge.setup_config import normalise_code
    assert normalise_code(typed) == "K7M2-9QX4"


def test_something_the_wrong_length_is_not_dressed_up_as_a_code():
    """Only an eight-character code gets the dash, so a typo stays visibly a
    typo rather than being reformatted into something that looks right."""
    from whoop_bridge.setup_config import normalise_code
    assert normalise_code("") == ""
    assert normalise_code("short") == "SHORT"
    assert normalise_code("waytoolongforacode") == "WAYTOOLONGFORACODE"


def test_the_window_is_only_needed_until_both_halves_are_set(tmp_path):
    """What decides whether first-run setup appears at all."""
    from whoop_bridge.setup_config import needs_setup, set_config_value
    path = write(tmp_path)
    assert needs_setup(path)
    set_config_value(path, "forward", "forward_token", "a-key")
    assert needs_setup(path), "a key without a strap is not set up"
    set_config_value(path, "device", "address", "AA:BB:CC:DD:EE:FF")
    assert not needs_setup(path)
