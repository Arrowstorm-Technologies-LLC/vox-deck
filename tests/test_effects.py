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


PREFIXED = """@prefix lv2: <http://lv2plug.in/ns/lv2core#> .
@prefix plug: <http://lsp-plug.in/plugins/lv2/> .
plug:gott_compressor_mono
\ta lv2:Plugin .
plug:compressor_mono
\ta lv2:Plugin ;
\trdfs:seeAlso <compressor_mono.ttl> .
"""
FULL = "<http://lsp-plug.in/plugins/lv2/compressor_mono>\n\ta lv2:Plugin .\n"
URI = "http://lsp-plug.in/plugins/lv2/compressor_mono"


def test_manifest_prefixed_name():
    assert effects.manifest_declares(PREFIXED, URI)
    assert effects.manifest_declares(
        "PREFIX x: <http://lsp-plug.in/plugins/lv2/>\nx:compressor_mono a lv2:Plugin .", URI)


def test_manifest_full_uri():
    assert effects.manifest_declares(FULL, URI)


def test_manifest_no_false_positive():
    assert not effects.manifest_declares(PREFIXED.replace("plug:compressor_mono", "plug:other"), URI)
    assert not effects.manifest_declares(
        "@prefix plug: <http://other/> .\nplug:compressor_mono a x .", URI)
