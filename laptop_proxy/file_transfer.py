"""v1.5.45 (beta1.0.1): file-transfer TRIAL — single file, <= 1 GB, Wi-Fi Direct only.

Wire protocol (newline-terminated ASCII headers, then raw bytes), port 8765:
    -> "PAIR <user> <pass>\n"               <- "PAIRED\n" | "ERR busy\n"   (beta1.0.3)
    -> "UNPAIR <user> <pass>\n"             <- "UNPAIRED\n"                  (beta1.0.3)
    -> "AUTH <user> <pass>\n"               <- "OK\n" | "ERR auth\n" | "ERR pair\n"
    -> "OFFER <nameB64> <size> <sha256>\n"  <- "ACCEPT\n" | "REJECT <reason>\n"
    -> <raw file bytes>                     <- "DONE\n" | "FAIL <reason>\n"
Closing the socket at any point cancels the transfer; the receiver deletes
its partial file. The channel is independent of the SOCKS5 exit: files never
touch the phone's uplink and the transfer cannot change system routes.

beta1.0.3: IP PAIRING. A transfer is only allowed between mutually paired
devices: the peer's IP must be in this device's paired set AND this device's
IP must be in the peer's set. The PAIR handshake establishes both directions
in one tap — the responder records the requester's IP, and the requester
records the responder's from the successful reply. Paired IPs persist in
ft_paired.json next to the executable (Wi-Fi Direct leases change when the
group is rebuilt; pairing again fixes a stale entry).
"""
import base64
import hashlib
import json
import os
import socket
import sys
import threading

FT_PORT = 8765
FT_MAX_BYTES = 1_000_000_000
FT_CHUNK = 128 * 1024


class FileTransferError(Exception):
    pass


def _pair_store_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "ft_paired.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ft_paired.json")


def paired_ips():
    try:
        with open(_pair_store_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(str(x) for x in data)
    except Exception:
        pass
    return set()


def _save_paired_ips(ips):
    try:
        with open(_pair_store_path(), "w", encoding="utf-8") as f:
            json.dump(sorted(ips), f)
    except Exception:
        pass


def add_paired_ip(ip):
    ips = paired_ips()
    ips.add(ip)
    _save_paired_ips(ips)


def remove_paired_ip(ip):
    ips = paired_ips()
    ips.discard(ip)
    _save_paired_ips(ips)


def is_paired(ip):
    return ip in paired_ips()


def pair_with(target_ip, user, password, log_fn=None):
    """One-tap mutual IP pairing. Returns None on success, else a reason."""
    try:
        sock = socket.create_connection((target_ip, FT_PORT), timeout=5)
    except Exception as exc:
        return (f"无法连接 {target_ip}:{FT_PORT}（{exc}）——"
                "请确认对方 IP 正确且与本机在同一 Wi-Fi Direct 网络")
    try:
        sock.settimeout(5)
        _send_line(sock, f"PAIR {user} {password}")
        reply = _recv_line(sock)
        if reply == "PAIRED":
            add_paired_ip(target_ip)
            if log_fn:
                log_fn(f"File transfer: paired with {target_ip}")
            return None
        if reply == "ERR busy":
            return "对端正忙，请稍后再试"
        return "对端未确认配对（请检查两端版本与凭据）"
    except Exception as exc:
        return f"配对握手失败（{exc}）"
    finally:
        try:
            sock.close()
        except Exception:
            pass


def unpair_with(target_ip, user, password):
    try:
        sock = socket.create_connection((target_ip, FT_PORT), timeout=5)
        try:
            sock.settimeout(5)
            _send_line(sock, f"UNPAIR {user} {password}")
            _recv_line(sock)
        finally:
            sock.close()
    except Exception:
        pass
    remove_paired_ip(target_ip)


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
    # beta1.0.3: pairing is checked BEFORE dialling — a rejected attempt must
    # not consume the receiver's one-shot accept slot.
    if not is_paired(target_ip):
        raise FileTransferError(
            "尚未与对方配对：请先在对话框中输入对方 IP 并点击「配对」")
    sock = socket.create_connection((target_ip, FT_PORT), timeout=8)
    try:
        sock.settimeout(60)
        _send_line(sock, f"AUTH {user} {password}")
        auth_reply = _recv_line(sock)
        if auth_reply == "OK":
            pass
        elif auth_reply == "ERR pair":
            raise FileTransferError(
                "对方尚未与本机配对：请先在两端分别点击「配对」")
        else:
            raise FileTransferError("认证被对端拒绝（请核对 SOCKS5 凭据）")
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
                outcome = {"transfer": False}
                t = threading.Thread(target=self._handle,
                                     args=(conn, outcome), daemon=True)
                t.start()
                t.join(timeout=FT_MAX_BYTES)  # a trial transfer is bounded
                # beta1.0.3: PAIR/UNPAIR handshakes do NOT consume the receive
                # slot — only an actual file transfer ends the wait.
                if outcome["transfer"]:
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

    def _handle(self, conn, outcome):
        part_path = self.save_path + ".part"
        try:
            conn.settimeout(60)
            remote = conn.getpeername()[0]
            first = _recv_line(conn) or ""
            # beta1.0.3: PAIR/UNPAIR handshake — records/removes the
            # requester's IP in this device's paired set.
            if first == f"PAIR {self.user} {self.password}":
                add_paired_ip(remote)
                _send_line(conn, "PAIRED")
                if self.log_fn:
                    self.log_fn(f"File transfer: paired with {remote}")
                return
            if first == f"UNPAIR {self.user} {self.password}":
                remove_paired_ip(remote)
                _send_line(conn, "UNPAIRED")
                if self.log_fn:
                    self.log_fn(f"File transfer: unpaired from {remote}")
                return
            # beta1.0.3: a blank/dropped connection is not a transfer attempt —
            # keep waiting for the real sender.
            if not first:
                return
            outcome["transfer"] = True
            parts = first.split(" ")
            if len(parts) != 3 or parts[0] != "AUTH" or \
                    parts[1] != self.user or parts[2] != self.password:
                _send_line(conn, "ERR auth")
                raise FileTransferError("Authentication failed")
            # beta1.0.3: a transfer requires prior IP pairing on BOTH ends.
            if not is_paired(remote):
                _send_line(conn, "ERR pair")
                raise FileTransferError(
                    f"Refused {remote}: not paired (pair on both ends first)")
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
