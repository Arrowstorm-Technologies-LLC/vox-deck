import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from voxdeck import audio, effects  # noqa: E402


def test_presets_build():
    for p in effects.load_presets().values():
        if p["available"]:
            g = effects.build_graph(p)
            assert "inputs" in g and "outputs" in g


def test_configs_render():
    p = effects.load_presets()["clean"]
    assert "Audio/Source" in audio.mix_conf()
    assert "vox_voice_in" in audio.voice_conf(p, "some_mic")
    assert "stream.capture.sink = true" in audio.voice_conf(p, "x.monitor")
