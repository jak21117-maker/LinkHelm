"""v1.5.45 (beta1.0.1): file-transfer TRIAL — single file, <= 1 GB, Wi-Fi Direct only.

Wire protocol (newline-terminated ASCII headers, then raw bytes), port 8765:
    -> "AUTH <user> <pass>\n"               <- "OK\n" | "ERR auth\n"
    -> "OFFER <nameB64> <size> <sha256>\n"  <- "ACCEPT\n" | "REJECT <reason>\n"
    -> <raw file bytes>                     <- "DONE\n" | "FAIL <reason>\n"
Closing the socket at any point cancels the transfer; the receiver deletes
its partial file. The channel is independent of the SOCKS5 exit: files never
touch the phone's uplink and the transfer cannot change system routes.
"""
import base64
import hashlib
import os
import socket
import threading

FT_PORT = 8765
FT_MAX_BYTES = 1_000_000_000
FT_CHUNK = 128 * 1024


class FileTransferError(Exception):
    pass


def _recv_line(sock):
    buf = bytearray()
    while True:
        c = sock.recv(1)
        if not c:
            return None
        if c == b"\n":
            return buf.decode("utf-8", "replace")
        if c != b"\r":
            buf += c
        if len(buf) > 8192:
            raise IOError("header line too long")


def _send_line(sock, line):
    sock.sendall((line + "\n").encode("utf-8"))


def local_ip_toward(target_ip):
    """The local adapter address used to reach target_ip (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 9))
        return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"
    finally:
        s.close()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(FT_CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sanitize_name(name):
    base = name.replace("\\", "/").split("/")[-1].strip()
    cleaned = "".join(
        c for c in base if 0x20 <= ord(c) <= 0x7E and c not in '\\/:*?"<>|'
    )
    return cleaned or "file.bin"


def send_file(target_ip, user, password, file_path, progress_cb=None,
              cancel_event=None, log_fn=None):
    """Send one file to a receiver (the Android host by default).

    Raises FileTransferError on any refusal / IO failure. The size limit is
    enforced here (sender) and again by the receiver — a too-large file is
    refused before a single byte moves.
    """
    name = sanitize_name(os.path.basename(file_path))
    size = os.path.getsize(file_path)
    if size > FT_MAX_BYTES:
        raise FileTransferError(
            f"File exceeds the 1 GB trial limit ({size:,} bytes)")
    if log_fn:
        log_fn(f"File transfer: sending {name} ({size:,} bytes) to {target_ip}:{FT_PORT}")
    digest = sha256_file(file_path)
    sock = socket.create_connection((target_ip, FT_PORT), timeout=8)
    try:
        sock.settimeout(60)
        _send_line(sock, f"AUTH {user} {password}")
        if _recv_line(sock) != "OK":
            raise FileTransferError("Authentication rejected by the receiver")
        name_b64 = base64.b64encode(name.encode("utf-8")).decode("ascii")
        _send_line(sock, f"OFFER {name_b64} {size} {digest}")
        reply = _recv_line(sock)
        if reply != "ACCEPT":
            raise FileTransferError(f"Receiver refused: {reply or 'no reply'}")
        sent = 0
        with open(file_path, "rb") as f:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise FileTransferError("Cancelled")
                chunk = f.read(FT_CHUNK)
                if not chunk:
                    break
                sock.sendall(chunk)
                sent += len(chunk)
                if progress_cb:
                    progress_cb(sent, size)
        sock.settimeout(120)
        done = _recv_line(sock)
        if done != "DONE":
            raise FileTransferError(f"Receiver reported: {done or 'no reply'}")
        if log_fn:
            log_fn(f"File transfer: {name} delivered and verified (SHA-256)")
    finally:
        try:
            sock.close()
        except Exception:
            pass


class ReceiverServer:
    """Windows-side receiver: one file per session, saved to a pre-picked path.

    Binds the local adapter address that faces the phone, so the listener is
    only reachable over the Wi-Fi Direct link. Stops after one transfer (or
    on stop()).
    """

    def __init__(self, bind_ip, user, password, save_path, log_fn=None):
        self.bind_ip = bind_ip
        self.user = user
        self.password = password
        self.save_path = save_path
        self.log_fn = log_fn
        self._stop = threading.Event()
        self._thread = None
        self._sock = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="FileTransfer-Receiver", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def _run(self):
        srv = None
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.bind_ip, FT_PORT))
            srv.listen(1)
            srv.settimeout(1.0)
            self._sock = srv
            if self.log_fn:
                self.log_fn(f"File transfer: receiver listening on "
                            f"{self.bind_ip}:{FT_PORT} (one file, then it stops)")
            while not self._stop.is_set():
                try:
                    conn, _addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                t = threading.Thread(target=self._handle, args=(conn,), daemon=True)
                t.start()
                t.join(timeout=FT_MAX_BYTES)  # a trial transfer is bounded
                break
        except Exception as exc:
            if self.log_fn:
                self.log_fn(f"File transfer receiver stopped: {exc}")
        finally:
            try:
                if srv:
                    srv.close()
            except Exception:
                pass
            self._sock = None

    def _handle(self, conn):
        part_path = self.save_path + ".part"
        try:
            conn.settimeout(60)
            auth = _recv_line(conn)
            parts = (auth or "").split(" ")
            if len(parts) != 3 or parts[0] != "AUTH" or \
                    parts[1] != self.user or parts[2] != self.password:
                _send_line(conn, "ERR auth")
                raise FileTransferError("Authentication failed")
            _send_line(conn, "OK")
            offer = (_recv_line(conn) or "").split(" ")
            if len(offer) != 4 or offer[0] != "OFFER":
                _send_line(conn, "REJECT bad-offer")
                raise FileTransferError("Malformed offer")
            try:
                name = sanitize_name(base64.b64decode(offer[1]).decode("utf-8", "replace"))
                size = int(offer[2])
                expected_sha = offer[3].lower()
            except Exception:
                _send_line(conn, "REJECT bad-offer")
                raise FileTransferError("Malformed offer fields")
            if size < 0 or size > FT_MAX_BYTES:
                _send_line(conn, "REJECT size")
                raise FileTransferError(
                    f"Refused {name}: {size} bytes exceeds the 1 GB trial limit")
            _send_line(conn, "ACCEPT")
            if self.log_fn:
                self.log_fn(f"File transfer: receiving {name} ({size:,} bytes)")
            h = hashlib.sha256()
            received = 0
            with open(part_path, "wb") as out:
                while received < size:
                    want = min(FT_CHUNK, size - received)
                    chunk = conn.recv(want)
                    if not chunk:
                        raise FileTransferError("Connection closed mid-file")
                    out.write(chunk)
                    h.update(chunk)
                    received += len(chunk)
            actual = h.hexdigest()
            if actual != expected_sha:
                _send_line(conn, "FAIL sha256")
                raise FileTransferError("SHA-256 mismatch; partial file discarded")
            os.replace(part_path, self.save_path)
            _send_line(conn, "DONE")
            if self.log_fn:
                self.log_fn(f"File transfer: saved and verified -> {self.save_path}")
        except Exception as exc:
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except Exception:
                pass
            try:
                _send_line(conn, f"FAIL {exc}")
            except Exception:
                pass
            if self.log_fn:
                self.log_fn(f"File transfer receive failed: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
