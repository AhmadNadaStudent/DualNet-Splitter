"""
High-Performance Outbound Interface-Bound Proxy Server.
Supports both HTTP CONNECT tunneling (default port 8080) and SOCKS5 (default port 1080).
All outbound connections dynamically bind to the high-speed Ethernet interface IP.
"""

import asyncio
import socket
import struct
import time
from typing import Optional, Callable, Tuple

class TrafficStats:
    def __init__(self):
        self.bytes_sent = 0
        self.bytes_recv = 0
        self.active_conns = 0
        self._last_time = time.time()
        self._last_bytes_recv = 0
        self._last_bytes_sent = 0
        self.current_download_speed = 0.0  # bytes/sec
        self.current_upload_speed = 0.0    # bytes/sec

    def update_speed(self):
        now = time.time()
        elapsed = now - self._last_time
        if elapsed >= 0.8:
            self.current_download_speed = (self.bytes_recv - self._last_bytes_recv) / elapsed
            self.current_upload_speed = (self.bytes_sent - self._last_bytes_sent) / elapsed
            self._last_bytes_recv = self.bytes_recv
            self._last_bytes_sent = self.bytes_sent
            self._last_time = now

class DualNetProxy:
    def __init__(
        self,
        get_outbound_ip_fn: Optional[Callable[[], Optional[str]]] = None,
        default_outbound_ip: Optional[str] = None,
        http_port: int = 8080,
        socks_port: int = 1080
    ):
        self.get_outbound_ip_fn = get_outbound_ip_fn
        self.default_outbound_ip = default_outbound_ip
        self.http_port = http_port
        self.socks_port = socks_port
        self.stats = TrafficStats()
        self._http_server = None
        self._socks_server = None
        self.is_running = False

    def get_effective_outbound_ip(self) -> Optional[str]:
        candidate = None
        if self.get_outbound_ip_fn:
            candidate = self.get_outbound_ip_fn()
        if not candidate:
            candidate = self.default_outbound_ip

        if not candidate or candidate.startswith("169.254") or candidate.startswith("127."):
            return None

        # Verify that candidate IP can actually be bound on this machine right now
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind((candidate, 0))
            s.close()
            return candidate
        except Exception:
            return None

    async def _resolve_ipv4(self, host: str, port: int) -> str:
        """Resolves hostname to IPv4 address to match IPv4 interface binding."""
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
            if infos:
                return infos[0][4][0]
        except Exception:
            pass
        return host

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, is_download: bool):
        buffer_size = 65536
        try:
            while True:
                data = await reader.read(buffer_size)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
                if is_download:
                    self.stats.bytes_recv += len(data)
                else:
                    self.stats.bytes_sent += len(data)
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ---------------- SOCKS5 HANDLER ----------------
    async def _handle_socks(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.stats.active_conns += 1
        try:
            # 1. Greeting
            header = await reader.read(2)
            if len(header) < 2:
                return
            ver, nmethods = header[0], header[1]
            if ver != 5:
                return
            await reader.read(nmethods)
            writer.write(b"\x05\x00")  # No authentication required
            await writer.drain()

            # 2. Connection Request
            req = await reader.read(4)
            if len(req) < 4:
                return
            ver, cmd, _, atyp = req[0], req[1], req[2], req[3]
            if cmd != 1:  # Only CONNECT is supported
                writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return

            dest_addr = ""
            if atyp == 1:  # IPv4
                addr_bytes = await reader.read(4)
                dest_addr = socket.inet_ntoa(addr_bytes)
            elif atyp == 3:  # Domain name
                domain_len = (await reader.read(1))[0]
                domain_bytes = await reader.read(domain_len)
                dest_addr = domain_bytes.decode("latin1", errors="ignore")
            elif atyp == 4:  # IPv6
                addr_bytes = await reader.read(16)
                dest_addr = socket.inet_ntop(socket.AF_INET6, addr_bytes)
            else:
                writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return

            port_bytes = await reader.read(2)
            dest_port = struct.unpack("!H", port_bytes)[0]

            out_ip = self.get_effective_outbound_ip()
            target_host = dest_addr
            local_addr = None

            if out_ip:
                target_host = await self._resolve_ipv4(dest_addr, dest_port)
                local_addr = (out_ip, 0)

            try:
                remote_reader, remote_writer = await asyncio.open_connection(
                    target_host, dest_port, local_addr=local_addr
                )
            except Exception:
                # If bound connection failed, fallback to system default without local_addr
                try:
                    remote_reader, remote_writer = await asyncio.open_connection(target_host, dest_port)
                except Exception:
                    writer.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")  # Host unreachable
                    await writer.drain()
                    return

            # Success reply
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()

            # Bidirectional pipe
            await asyncio.gather(
                self._pipe(reader, remote_writer, is_download=False),
                self._pipe(remote_reader, writer, is_download=True)
            )
        except Exception:
            pass
        finally:
            self.stats.active_conns -= 1
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ---------------- HTTP / HTTPS CONNECT HANDLER ----------------
    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.stats.active_conns += 1
        try:
            line = await reader.readline()
            if not line:
                return
            first_line = line.decode("latin1", errors="ignore").strip()
            parts = first_line.split()
            if len(parts) < 2:
                return

            method = parts[0].upper()
            target = parts[1]
            out_ip = self.get_effective_outbound_ip()

            if method == "CONNECT":
                # HTTPS Tunneling
                if ":" in target:
                    host, port_str = target.split(":", 1)
                    port = int(port_str)
                else:
                    host = target
                    port = 443

                # Read headers
                while True:
                    hdr = await reader.readline()
                    if not hdr or hdr in (b"\r\n", b"\n"):
                        break

                target_host = host
                local_addr = None
                if out_ip:
                    target_host = await self._resolve_ipv4(host, port)
                    local_addr = (out_ip, 0)

                try:
                    remote_reader, remote_writer = await asyncio.open_connection(
                        target_host, port, local_addr=local_addr
                    )
                except Exception:
                    # Fallback to default routing if local_addr failed
                    try:
                        remote_reader, remote_writer = await asyncio.open_connection(target_host, port)
                    except Exception:
                        writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                        await writer.drain()
                        return

                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

                await asyncio.gather(
                    self._pipe(reader, remote_writer, is_download=False),
                    self._pipe(remote_reader, writer, is_download=True)
                )
            else:
                # Plain HTTP Proxy
                from urllib.parse import urlparse
                parsed = urlparse(target if "://" in target else f"http://{target}")
                host = parsed.hostname or ""
                port = parsed.port or 80
                path = parsed.path or "/"
                if parsed.query:
                    path += f"?{parsed.query}"

                headers = [f"{method} {path} HTTP/1.1\r\n".encode("latin1")]
                while True:
                    hdr = await reader.readline()
                    if not hdr or hdr in (b"\r\n", b"\n"):
                        break
                    headers.append(hdr)
                headers.append(b"\r\n")

                target_host = host
                local_addr = None
                if out_ip:
                    target_host = await self._resolve_ipv4(host, port)
                    local_addr = (out_ip, 0)

                try:
                    remote_reader, remote_writer = await asyncio.open_connection(
                        target_host, port, local_addr=local_addr
                    )
                except Exception:
                    try:
                        remote_reader, remote_writer = await asyncio.open_connection(target_host, port)
                    except Exception:
                        writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                        await writer.drain()
                        return

                remote_writer.writelines(headers)
                await remote_writer.drain()

                await asyncio.gather(
                    self._pipe(reader, remote_writer, is_download=False),
                    self._pipe(remote_reader, writer, is_download=True)
                )
        except Exception:
            pass
        finally:
            self.stats.active_conns -= 1
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self) -> Tuple[bool, str]:
        try:
            self._http_server = await asyncio.start_server(
                self._handle_http, "127.0.0.1", self.http_port
            )
            self._socks_server = await asyncio.start_server(
                self._handle_socks, "127.0.0.1", self.socks_port
            )
            self.is_running = True
            return True, f"Proxy engine active: HTTP on {self.http_port}, SOCKS5 on {self.socks_port}."
        except OSError as e:
            await self.stop()
            self.is_running = False
            if getattr(e, 'errno', None) == 10048 or "10048" in str(e):
                return False, f"Port {self.http_port} atau {self.socks_port} sedang dipakai proses lain (Errno 10048)."
            return False, f"Gagal menjalankan proxy server: {e}"
        except Exception as e:
            await self.stop()
            self.is_running = False
            return False, f"Gagal menjalankan proxy server: {e}"

    async def stop(self):
        self.is_running = False
        try:
            if self._http_server:
                self._http_server.close()
                await self._http_server.wait_closed()
        except Exception:
            pass
        try:
            if self._socks_server:
                self._socks_server.close()
                await self._socks_server.wait_closed()
        except Exception:
            pass
