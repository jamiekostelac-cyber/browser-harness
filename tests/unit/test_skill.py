from importlib import resources


def _frontmatter(text: str) -> str:
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    assert end != -1
    return text[4:end]


def test_packaged_skill_frontmatter_is_valid_simple_yaml():
    text = resources.files("browser_harness").joinpath("SKILL.md").read_text()
    metadata = {}

    for line in _frontmatter(text).splitlines():
        key, separator, value = line.partition(":")
        assert separator == ":", line
        assert key in {"name", "description"}
        assert key.strip() == key
        value = value.strip()
        assert value, key

        if value[0] in {"'", '"'}:
            assert value[-1] == value[0], line
            parsed = value[1:-1]
        else:
            parsed = value
            assert ": " not in parsed, line

        metadata[key] = parsed

    assert metadata == {
        "name": "browser-harness",
        "description": "Use Titan's isolated headless browser runner for new web automation. Use titan-doctor only to diagnose the local 9333 transport.",
    }


def test_packaged_skill_scopes_shared_browser_to_legacy_workflows():
    text = resources.files("browser_harness").joinpath("SKILL.md").read_text()
    assert "Each job starts a new headless Playwright process" in text
    assert "Legacy Shared-Browser Reference" in text
    assert "Legacy browser-harness daemon and interaction references (not for new jobs)" in text
    assert "chrome://inspect/#remote-debugging" in text
