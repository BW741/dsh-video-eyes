"""`av` 的最小替身（stub）。

为什么需要：本机 Windows Smart App Control 处于强制状态
（`HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy:VerifiedAndReputablePolicyState = 1`），
它拦掉了 PyAV 轮子里的 FFmpeg DLL：

    ImportError: DLL load failed while importing frame:
    Application Control policy has blocked this file

而 faster-whisper 的 `faster_whisper/audio.py:15` 在模块级 `import av`，于是整个包都导入失败。
本工具链的解码统一交给系统 ffmpeg（见 `scripts/transcribe.py`），从不调用 `decode_audio`，
所以这里只需要让 `import av` 能过、且属性齐全即可。

用法：把本目录的**父目录**（`video-kit\\shims`）放到 `PYTHONPATH` 最前面，
它会排在 site-packages 之前，从而覆盖真正的 av 包。
"""
import sys
import types


class AudioResampler:  # noqa: D101
    def __init__(self, *args, **kwargs):
        raise RuntimeError("av 已被 video-kit 替换为 stub：音频解码请用 ffmpeg + transcribe.py")


class AudioFrame:  # noqa: D101
    pass


class FFmpegError(Exception):
    """对应 av.error.FFmpegError。"""


_audio = types.ModuleType("av.audio")
_audio.__path__ = []  # 让 `av.audio.x` 形式的导入也能成立
_resampler = types.ModuleType("av.audio.resampler")
_resampler.AudioResampler = AudioResampler
_frame = types.ModuleType("av.audio.frame")
_frame.AudioFrame = AudioFrame
_error = types.ModuleType("av.error")
_error.FFmpegError = FFmpegError
_packet = types.ModuleType("av.packet")
_packet.Packet = object

_audio.resampler = _resampler
_audio.frame = _frame
audio = _audio
error = _error
packet = _packet

for _name, _mod in (
    ("av.audio", _audio),
    ("av.audio.resampler", _resampler),
    ("av.audio.frame", _frame),
    ("av.error", _error),
    ("av.packet", _packet),
):
    sys.modules.setdefault(_name, _mod)
