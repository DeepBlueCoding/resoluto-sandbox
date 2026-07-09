"""The SNI egress proxy's pure logic: TLS ClientHello SNI parsing + wildcard domain matching."""

import struct

from resoluto.sandbox.egress_proxy import domain_allowed, is_public_ip, parse_sni


def test_is_public_ip_blocks_internal():
    assert is_public_ip("160.79.104.10")  # Anthropic — public
    assert is_public_ip("1.1.1.1")
    assert not is_public_ip("10.1.2.3")  # RFC1918
    assert not is_public_ip("192.168.1.5")
    assert not is_public_ip("172.16.0.9")
    assert not is_public_ip("127.0.0.1")  # loopback
    assert not is_public_ip("169.254.169.254")  # IMDS / link-local
    assert not is_public_ip("not-an-ip")


def _client_hello(sni: str | None) -> bytes:
    """Build a minimal TLS ClientHello record, optionally carrying an SNI extension."""
    if sni is not None:
        name = sni.encode()
        entry = b"\x00" + struct.pack(">H", len(name)) + name  # host_name type + len + name
        sni_list = struct.pack(">H", len(entry)) + entry  # server_name_list
        ext = b"\x00\x00" + struct.pack(">H", len(sni_list)) + sni_list  # ext 0x0000 + len + data
        exts = struct.pack(">H", len(ext)) + ext
    else:
        exts = struct.pack(">H", 0)  # no extensions
    body = (
        b"\x03\x03"
        + b"\x00" * 32
        + b"\x00"  # version + random + session_id(0)
        + b"\x00\x02\x00\x2f"  # cipher_suites (len 2 + 1 suite)
        + b"\x01\x00"  # compression (len 1 + null)
        + exts
    )
    hs = b"\x01" + struct.pack(">I", len(body))[1:] + body  # handshake type 1 + 3-byte len
    return b"\x16\x03\x01" + struct.pack(">H", len(hs)) + hs  # TLS record


def test_parse_sni_extracts_hostname():
    assert parse_sni(_client_hello("api.anthropic.com")) == "api.anthropic.com"
    assert parse_sni(_client_hello("registry.npmjs.org")) == "registry.npmjs.org"


def test_parse_sni_none_when_absent_or_garbage():
    assert parse_sni(_client_hello(None)) is None
    assert parse_sni(b"") is None
    assert parse_sni(b"\x16\x03\x01\x00\x02\xff\xff") is None  # truncated / not a ClientHello
    assert parse_sni(b"not tls at all") is None


def test_domain_allowed_exact():
    assert domain_allowed("api.anthropic.com", ["api.anthropic.com"])
    assert not domain_allowed("evil.com", ["api.anthropic.com"])
    assert not domain_allowed("api.anthropic.com.evil.com", ["api.anthropic.com"])


def test_domain_allowed_wildcard():
    pats = ["*.openai.com"]
    assert domain_allowed("api.openai.com", pats)
    assert domain_allowed("eu.api.openai.com", pats)
    assert not domain_allowed("openai.com", pats)  # bare apex NOT matched by *.
    assert not domain_allowed("notopenai.com", pats)
    assert not domain_allowed("openai.com.evil.com", pats)


def test_domain_allowed_case_and_trailing_dot():
    assert domain_allowed("API.Anthropic.Com.", ["api.anthropic.com"])
    assert domain_allowed("api.anthropic.com", ["  API.ANTHROPIC.COM  "])


def test_domain_allowed_empty_inputs():
    assert not domain_allowed("", ["api.anthropic.com"])
    assert not domain_allowed("api.anthropic.com", [])
    assert not domain_allowed("api.anthropic.com", ["", "  "])


def test_load_domains_file(tmp_path):
    from resoluto.sandbox.egress_proxy import load_domains_file

    f = tmp_path / "domains"
    f.write_text("api.anthropic.com, *.openai.com\nregistry.npmjs.org")
    assert load_domains_file(str(f)) == ["api.anthropic.com", "*.openai.com", "registry.npmjs.org"]
    f.write_text("")
    assert load_domains_file(str(f)) == []  # empty => deny-all
    assert load_domains_file(str(tmp_path / "missing")) == []


async def test_kata_apply_egress_writes_live_allowlist_file(tmp_path):
    # per-run egress on the local runtime = writing the proxy's live allowlist file (no re-provision)
    from resoluto.sandbox.egress_proxy import load_domains_file
    from resoluto.sandbox.runtime.kata_nerdctl import KataNerdctlSandboxRuntime

    f = tmp_path / "egress-domains"
    rt = KataNerdctlSandboxRuntime(
        address="x",
        namespace="n",
        conduit_host_dir=str(tmp_path),
        runtime="io.containerd.kata.v2",
        egress_domains_file=str(f),
    )
    await rt.apply_egress(["api.anthropic.com", " registry.npmjs.org "])
    assert load_domains_file(str(f)) == ["api.anthropic.com", "registry.npmjs.org"]
    await rt.clear_egress()
    assert load_domains_file(str(f)) == []  # cleared => deny-all after the run
