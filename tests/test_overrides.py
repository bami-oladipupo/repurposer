import pytest

from repurposer import config, overrides


def test_missing_file_returns_empty(tmp_path):
    assert overrides.load_overrides({"overrides_file": str(tmp_path / "nope.yaml")}) == {}


def test_default_name_resolves_relative_to_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    (tmp_path / "overrides.yaml").write_text("123:\n  action: hold\n", encoding="utf-8")
    assert overrides.load_overrides({}) == {"123": {"action": "hold"}}
    assert overrides.load_overrides({"overrides_file": None}) == {"123": {"action": "hold"}}


def test_invalid_action_raises(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("123:\n  action: delete\n", encoding="utf-8")
    with pytest.raises(overrides.OverridesError, match="unknown action 'delete'"):
        overrides.load_overrides({"overrides_file": str(f)})


def test_invalid_yaml_and_non_mapping_raise(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(overrides.OverridesError, match="mapping"):
        overrides.load_overrides({"overrides_file": str(f)})
    f.write_text("123: [unclosed\n", encoding="utf-8")
    with pytest.raises(overrides.OverridesError, match="not valid YAML"):
        overrides.load_overrides({"overrides_file": str(f)})


def test_keys_are_stringified_and_entries_preserved(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text(
        "7412345678901234567:\n  action: hold\n  yt_title: Custom\n"
        "999:\n"
        "abc:\n  ig_caption: hi\n",
        encoding="utf-8",
    )
    out = overrides.load_overrides({"overrides_file": str(f)})
    assert set(out) == {"7412345678901234567", "999", "abc"}
    assert all(isinstance(k, str) for k in out)
    assert out["7412345678901234567"] == {"action": "hold", "yt_title": "Custom"}
    assert out["999"] == {}
    assert overrides.override_for(out, 7412345678901234567) == {"action": "hold", "yt_title": "Custom"}
    assert overrides.override_for(out, "missing") == {}


def test_empty_file_returns_empty(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("# nothing here\n", encoding="utf-8")
    assert overrides.load_overrides({"overrides_file": str(f)}) == {}
