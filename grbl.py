"""GRBL 1.x serial driver for the Etcher Laser Pro.

Protocol facts below were observed from SmartDIYsCreator 2.1.1 talking to the
machine (FTDI FT232R, VID 0403 / PID 6001, 921600 bps, firmware banner "Grbl 1.2.1"):

  status poll   : "?" every 500 ms -> "<Idle|MPos:x,y,z|FS:f,s|WCO:..|Ov:..>"
  home          : "$H\n"           -> "ok" once homing finishes
  go to origin  : "$J=X0.00Y0.00F5500\n" (jog in work coordinates)
  stop          : "!" (feed hold) then 0x18 (soft reset) -> "Grbl 1.2.1 [...]"
  vendor extra  : 0x13 -> 0x06, then "C03\n" 0x11 -> 0x06 (sent after each
                  operation by the vendor app; meaning of C03 not confirmed, so
                  this driver never sends it)
  power-on      : firmware is silent until it receives XON (0x11), then acks
                  0x06 and answers normally. "$I" -> [VER:1.2.1.20220106:Etcher Laser Pro]
                  MPos reads 0,0 after power-on without homing, so home first.
"""

import re
import threading
import time
from dataclasses import dataclass, field

import serial
import serial.tools.list_ports

FTDI_VID, FTDI_PID = 0x0403, 0x6001
STATUS_RE = re.compile(r"<([^|>]+)\|(.*)>")


@dataclass
class Status:
    state: str = "Unknown"
    mpos: tuple = (0.0, 0.0, 0.0)
    wco: tuple = (0.0, 0.0, 0.0)
    feed: float = 0.0
    spindle: float = 0.0
    overrides: tuple = (100, 100, 100)
    pins: str = ""
    updated: float = 0.0
    wco_known: bool = False

    @property
    def wpos(self):
        return tuple(round(m - w, 3) for m, w in zip(self.mpos, self.wco))

    def as_dict(self):
        return {
            "state": self.state,
            "machine_pos": self.mpos,
            "work_pos": self.wpos,
            "work_offset": self.wco,
            "feed": self.feed,
            "laser_s": self.spindle,
            "overrides_pct": self.overrides,
            "pins": self.pins,
            "age_s": round(time.time() - self.updated, 2) if self.updated else None,
        }


@dataclass
class Job:
    path: str = ""
    total: int = 0
    sent: int = 0
    acked: int = 0
    errors: list = field(default_factory=list)
    state: str = "idle"  # idle | running | done | failed | stopped
    started: float = 0.0
    finished: float = 0.0


class GrblError(RuntimeError):
    pass


def find_port():
    for p in serial.tools.list_ports.comports():
        if p.vid == FTDI_VID and p.pid == FTDI_PID:
            return p.device
    return None


class Grbl:
    def __init__(self):
        self.ser = None
        self.port = None
        self.baud = None
        self.status = Status()
        self.settings = {}
        self.banner = ""
        self.job = Job()
        self._wlock = threading.Lock()
        self._cmd_lock = threading.Lock()
        self._acks = []
        self._ack_cv = threading.Condition()
        self._log = []
        self._stop = threading.Event()
        self._abort_job = threading.Event()
        self._reader = None
        self._poller = None

    # ---- connection -------------------------------------------------------
    @property
    def connected(self):
        return self.ser is not None and self.ser.is_open

    def connect(self, port=None, baud=921600, timeout=3.0):
        if self.connected:
            return
        port = port or find_port()
        if not port:
            raise GrblError("Etcher Laser (FTDI 0403:6001) のCOMポートが見つかりません。USBケーブルを確認してください。")
        try:
            self.ser = serial.Serial(port, baud, timeout=0.1, write_timeout=2)
        except serial.SerialException as e:
            raise GrblError(f"{port} を開けません。SmartDIYsCreator など他のソフトが使用中の可能性があります: {e}")
        self.port, self.baud = port, baud
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # After power-on the Etcher firmware stays silent until it gets XON
        # (0x11); it answers 0x06 and then behaves like normal GRBL.
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._write(b"\x11?")
            time.sleep(0.25)
            if self.status.updated:
                break
        if not self.status.updated:
            self.disconnect()
            raise GrblError(f"{port} ({baud}bps) から応答がありません。本体の電源とボーレートを確認してください。")
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()
        # WCO is only included in every Nth status report; wait for it so work
        # coordinates are right from the first command.
        deadline = time.time() + 6
        while not self.status.wco_known and time.time() < deadline:
            time.sleep(0.1)
        try:
            self.read_settings()
        except GrblError:
            pass

    def disconnect(self):
        self._stop.set()
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    # ---- low level --------------------------------------------------------
    def _write(self, data: bytes):
        if not self.connected:
            raise GrblError("未接続です。先に laser_connect を呼んでください。")
        with self._wlock:
            self.ser.write(data)

    def _read_loop(self):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self.ser.read(256)
            except Exception:
                break
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle(line.strip(b"\r\x06").decode("ascii", "replace"))

    def _handle(self, line):
        if not line:
            return
        m = STATUS_RE.match(line)
        if m:
            self._parse_status(m.group(1), m.group(2))
            return
        self._log.append((time.time(), line))
        del self._log[:-200]
        if line == "ok" or line.startswith("error:"):
            with self._ack_cv:
                self._acks.append(line)
                self._ack_cv.notify_all()
        elif line.startswith("ALARM:"):
            self.status.state = "Alarm"
            with self._ack_cv:
                self._acks.append(line)
                self._ack_cv.notify_all()
        elif line.startswith("Grbl "):
            self.banner = line
            with self._ack_cv:
                self._acks.append("reset")
                self._ack_cv.notify_all()
        elif line.startswith("$") and "=" in line:
            k, v = line[1:].split("=", 1)
            self.settings[k] = v.split(" ")[0]

    def _parse_status(self, state, rest):
        s = self.status
        s.state = state
        for part in rest.split("|"):
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            nums = v.split(",")
            try:
                if k == "MPos":
                    s.mpos = tuple(float(n) for n in nums)
                elif k == "WPos":
                    w = tuple(float(n) for n in nums)
                    s.mpos = tuple(a + b for a, b in zip(w, s.wco))
                elif k == "WCO":
                    s.wco = tuple(float(n) for n in nums)
                    s.wco_known = True
                elif k == "FS":
                    s.feed, s.spindle = float(nums[0]), float(nums[1])
                elif k == "F":
                    s.feed = float(nums[0])
                elif k == "Ov":
                    s.overrides = tuple(int(n) for n in nums)
                elif k == "Pn":
                    s.pins = v
            except ValueError:
                pass
        s.pins = s.pins if "Pn:" in rest else ""
        s.updated = time.time()

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self._write(b"?")
            except Exception:
                break
            time.sleep(0.25)

    # ---- commands ---------------------------------------------------------
    def command(self, line, timeout=10.0):
        """Send one G-code/$ line and wait for ok / error / alarm."""
        line = line.strip()
        with self._cmd_lock:
            with self._ack_cv:
                self._acks.clear()
            self._write((line + "\n").encode("ascii"))
            with self._ack_cv:
                if not self._ack_cv.wait_for(lambda: self._acks, timeout):
                    raise GrblError(f"'{line}' の応答がタイムアウトしました ({timeout}s)")
                reply = self._acks.pop(0)
        if reply != "ok":
            raise GrblError(f"'{line}' -> {reply}")
        return reply

    def realtime(self, byte: bytes):
        self._write(byte)

    def wait_idle(self, timeout=120.0):
        t0 = time.time()
        time.sleep(0.4)
        while time.time() - t0 < timeout:
            if self.status.state in ("Idle", "Alarm") and time.time() - self.status.updated < 1:
                return self.status.state
            time.sleep(0.1)
        raise GrblError("動作完了待ちがタイムアウトしました")

    def read_settings(self):
        self.command("$$", timeout=5)
        time.sleep(0.2)
        return dict(self.settings)

    def recent_lines(self, n=30):
        return [line for _, line in self._log[-n:]]

    def travel(self):
        try:
            return float(self.settings["130"]), float(self.settings["131"])
        except (KeyError, ValueError):
            return None

    def soft_reset(self):
        self._abort_job.set()
        self.realtime(b"!")
        time.sleep(0.05)
        with self._ack_cv:
            self._acks.clear()
        self.realtime(b"\x18")
        with self._ack_cv:
            self._ack_cv.wait_for(lambda: "reset" in self._acks, 3.0)

    # ---- job streaming ----------------------------------------------------
    def start_job(self, path, lines):
        if self.job.state == "running":
            raise GrblError("別のジョブが実行中です")
        self._abort_job.clear()
        self.job = Job(path=path, total=len(lines), state="running", started=time.time())
        threading.Thread(target=self._stream, args=(lines,), daemon=True).start()

    RX_BUFFER = 120  # GRBL serial RX buffer is 128 bytes; keep a margin

    def _stream(self, lines):
        """Character-counting streaming: keep GRBL's RX buffer filled so the
        planner never starves (stalls leave the beam dwelling on one spot)."""
        job = self.job
        inflight = []  # byte lengths of lines sent but not yet acknowledged
        with self._cmd_lock:
            with self._ack_cv:
                self._acks.clear()
            try:
                i = 0
                while i < len(lines) or inflight:
                    if self._abort_job.is_set():
                        job.state = "stopped"
                        return
                    if i < len(lines):
                        data = (lines[i] + "\n").encode("ascii")
                        if sum(inflight) + len(data) <= self.RX_BUFFER:
                            self._write(data)
                            inflight.append(len(data))
                            i += 1
                            job.sent = i
                            continue
                    with self._ack_cv:
                        if not self._ack_cv.wait_for(lambda: self._acks, 120):
                            raise GrblError("応答がタイムアウトしました (120s)")
                        reply = self._acks.pop(0)
                    if reply == "reset":
                        job.state = "stopped"
                        return
                    if reply.startswith("ALARM"):
                        job.errors.append(reply)
                        job.state = "failed"
                        return
                    inflight.pop(0)
                    job.acked += 1
                    if reply != "ok":
                        job.errors.append(f"line {job.acked}: {lines[job.acked - 1]} -> {reply}")
            except Exception as e:
                job.errors.append(str(e))
                job.state = "failed"
                job.finished = time.time()
                return
        try:
            self.wait_idle(timeout=600)
            job.state = "done" if not job.errors else "failed"
        except Exception as e:
            job.errors.append(str(e))
            job.state = "failed"
        finally:
            job.finished = time.time()
