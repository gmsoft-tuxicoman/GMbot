import base64

from gmbot.irc import irc_lower, parse_line, sasl_plain_chunks, strip_formatting, truncate_utf8


def test_parse_privmsg_with_prefix_and_tags():
    m = parse_line("@time=2026-01-01T00:00:00Z;account=bob :bob!b@host PRIVMSG #chan :hello there :)")
    assert m.tags == {"time": "2026-01-01T00:00:00Z", "account": "bob"}
    assert m.nick == "bob"
    assert m.command == "PRIVMSG"
    assert m.params == ["#chan", "hello there :)"]


def test_parse_ping_and_numeric():
    assert parse_line("PING :abc").params == ["abc"]
    m = parse_line(":srv 353 me = #c :@op +voice plain")
    assert m.command == "353" and m.params[-1] == "@op +voice plain"


def test_parse_cap_ls_multiline():
    m = parse_line(":srv CAP * LS * :sasl=PLAIN,EXTERNAL multi-prefix")
    assert m.params == ["*", "LS", "*", "sasl=PLAIN,EXTERNAL multi-prefix"]


def test_sasl_plain_payload():
    chunks = sasl_plain_chunks("user", "pass")
    assert len(chunks) == 1
    assert base64.b64decode(chunks[0]) == b"user\0user\0pass"


def test_sasl_plain_chunking_exact_multiple():
    # 300 raw bytes -> 400 base64 chars exactly -> needs a "+" terminator
    pw = "x" * (300 - len("u\0u\0"))
    chunks = sasl_plain_chunks("u", pw)
    assert chunks == [chunks[0], "+"] and len(chunks[0]) == 400


def test_truncate_utf8_does_not_split_chars():
    s = "é" * 10  # 2 bytes each
    out = truncate_utf8(s, 5)
    assert out == "éé" and len(out.encode()) <= 5


def test_irc_lower_rfc1459():
    assert irc_lower("Nick[]\\~") == "nick{}|^"


def test_strip_formatting():
    assert strip_formatting("\x02bold\x02 \x0304,01red\x03 x") == "bold red x"


def test_realname_from_owner(tmp_path):
    from gmbot.config import load_config

    p = tmp_path / "c.toml"
    p.write_text('[irc]\nowner = "guy"\ndescription = "llm bot"\n')
    assert load_config(p).irc.realname == "llm bot | owner: guy"
    p.write_text('[irc]\nowner = "guy"\nrealname = "custom"\n')
    assert load_config(p).irc.realname == "custom"
