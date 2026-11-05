"""Runtime audio graph. Everything is owned by this process.

Two child `pipewire -c <conf>` processes act as clients of the running
PipeWire daemon (the same way the stock filter-chain service does):

  mix   sink "vox_mix" -> source "Voice Mix"      (the virtual microphone)
  voice mic -> effect chain -> vox_mix

Nothing here touches the daemon, the session manager or the default devices.
Children die with the app (PDEATHSIG) and are cleaned up on exit.
"""
import ctypes
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import effects
from .paths import runtime_dir

MIX_SINK = "vox_mix"
MIX_SOURCE = "voice_mix"
MIX_DESC = "Voice Mix"

HEADER = """context.properties = { log.level = 1 }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt  args = { }  flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
"""


def _pdeathsig():
    try:
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def mix_conf() -> str:
    return HEADER + f"""    {{ name = libpipewire-module-filter-chain
      args = {{
        node.description = "{MIX_DESC}"
        filter.graph = {{
          nodes = [
            {{ type = builtin name = cl label = copy }}
            {{ type = builtin name = cr label = copy }}
          ]
          inputs  = [ "cl:In" "cr:In" ]
          outputs = [ "cl:Out" "cr:Out" ]
        }}
        capture.props = {{
          node.name = "{MIX_SINK}"
          node.description = "Voice Mix input (Vox Deck)"
          media.class = Audio/Sink
          audio.rate = 48000
          audio.channels = 2
          audio.position = [ FL FR ]
          priority.session = 1
          priority.driver = 1
        }}
        playback.props = {{
          node.name = "{MIX_SOURCE}"
          node.description = "{MIX_DESC}"
          media.class = Audio/Source
          audio.rate = 48000
          audio.channels = 2
          audio.position = [ FL FR ]
          priority.session = 1
          node.latency = 512/48000
        }}
      }}
    }}
]
"""


def voice_conf(preset: dict, mic: str) -> str:
    props = ""
    if mic.endswith(".monitor"):
        mic, props = mic[:-len(".monitor")], "stream.capture.sink = true"
    graph = effects.build_graph(preset)
    return HEADER + f"""    {{ name = libpipewire-module-filter-chain
      args = {{
        node.description = "Vox Deck voice effects"
        filter.graph = {graph}
        capture.props = {{
          node.name = "vox_voice_in"
          target.object = {json.dumps(mic)}
          node.autoconnect = true
          node.dont-fallback = true
          node.dont-reconnect = true
          state.restore-target = false
          audio.rate = 48000
          audio.channels = 1
          audio.position = [ MONO ]
          node.latency = 512/48000
          {props}
        }}
        playback.props = {{
          node.name = "vox_voice_out"
          target.object = "{MIX_SINK}"
          node.autoconnect = true
          node.dont-fallback = true
          node.dont-reconnect = true
          state.restore-target = false
          audio.rate = 48000
          audio.channels = 1
          audio.position = [ MONO ]
          node.latency = 512/48000
        }}
      }}
    }}
]
"""


def list_mics() -> list:
    """Physical-looking capture sources: [(node_name, description)]. Our own are excluded."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True,
                             text=True, timeout=5).stdout
        items = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    res = []
    for s in items:
        n = s.get("name", "")
        if n.endswith(".monitor") or n == MIX_SOURCE:
            continue
        res.append((n, s.get("description", n)))
    return res


def default_source() -> str:
    try:
        return subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class Engine:
    def __init__(self):
        self.dir = runtime_dir() / "vox-deck"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mix = None
        self.voice = None
        self.voice_key = None       # (graph structure, mic): a change here needs a new voice child
        self.voice_ctl = {}         # control values the running chain has
        self.pending = {}           # control values still to send
        self.voice_node = None      # PipeWire id of vox_voice_in (holds the filter controls)
        self.inflight = None        # (pw-cli process, values) of the update being sent
        self.live_updates = 0       # count of live control updates (for tests / status)
        self.last_error = ""
        self.kill_stale()

    # -- process helpers
    def _spawn(self, conf_name: str, text: str) -> subprocess.Popen:
        conf = self.dir / conf_name
        conf.write_text(text)
        log = open(self.dir / (conf_name + ".log"), "w")
        return subprocess.Popen(["pipewire", "-c", str(conf)], stdout=log, stderr=log,
                                preexec_fn=_pdeathsig, start_new_session=True)

    @staticmethod
    def _stop(p):
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def kill_stale(self):
        """Stop leftovers of an earlier crashed run (matched by our config path)."""
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit() or int(pid_dir.name) == os.getpid():
                continue
            try:
                cmd = (pid_dir / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if len(cmd) > 2 and cmd[0].endswith(b"pipewire") and cmd[1] == b"-c" \
                    and cmd[2].startswith(str(self.dir).encode()):
                try:
                    os.kill(int(pid_dir.name), signal.SIGTERM)
                except OSError:
                    pass

    # -- public API
    def start_mix(self):
        if not self.running(self.mix):
            self.mix = self._spawn("mix.conf", mix_conf())

    @staticmethod
    def running(p) -> bool:
        return p is not None and p.poll() is None

    def set_voice(self, preset: dict | None, mic: str):
        """Run the voice chain for `preset`; preset None = microphone muted (chain stopped).

        If the running chain has the same structure (same plugins and control names) and the
        same mic, only control values differ: they are sent live with set-param, without a new
        child and without an audio gap. Anything else starts a new voice child.
        """
        if preset is None or not mic:
            self._stop_voice()
            return
        key = (effects.structure(preset), mic)
        ctl = effects.control_map(preset)
        if self.running(self.voice) and key == self.voice_key:
            want = {k: v for k, v in ctl.items() if self.voice_ctl.get(k) != v}
            self.pending.update(want)
            for k in list(self.pending):           # dropped back to what is already running
                if self.voice_ctl.get(k) == ctl.get(k):
                    del self.pending[k]
            self.flush()
            return
        self._stop_voice()
        try:
            self.voice = self._spawn("voice.conf", voice_conf(preset, mic))
            self.voice_key, self.voice_ctl = key, ctl
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            self.voice, self.voice_key = None, None
            self.last_error = str(e)

    def _stop_voice(self):
        self._stop(self.voice)
        self.voice, self.voice_key, self.voice_ctl, self.pending, self.voice_node = None, None, {}, {}, None
        if self.inflight:
            self.inflight[0].wait()
            self.inflight = None

    def find_voice_node(self):
        """Id of our vox_voice_in node (owned by our voice child), or None if not registered yet."""
        if not self.running(self.voice):
            return None
        try:
            objs = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        clients = {o["id"]: o["info"]["props"].get("application.process.id")
                   for o in objs if o.get("type", "").endswith("Client") and o.get("info")}
        for o in objs:
            if o.get("type", "").endswith("Node") and o.get("info"):
                p = o["info"].get("props", {})
                if p.get("node.name") == "vox_voice_in" and \
                        str(clients.get(p.get("client.id"))) == str(self.voice.pid):
                    return o["id"]
        return None

    def flush(self) -> bool:
        """Send pending control values to the running chain. False = not done yet (call again).

        One short-lived `pw-cli set-param` per batch, at most one in flight; values that arrive
        meanwhile are merged and sent next, so slider drags coalesce instead of queueing up.
        """
        if self.inflight:
            proc, sent = self.inflight
            if proc.poll() is None:
                return False
            self.inflight = None
            if proc.returncode != 0:          # node vanished or id stale: look it up again
                self.voice_node = None
                for k, v in sent.items():
                    self.pending.setdefault(k, v)
                    self.voice_ctl.pop(k, None)
        if not self.pending:
            return True
        if not self.running(self.voice):
            return False
        if self.voice_node is None:
            self.voice_node = self.find_voice_node()
            if self.voice_node is None:
                return False
        sent, self.pending = self.pending, {}
        params = " ".join(f"{json.dumps(k)} {float(v)!r}" for k, v in sent.items())
        try:
            proc = subprocess.Popen(["pw-cli", "set-param", str(self.voice_node), "Props",
                                     f"{{ params = [ {params} ] }}"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, preexec_fn=_pdeathsig)
        except OSError as e:
            self.last_error = f"pw-cli: {e}"
            self.pending = {**sent, **self.pending}
            return False
        self.inflight = (proc, sent)
        self.voice_ctl.update(sent)
        self.live_updates += 1
        return False

    def busy(self) -> bool:
        return bool(self.pending or self.inflight)

    def healthy(self) -> dict:
        return {"mix": self.running(self.mix), "voice": self.running(self.voice)}

    def stop(self):
        self._stop_voice()
        self._stop(self.mix)
        self.mix = None
        for f in self.dir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
