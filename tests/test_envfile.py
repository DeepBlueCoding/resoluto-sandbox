from resoluto_sandbox.envfile import parse_env_file


def test_parse_basic_key_value(tmp_path):
    f = tmp_path / ".env"
    f.write_text("ANTHROPIC_API_KEY=sk-abc123\nOPENAI_API_KEY=sk-def456\n")
    assert parse_env_file(str(f)) == {
        "ANTHROPIC_API_KEY": "sk-abc123",
        "OPENAI_API_KEY": "sk-def456",
    }


def test_parse_skips_blank_lines_and_comments(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# a comment\n\nKEY=value\n   \n# another\nKEY2=value2\n")
    assert parse_env_file(str(f)) == {"KEY": "value", "KEY2": "value2"}


def test_parse_strips_matching_quotes(tmp_path):
    f = tmp_path / ".env"
    f.write_text('DOUBLE="quoted value"\nSINGLE=\'quoted value\'\nUNQUOTED=plain\n')
    assert parse_env_file(str(f)) == {
        "DOUBLE": "quoted value",
        "SINGLE": "quoted value",
        "UNQUOTED": "plain",
    }


def test_parse_tolerates_export_prefix(tmp_path):
    f = tmp_path / ".env"
    f.write_text("export KEY=value\nKEY2=value2\n")
    assert parse_env_file(str(f)) == {"KEY": "value", "KEY2": "value2"}


def test_parse_ignores_lines_without_equals(tmp_path):
    f = tmp_path / ".env"
    f.write_text("not-a-kv-line\nKEY=value\n")
    assert parse_env_file(str(f)) == {"KEY": "value"}


def test_parse_empty_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("")
    assert parse_env_file(str(f)) == {}
