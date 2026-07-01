"""SNI-based egress proxy: allow outbound HTTPS by DOMAIN, not by IP.

Why: iptables/NetworkPolicy match IP:port, so allowlisting a domain means resolving it to CIDRs —
which goes stale for CDN-backed hosts (rotating IPs) and can never match a URL path. This proxy
filters by the TLS **SNI** (the hostname in the ClientHello), so the allowlist is DOMAINS
(`api.anthropic.com`, `*.openai.com`) — no IP resolution, survives rotation, works under any CNI.

Deployment (transparent): the lane's egress firewall REDIRECTs outbound TCP :443 to this proxy. For
each connection it reads the original destination (SO_ORIGINAL_DST), peeks the TLS ClientHello for the
SNI, and — if the SNI matches the allowlist — splices the bytes to the real `<origIP>:443`. No TLS
termination, no CA: the client's encrypted stream is forwarded untouched. Non-matching SNI (or no SNI)
is dropped. DNS (:53) is allowed to go direct by the base firewall; everything else stays denied.

The parsing + matching are pure functions (unit-tested); the server is stdlib asyncio only.
"""
from __future__ import annotations

import asyncio
import socket
import struct
from typing import Sequence

SO_ORIGINAL_DST = 80  # <linux/netfilter_ipv4.h>


def parse_sni(data: bytes) -> str | None:
    """Extract the SNI host_name from a TLS ClientHello record. Returns None if absent/unparseable."""
    try:
        # TLS record: type(1)=0x16 handshake, version(2), length(2)
        if len(data) < 5 or data[0] != 0x16:
            return None
        pos = 5
        # Handshake: type(1)=0x01 ClientHello, length(3)
        if data[pos] != 0x01:
            return None
        pos += 4
        pos += 2 + 32  # client_version(2) + random(32)
        sid_len = data[pos]; pos += 1 + sid_len          # session_id
        cs_len = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2 + cs_len   # cipher_suites
        comp_len = data[pos]; pos += 1 + comp_len          # compression_methods
        if pos + 2 > len(data):
            return None
        ext_total = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2
        end = pos + ext_total
        while pos + 4 <= end and pos + 4 <= len(data):
            etype = struct.unpack(">H", data[pos:pos + 2])[0]
            elen = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            pos += 4
            if etype == 0x0000:  # server_name
                # server_name_list(2) + entry: type(1)=host_name + name_len(2) + name
                p = pos + 2 + 1
                nlen = struct.unpack(">H", data[p:p + 2])[0]
                p += 2
                return data[p:p + nlen].decode("utf-8", "ignore") or None
            pos += elen
        return None
    except (IndexError, struct.error, UnicodeError):
        return None


def domain_allowed(host: str, patterns: Sequence[str]) -> bool:
    """True if `host` matches any allowlist pattern. Exact (`api.x.com`) or wildcard (`*.x.com`,
    matching one-or-more subdomain labels, NOT the bare apex)."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    for raw in patterns:
        p = (raw or "").strip().lower().rstrip(".")
        if not p:
            continue
        if p.startswith("*."):
            if h.endswith(p[1:]) and h != p[2:]:  # *.x.com → a.x.com, not x.com
                return True
        elif h == p:
            return True
    return False


def is_public_ip(ip: str) -> bool:
    """False for private / loopback / link-local (IMDS) / reserved — no lateral movement or SSRF even
    if the SNI matches. Public destinations only."""
    import ipaddress

    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved
                or a.is_unspecified)


def _original_dst(sock: socket.socket) -> tuple[str, int]:
    """The pre-REDIRECT destination of a transparently-proxied connection (SO_ORIGINAL_DST)."""
    raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    port = struct.unpack(">H", raw[2:4])[0]
    ip = socket.inet_ntoa(raw[4:8])
    return ip, port


async def _splice(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


class SniProxy:
    """Transparent SNI-allowlisting TCP proxy. Deny by default; forward only allowed SNI to orig dst."""

    def __init__(self, patterns: Sequence[str]) -> None:
        self._patterns = list(patterns)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("socket")
        try:
            dst_ip, dst_port = _original_dst(peer)
            if not is_public_ip(dst_ip):        # never proxy to internal/IMDS even if SNI matches
                writer.close()
                return
            hello = await asyncio.wait_for(reader.read(4096), timeout=10)
            sni = parse_sni(hello)
            if not sni or not domain_allowed(sni, self._patterns):
                writer.close()
                return
            up_r, up_w = await asyncio.open_connection(dst_ip, dst_port)
            up_w.write(hello)          # replay the ClientHello we already consumed
            await up_w.drain()
            await asyncio.gather(_splice(reader, up_w), _splice(up_r, writer))
        except (OSError, asyncio.TimeoutError, ConnectionError):
            try:
                writer.close()
            except OSError:
                pass

    async def serve(self, host: str, port: int) -> None:
        server = await asyncio.start_server(self.handle, host, port)
        async with server:
            await server.serve_forever()


def _main(argv: "list[str] | None" = None) -> int:
    """CLI: run the transparent SNI proxy. Domains from RESOLUTO_EGRESS_DOMAINS (comma list)."""
    import argparse
    import os

    p = argparse.ArgumentParser(prog="resoluto_sandbox.egress_proxy")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=3129)
    args = p.parse_args(argv)
    patterns = [d for d in (os.environ.get("RESOLUTO_EGRESS_DOMAINS") or "").split(",") if d.strip()]
    print(f"[egress-proxy] SNI allowlist {patterns} on {args.host}:{args.port}", flush=True)
    asyncio.run(SniProxy(patterns).serve(args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
