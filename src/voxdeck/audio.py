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
          target.object = "{mic}"
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
        self.voice_key = None
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
        """Run the voice chain for `preset`; preset None = microphone muted (chain stopped)."""
        if preset is None or not mic:
            self._stop(self.voice)
            self.voice, self.voice_key = None, None
            return
        key = (preset["id"], json.dumps(preset, sort_keys=True, default=str), mic)
        if self.running(self.voice) and key == self.voice_key:
            return
        self._stop(self.voice)
        try:
            self.voice = self._spawn("voice.conf", voice_conf(preset, mic))
            self.voice_key = key
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            self.voice, self.voice_key = None, None
            self.last_error = str(e)

    def healthy(self) -> dict:
        return {"mix": self.running(self.mix), "voice": self.running(self.voice)}

    def stop(self):
        self._stop(self.voice)
        self._stop(self.mix)
        self.voice = self.mix = None
        for f in self.dir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
