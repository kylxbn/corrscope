import shlex
import subprocess
from abc import ABC, abstractmethod
from os.path import abspath
from typing import TYPE_CHECKING, Type, List, Union, Optional

import numpy as np

from corrscope.config import register_config
from corrscope.ffmpeg_path import MissingFFmpegError

if TYPE_CHECKING:
    from corrscope.corrscope import Config


ByteBuffer = Union[bytes, np.ndarray]
RGB_DEPTH = 3
PIXEL_FORMAT = "rgb24"

FRAMES_TO_BUFFER = 2


class IOutputConfig:
    cls: "Type[Output]"

    def __call__(self, corr_cfg: "Config"):
        return self.cls(corr_cfg, cfg=self)


class _Stop:
    pass


Stop = _Stop()


class Output(ABC):
    def __init__(self, corr_cfg: "Config", cfg: IOutputConfig):
        self.corr_cfg = corr_cfg
        self.cfg = cfg

        rcfg = corr_cfg.render

        frame_bytes = rcfg.height * rcfg.width * RGB_DEPTH
        self.bufsize = frame_bytes * FRAMES_TO_BUFFER

    def __enter__(self):
        return self

    @abstractmethod
    def write_frame(self, frame: ByteBuffer) -> Optional[_Stop]:
        """ Output a Numpy ndarray. """

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def terminate(self):
        pass


# Glue logic


def register_output(config_t: Type[IOutputConfig]):
    def inner(output_t: Type[Output]):
        config_t.cls = output_t
        return output_t

    return inner


# FFmpeg command line generation


class _FFmpegProcess:
    def __init__(self, templates: List[str], corr_cfg: "Config"):
        self.templates = templates
        self.corr_cfg = corr_cfg

        self.templates += ffmpeg_input_video(corr_cfg)  # video
        if corr_cfg.master_audio:
            # Load master audio and trim to timestamps.

            self.templates.append(f"-ss {corr_cfg.begin_time}")

            audio_path = shlex.quote(abspath(corr_cfg.master_audio))
            self.templates += ffmpeg_input_audio(audio_path)  # audio

            if corr_cfg.end_time is not None:
                dur = corr_cfg.end_time - corr_cfg.begin_time
                self.templates.append(f"-to {dur}")

    def add_output(self, cfg: "Union[FFmpegOutputConfig, FFplayOutputConfig]") -> None:
        self.templates.append(cfg.video_template)  # video
        if self.corr_cfg.master_audio:
            self.templates.append(cfg.audio_template)  # audio

    def popen(self, extra_args, bufsize, **kwargs) -> subprocess.Popen:
        """Raises FileNotFoundError if FFmpeg missing"""
        try:
            args = self._generate_args() + extra_args
            return subprocess.Popen(
                args, stdin=subprocess.PIPE, bufsize=bufsize, **kwargs
            )
        except FileNotFoundError as e:
            raise MissingFFmpegError()

    def _generate_args(self) -> List[str]:
        return [arg for template in self.templates for arg in shlex.split(template)]


def ffmpeg_input_video(cfg: "Config") -> List[str]:
    fps = cfg.render_fps
    width = cfg.render.width
    height = cfg.render.height

    return [
        f"-f rawvideo -pixel_format {PIXEL_FORMAT} -video_size {width}x{height}",
        f"-framerate {fps}",
        "-i -",
    ]


def ffmpeg_input_audio(audio_path: str) -> List[str]:
    return ["-i", audio_path]


class PipeOutput(Output):
    def open(self, *pipeline: subprocess.Popen):
        """ Called by __init__ with a Popen pipeline to ffmpeg/ffplay. """
        if len(pipeline) == 0:
            raise TypeError("must provide at least one Popen argument to popens")

        self._pipeline = pipeline
        self._stream = pipeline[0].stdin
        # Python documentation discourages accessing popen.stdin. It's wrong.
        # https://stackoverflow.com/a/9886747

    def __enter__(self):
        return self

    def write_frame(self, frame: ByteBuffer) -> Optional[_Stop]:
        try:
            self._stream.write(frame)
            return None
        except BrokenPipeError:
            return Stop

    def close(self, wait=True) -> int:
        try:
            self._stream.close()
        except (BrokenPipeError, OSError):  # BrokenPipeError is a OSError
            pass

        if not wait:
            return 0

        retval = 0
        for popen in self._pipeline:
            retval |= popen.wait()
        return retval  # final value

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.close()
        else:
            self.terminate()

    def terminate(self):
        # Calling self.close() is bad.
        # If exception occurred but ffplay continues running,
        # popen.wait() will prevent stack trace from showing up.
        self.close(wait=False)

        exc = None
        for popen in self._pipeline:
            popen.terminate()
            # https://stackoverflow.com/a/49038779/2683842
            try:
                popen.wait(1)  # timeout=seconds
            except subprocess.TimeoutExpired as e:
                # gee thanks Python, https://stackoverflow.com/questions/45292479/
                exc = e
                popen.kill()

        if exc:
            raise exc


# FFmpegOutput


@register_config
class FFmpegOutputConfig(IOutputConfig):
    # path=None writes to stdout.
    path: Optional[str]
    args: str = ""

    video_template: str = "-c:v libx264 -crf 18 -preset superfast -movflags faststart"
    audio_template: str = "-c:a aac -b:a 384k"


FFMPEG = "ffmpeg"


@register_output(FFmpegOutputConfig)
class FFmpegOutput(PipeOutput):
    def __init__(self, corr_cfg: "Config", cfg: FFmpegOutputConfig):
        super().__init__(corr_cfg, cfg)

        ffmpeg = _FFmpegProcess([FFMPEG, "-y"], corr_cfg)
        ffmpeg.add_output(cfg)
        ffmpeg.templates.append(cfg.args)

        if cfg.path is None:
            video_path = "-"  # Write to stdout
        else:
            video_path = abspath(cfg.path)

        self.open(ffmpeg.popen([video_path], self.bufsize))


# FFplayOutput


@register_config
class FFplayOutputConfig(IOutputConfig):
    video_template: str = "-c:v copy"
    audio_template: str = "-c:a copy"


FFPLAY = "ffplay"


@register_output(FFplayOutputConfig)
class FFplayOutput(PipeOutput):
    def __init__(self, corr_cfg: "Config", cfg: FFplayOutputConfig):
        super().__init__(corr_cfg, cfg)

        ffmpeg = _FFmpegProcess([FFMPEG, "-nostats"], corr_cfg)
        ffmpeg.add_output(cfg)
        ffmpeg.templates.append("-f nut")

        p1 = ffmpeg.popen(["-"], self.bufsize, stdout=subprocess.PIPE)

        ffplay = shlex.split("ffplay -autoexit -")
        p2 = subprocess.Popen(ffplay, stdin=p1.stdout)

        p1.stdout.close()
        # assert p2.stdin is None   # True unless Popen is being mocked (test_output).

        self.open(p1, p2)
