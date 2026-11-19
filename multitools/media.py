from __future__ import annotations

from pathlib import Path
from threading import Event

import av

from .operations import MEDIA, MKV_COPY, MOV_COPY, Operation, check_cancelled


VIDEO_PROFILES = {
    ".mp4": ("libx264", "yuv420p", {"preset": "fast", "crf": "23"}),
    ".mov": ("prores_ks", "yuv422p10le", {"profile": "3"}),
    ".webm": ("libvpx-vp9", "yuv420p", {"crf": "31", "b": "0", "deadline": "good", "cpu-used": "4"}),
}
AAC = ("aac", "fltp", 192000)
PCM = ("pcm_s16le", "s16", None)
OPUS = ("libopus", "flt", 128000)
AUDIO_PROFILES = {
    ".mp4": AAC, ".mov": PCM, ".webm": OPUS,
    ".mp3": ("libmp3lame", "fltp", 192000), ".wav": PCM,
    ".m4a": AAC, ".ogg": ("vorbis", "fltp", 160000), ".opus": OPUS,
    ".flac": ("flac", "s16", None),
}
COPY_PROFILES = {MOV_COPY: "mov", MKV_COPY: "matroska"}


def _copy_streams(container, output, streams, cancelled: Event) -> None:
    mapping = {stream.index: output.add_stream_from_template(stream) for stream in streams}
    output.metadata.update(container.metadata)
    for packet in container.demux(*streams):
        check_cancelled(cancelled)
        if packet.dts is not None:
            packet.stream = mapping[packet.stream.index]
            output.mux(packet)


def _add_audio(output, input_audio, extension: str):
    codec, sample_format, bit_rate = AUDIO_PROFILES[extension]
    rate, layout = 48000, "stereo"
    if codec == "flac":
        rate = input_audio.sample_rate or rate
        layout = {"1 channels": "mono", "2 channels": "stereo"}.get(input_audio.layout.name, input_audio.layout.name)
        input_format = input_audio.format
        if input_format is None or input_format.name not in {"u8", "u8p", "s16", "s16p"}:
            sample_format = "s32"

    audio = output.add_stream(codec, rate=rate)
    audio.layout = layout
    audio.format = sample_format
    if audio.codec_context.name == "vorbis":
        audio.options = {"strict": "experimental"}
    if bit_rate is not None:
        audio.bit_rate = bit_rate
    return audio, av.AudioResampler(format=sample_format, layout=layout, rate=rate)


def _encode(container, output, input_video, input_audio, extension: str, cancelled: Event) -> None:
    video = audio = resampler = None
    if input_video is not None:
        codec, pixel_format, options = VIDEO_PROFILES[extension]
        video = output.add_stream(codec, rate=input_video.average_rate or 25)
        video.width = input_video.width + input_video.width % 2
        video.height = input_video.height + input_video.height % 2
        video.pix_fmt = pixel_format
        video.options = options.copy()
        if input_video.time_base is not None:
            video.codec_context.time_base = input_video.time_base
        if input_video.sample_aspect_ratio is not None:
            video.codec_context.sample_aspect_ratio = input_video.sample_aspect_ratio

    if input_audio is not None:
        audio, resampler = _add_audio(output, input_audio, extension)

    input_streams = [stream for stream in (input_video, input_audio) if stream is not None]
    for packet in container.demux(*input_streams):
        check_cancelled(cancelled)
        for frame in packet.decode():
            check_cancelled(cancelled)
            if isinstance(frame, av.VideoFrame):
                frame = frame.reformat(width=video.width, height=video.height, format=video.pix_fmt)
                output.mux(video.encode(frame))
            else:
                for sample in resampler.resample(frame):
                    output.mux(audio.encode(sample))

    if video is not None:
        output.mux(video.encode(None))
    if audio is not None:
        for sample in resampler.resample(None):
            output.mux(audio.encode(sample))
        output.mux(audio.encode(None))


def convert_media(source: Path, target: Path, operation: Operation, cancelled: Event) -> None:
    if operation not in MEDIA:
        raise ValueError("Format de sortie inconnu.")

    extension = operation.extension
    copy_format = COPY_PROFILES.get(operation)
    audio_only = copy_format is None and extension not in VIDEO_PROFILES
    with av.open(str(source), options={"protocol_whitelist": "file"}) as container:
        videos = [stream for stream in container.streams.video if not stream.disposition & av.stream.Disposition.attached_pic]
        audio = list(container.streams.audio)
        if audio_only and not audio:
            raise ValueError("Ce fichier ne contient aucune piste audio.")
        if not audio_only and not videos:
            raise ValueError("Ce fichier ne contient aucune piste vidéo.")

        options = {"movflags": "+faststart"} if extension in {".mp4", ".m4a"} else None
        with av.open(str(target), mode="w", format=copy_format, options=options) as output:
            if copy_format is not None:
                _copy_streams(container, output, videos + audio, cancelled)
            else:
                _encode(container, output, None if audio_only else videos[0], next(iter(audio), None), extension, cancelled)
