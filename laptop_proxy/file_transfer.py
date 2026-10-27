"""v1.5.45 (beta1.0.1): file-transfer TRIAL — single file, <= 30 GB, Wi-Fi Direct only.

Wire protocol (newline-terminated ASCII headers, then raw bytes), port 8765:
    -> "PAIR <user> <pass>\n"               <- "PAIRED\n" | "ERR busy\n"   (beta1.0.3)
    -> "UNPAIR <user> <pass>\n"             <- "UNPAIRED\n"                  (beta1.0.3)
    -> "AUTH <user> <pass>\n"               <- "OK\n" | "ERR auth\n" | "ERR pair\n"
    -> "OFFER <nameB64> <size> <sha256>\n"  <- "ACCEPT\n" | "RESUME <offset>\n"
                                              | "REJECT <reason>\n"   (beta1.0.58)
    -> <raw file bytes>                     <- "DONE\n" | "FAIL <reason>\n"

beta1.0.58: RESUMABLE TRANSFERS. The receiver keeps a .part staging file (and
a .meta sidecar with name/size/sha256) between attempts. A dropped
connection no longer destroys progress: the receiver answers "RESUME
<offset>" and the sender seeks there. The sidecar is refreshed atomically
every few MB, so an interrupted transfer resumes from a recent checkpoint
rather than from zero — or from a stale offset that no longer matches the
.part. Partials survive a cancelled/failed transfer so the next attempt
continues, but are deleted when the receiver stops (app exit / receiver
off), on a size/SHA-256 mismatch, or when the meta no longer matches the
new offer. Limit raised 1 GB -> 10 GB -> 30 GB (beta1.0.62).

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
import time

# beta1.0.8 relay: optional dependency — the transfer module must keep
# working even when bundled standalone (relay missing -> no backoff).
try:
    from relay import RELAY as _RELAY

    def _relay_backoff():
        return _RELAY.transfer_backoff()
except ImportError:  # pragma: no cover
    def _relay_backoff():
        return 0.0

FT_PORT = 8765
FT_MAX_BYTES = 30_000_000_000
# beta1.0.85: adaptive chunk sizing (user request: raise the multi-MB/s
# ceiling to tens of MB/s, cap 100MB/s). Small files keep small chunks
# (responsive progress, low memory); big files get 4MB chunks so a single
# sendall can saturate the link instead of paying per-128KB syscall tax.
# 100 MB/s ceiling is enforced on the SENDER by pacing (never rely on the
# link alone), so the cap holds on fast Wi-Fi Direct too.
FT_CHUNK_SMALL = 128 * 1024        # files < 64 MB
FT_CHUNK_LARGE = 4 * 1024 * 1024   # files >= 64 MB
FT_SPEED_CAP_BYTES = 100 * 1024 * 1024  # 100 MB/s hard ceiling


def _chunk_size_for(total: int) -> int:
    return FT_CHUNK_LARGE if total >= 64 * 1024 * 1024 else FT_CHUNK_SMALL
# The .meta sidecar is refreshed every N bytes (atomically, via os.replace)
# so an interrupted transfer resumes from a recent checkpoint rather than
# from zero — or from a stale offset that no longer matches the .part.
FT_META_INTERVAL = 8 * 1024 * 1024
# Bound on how long the accept loop waits for one in-flight transfer to
# unwind. Must stay far below FT_MAX_BYTES (seconds): a 30 GB value overflows
# the C timestamp conversion inside Thread.join and kills the listener.
FT_JOIN_TIMEOUT = 6 * 3600


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
            b = f.read(FT_CHUNK_LARGE)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sanitize_name(name):
    """Keep the sender's file name intact — Chinese included.

    beta1.0.59 filtered everything outside 0x20..0x7E, which silently ate
    every CJK character: "微信图片_2024.jpg" became ".jpg" and a fully
    Chinese name degenerated into "file.bin". Only path separators, control
    characters and the Windows-invalid set are removed now.
    """
    base = name.replace("\\", "/").split("/")[-1].strip()
    cleaned = "".join(
        c for c in base
        if ord(c) >= 0x20 and c not in '\\/:*?"<>|'
    ).rstrip(" .")
    return cleaned[:200] or "file"


# Magic-byte -> extension. beta1.0.61: a name that lost its extension (or
# still carries a generic .bin) is renamed to the REAL format detected from
# the file's own header, so "the phone sent a .jpg" can never land as .bin.
_MAGIC_EXTENSIONS = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"\x00\x00\x01\x00", ".ico"),
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),
    (b"PK\x05\x06", ".zip"),
    (b"Rar!\x1a\x07", ".rar"),
    (b"7z\xbc\xaf\x27\x1c", ".7z"),
    (b"\x1f\x8b", ".gz"),
    (b"RIFF", ".webp"),          # confirmed by the WEBP fourcc below
    (b"ID3", ".mp3"),
    (b"\xff\xfb", ".mp3"),
    (b"\xff\xf3", ".mp3"),
    (b"\xff\xf2", ".mp3"),
    (b"fLaC", ".flac"),
    (b"OggS", ".ogg"),
    (b"\xd0\xcf\x11\xe0", ".doc"),
    (b"SQLite format 3\x00", ".db"),
)

# extensions that carry no format information — worth re-sniffing
_GENERIC_EXTENSIONS = {"", ".bin", ".file", ".dat", ".raw"}


def sniff_extension(path):
    """Best-effort real format from the file's magic bytes ('' if unknown)."""
    try:
        with open(path, "rb") as fin:
            head = fin.read(32)
    except Exception:
        return ""
    if len(head) < 4:
        return ""
    if head.startswith(b"RIFF"):
        return ".webp" if head[8:12] == b"WEBP" else ""
    if head.startswith(b"ftyp") or (len(head) >= 12 and head[4:8] == b"ftyp"):
        brand = head[8:12]
        if brand[:2] == b"qt":
            return ".mov"
        return ".mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):     # EBML / Matroska
        return ".mkv"
    for magic, ext in _MAGIC_EXTENSIONS:
        if head.startswith(magic):
            return ext
    return ""


def _with_real_extension(path):
    """Rename `path` to the format its content really is, when the current
    name has no (or only a generic) extension. Returns the final path.
    """
    stem, ext = os.path.splitext(path)
    if ext.lower() not in _GENERIC_EXTENSIONS:
        return path
    real = sniff_extension(path)
    if not real or real.lower() == ext.lower():
        return path
    candidate = stem + real
    n = 1
    while os.path.exists(candidate):
        candidate = "%s (%d)%s" % (stem, n, real)
        n += 1
    try:
        os.replace(path, candidate)
        return candidate
    except Exception:
        return path


def send_file(target_ip, user, password, file_path, progress_cb=None,
              cancel_event=None, log_fn=None):
    """Send one file to a receiver (the Android host by default).

    Raises FileTransferError on any refusal / IO failure. The size limit is
    enforced here (sender) and again by the receiver — a too-large file is
    refused before a single byte moves.
    """
    name = sanitize_name(os.path.basename(file_path))
    # beta1.0.61: an extension-less file gets its REAL format from the
    # magic bytes, so the receiver never has to guess (.bin).
    if os.path.splitext(name)[1].lower() in _GENERIC_EXTENSIONS:
        real = sniff_extension(file_path)
        if real:
            name = sanitize_name(os.path.splitext(name)[0] + real)
    size = os.path.getsize(file_path)
    if size > FT_MAX_BYTES:
        raise FileTransferError(
            f"File exceeds the 30 GB limit ({size:,} bytes)")
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
        # beta1.0.58: "RESUME <offset>" means a verified .part of the SAME
        # file exists on the receiver — continue from there.
        offset = 0
        if reply and reply.startswith("RESUME "):
            try:
                offset = int(reply.split(" ", 1)[1])
            except ValueError:
                raise FileTransferError(f"Bad resume reply: {reply!r}")
            if offset < 0 or offset >= size:
                raise FileTransferError(f"Bad resume offset: {offset}")
            if log_fn:
                log_fn(f"File transfer: resuming {name} at {offset:,}/{size:,}")
        elif reply != "ACCEPT":
            raise FileTransferError(f"Receiver refused: {reply or 'no reply'}")
        sent = offset
        # beta1.0.85: TCP_NODELAY + adaptive chunks + 100MB/s pacing cap.
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        chunk_size = _chunk_size_for(size)
        if log_fn and chunk_size > FT_CHUNK_SMALL:
            log_fn(f"File transfer: large file, {chunk_size // (1024 * 1024)}MB chunks")
        # beta1.0.85: precise byte-budget pacing. A simple "sleep after
        # sending the slice quota" still overshoots because sendall can
        # buffer far ahead in the kernel. Budget approach: never let bytes
        # sent run ahead of (elapsed * cap) + one chunk of slack.
        cap_bytes_per_sec = FT_SPEED_CAP_BYTES
        pace_origin = time.monotonic()
        # half-chunk slack: measured overshoot on loopback was exactly one
        # chunk (101MB/s with chunk slack); half keeps us under the cap while
        # still allowing a burst to fill the pipe.
        slack = chunk_size // 2
        last_progress = 0
        with open(file_path, "rb") as f:
            if offset:
                f.seek(offset)
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise FileTransferError("Cancelled")
                backoff = _relay_backoff()
                if backoff:
                    time.sleep(backoff)
                # byte-budget pacing: block until sent <= elapsed*cap + slack
                elapsed = time.monotonic() - pace_origin
                budget = elapsed * cap_bytes_per_sec + slack
                if sent - offset > budget:
                    wait = ((sent - offset) - budget) / cap_bytes_per_sec
                    if wait > 0:
                        time.sleep(min(wait, 0.05))
                        continue  # re-check (cancellation, relay backoff)
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                sock.sendall(chunk)
                sent += len(chunk)
                if progress_cb and sent - last_progress >= 1024 * 1024:
                    progress_cb(sent, size)
                    last_progress = sent
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
    """Windows-side receiver: files saved to the picked directory.

    beta1.0.61:
    - The listener binds 0.0.0.0 (never a Wi-Fi Direct lease): the lease
      changes whenever the group is rebuilt, and a listener pinned to a
      vanished address refused every later connect. Access stays gated by
      AUTH + explicit one-tap pairing; no adapter/WLAN/route state is
      touched anywhere in this class.
    - The listener stays ARMED for any number of files until the user
      cancels or closes the dialog (the old one-shot behaviour turned the
      second file into ECONNREFUSED).
    - Files are published under the SENDER's name, with the real format
      detected from the content when the name lost its extension.
    """

    def __init__(self, bind_ip, user, password, save_path, log_fn=None):
        self.bind_ip = bind_ip
        self.user = user
        self.password = password
        self.save_path = save_path
        # beta1.0.61: save_path may be a DIRECTORY (the picker now returns a
        # folder — the file name always comes from the sender) or a legacy
        # file path whose directory is used.
        self._save_dir = (save_path if os.path.isdir(save_path)
                          else (os.path.dirname(save_path) or "."))
        self.log_fn = log_fn
        self._stop = threading.Event()
        self._thread = None
        self._sock = None
        self._last_target = None   # real file path of the last/current offer

    def _target_path(self, name):
        """Save under the SENDER's file name, in the directory the user
        picked (the picked path itself is only directory + placeholder).
        Never silently overwrites: an existing file gets a (1) suffix."""
        directory = self._save_dir
        base = sanitize_name(name)
        candidate = os.path.join(directory, base)
        stem, ext = os.path.splitext(base)
        n = 1
        while os.path.exists(candidate):
            candidate = os.path.join(directory, "%s (%d)%s" % (stem, n, ext))
            n += 1
        return candidate

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
        # beta1.0.58: exiting removes the staging buffer entirely — a fresh
        # start never silently resumes an old session. beta1.0.59: the
        # staging pair follows the REAL target (the sender's file name),
        # not the placeholder path the user picked.
        targets = {self.save_path}
        if self._last_target:
            targets.add(self._last_target)
        for target in targets:
            for p in (target + ".part", target + ".part.meta"):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

    def _run(self):
        srv = None
        try:
            # beta1.0.61: ALWAYS bind the wildcard. Binding the Wi-Fi Direct
            # lease looked tidy but the lease changes whenever the group is
            # rebuilt — a listener pinned to a vanished address silently
            # refused every later connect (the reported "偶尔也会断开").
            # Access is still gated by AUTH + explicit pairing, and the
            # Android receiver only accepts private-LAN sources.
            # No adapter/WLAN/route state is touched — this is a plain
            # loopback+wildcard TCP listener.
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", FT_PORT))
            srv.listen(4)
            srv.settimeout(1.0)
            self._sock = srv
            if self.log_fn:
                self.log_fn(f"File transfer: receiver listening on "
                            f"0.0.0.0:{FT_PORT} (armed until cancelled)")
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
                t.join(timeout=FT_JOIN_TIMEOUT)  # one in-flight transfer is bounded
                # beta1.0.3: PAIR/UNPAIR handshakes do NOT consume the receive
                # slot. beta1.0.58: a RESUMABLE interruption (peer exit,
                # dropped link) must NOT end it either — the .part is kept and
                # the very next attempt continues from the receiver's offset.
                # beta1.0.61: a COMPLETED transfer no longer disarms the
                # listener — the receiver stays armed for the next file until
                # the user cancels or closes the dialog (the old one-shot
                # behaviour turned the 2nd file into ECONNREFUSED).
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
        part_path = None
        meta_path = None
        keep_part = False  # beta1.0.62: bound before any early raise (the
        # oversize/malformed-offer paths bail before the resume block below
        # used to assign it, and the except handler reads it — that raised
        # UnboundLocalError and skipped the staging cleanup)
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
            # beta1.0.58: only a transfer that can NOT be resumed marks the
            # receive slot as consumed (see ReceiverServer._run). A dropped
            # mid-file connection leaves the listener armed for the resume.
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
            # beta1.0.8 relay: a real, authenticated offer = transfer
            # activity; the coordinator wakes for this receive only.
            try:
                _RELAY.claim(_RELAY.TRANSFER, "ft_receiver")
            except Exception:
                pass
            # beta1.0.59: stage under the SENDER's file name (the path the
            # user picked only supplies the directory). This is what fixes
            # "the phone sent a .jpg but the PC saved received.bin".
            target = self._target_path(name)
            self._last_target = target
            part_path = target + ".part"
            meta_path = part_path + ".meta"
            if size < 0 or size > FT_MAX_BYTES:
                _send_line(conn, "REJECT size")
                raise FileTransferError(
                    f"Refused {name}: {size} bytes exceeds the 30 GB limit")
            # beta1.0.58: resumable staging. A .part whose sidecar meta
            # matches THIS offer (name/size/sha) continues at its length;
            # anything stale is discarded and the transfer restarts clean.
            offset = 0
            keep_part = False
            if os.path.isfile(part_path) and os.path.isfile(meta_path):
                try:
                    with open(meta_path, encoding="utf-8") as fin:
                        meta = json.load(fin)
                    part_size = os.path.getsize(part_path)
                    checkpoint = meta.get("part_size") if isinstance(meta, dict) else None
                    if (isinstance(meta, dict)
                            and meta.get("name") == name
                            and meta.get("size") == size
                            and str(meta.get("sha", "")).lower() == expected_sha
                            and isinstance(checkpoint, int)
                            and 0 < checkpoint < size
                            and part_size >= checkpoint):
                        # The sidecar is the authority: an abrupt drop flushes
                        # everything buffered, so the .part may reach past the
                        # last checkpoint — trim the uncheckpointed tail.
                        offset = checkpoint
                        if part_size > offset:
                            with open(part_path, "r+b") as trunc:
                                trunc.truncate(offset)
                except Exception:
                    offset = 0
            if offset:
                _send_line(conn, f"RESUME {offset}")
            else:
                for p in (part_path, meta_path):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass
                _send_line(conn, "ACCEPT")
            if self.log_fn:
                self.log_fn(f"File transfer: receiving {name} ({size:,} bytes"
                            + (f", resuming at {offset:,}" if offset else "") + ")")
            h = hashlib.sha256()
            received = offset
            keep_part = True  # a mid-file failure from here on KEEPS progress

            def _write_meta(part_size):
                tmp = meta_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fout:
                    json.dump({"name": name, "size": size, "sha": expected_sha,
                               "part_size": part_size}, fout)
                os.replace(tmp, meta_path)

            with open(part_path, "ab") as out:
                if offset:
                    with open(part_path, "rb") as existing:
                        while True:
                            b = existing.read(_chunk_size_for(size))
                            if not b:
                                break
                            h.update(b)
                since_meta = 0
                # beta1.0.85: receive loop uses the same adaptive chunking
                # as the sender so a 100MB/s sender isn't throttled by a
                # 128KB-recv reader; meta flush interval scales with it too.
                recv_chunk = _chunk_size_for(size)
                while received < size:
                    want = min(recv_chunk, size - received)
                    chunk = conn.recv(want)
                    if not chunk:
                        raise FileTransferError("Connection closed mid-file")
                    out.write(chunk)
                    h.update(chunk)
                    received += len(chunk)
                    since_meta += len(chunk)
                    if since_meta >= FT_META_INTERVAL * 4:
                        out.flush()
                        _write_meta(received)
                        since_meta = 0
                out.flush()
            actual = h.hexdigest()
            if actual != expected_sha:
                _send_line(conn, "FAIL sha256")
                keep_part = False
                raise FileTransferError("SHA-256 mismatch; partial file discarded")
            os.replace(part_path, target)
            for p in (part_path, meta_path):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            # beta1.0.61: "你要转码，转换成实际文件和格式" — a name with no
            # extension (or a generic .bin) is renamed to what the bytes
            # really are (jpg/png/mp4/zip/...), on BOTH ends.
            final = _with_real_extension(target)
            if final != target:
                target = final
                self._last_target = final
            _send_line(conn, "DONE")
            outcome["transfer"] = True
            if self.log_fn:
                self.log_fn(f"File transfer: saved and verified -> {target}")
        except Exception as exc:
            # beta1.0.58: a failure WITH a staged partial is resumable — the
            # listener stays armed; anything else consumes the slot.
            if not keep_part:
                outcome["transfer"] = True
            try:
                if not keep_part and part_path:
                    # only a non-resumable failure wipes the staging pair;
                    # a mid-file drop keeps BOTH the .part and its .meta
                    if os.path.exists(part_path):
                        os.remove(part_path)
                    if meta_path and os.path.exists(meta_path):
                        os.remove(meta_path)
            except Exception:
                pass
            try:
                _send_line(conn, f"FAIL {exc}")
            except Exception:
                pass
            if self.log_fn:
                self.log_fn(f"File transfer receive failed: {exc}")
        finally:
            # beta1.0.8 relay: receive finished — release the transfer lane.
            try:
                _RELAY.release(_RELAY.TRANSFER, "ft_receiver")
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
