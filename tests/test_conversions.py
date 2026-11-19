from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
import wave

import av
import numpy as np
from PIL import Image, ImageDraw, features

from multitools.operations import IMAGES, MEDIA, convert_file


class ConversionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="formats-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)

    def test_new_image_formats(self):
        source = self.output / "Image source.png"
        image = Image.new("RGBA", (95, 75), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle((18, 20, 70, 50), fill="navy")
        image.save(source)
        originals = source.read_bytes()
        expected = {".avif": "AVIF", ".tiff": "TIFF", ".bmp": "BMP", ".gif": "GIF", ".ico": "ICO"}
        for extension, format_name in expected.items():
            with self.subTest(format=format_name):
                if format_name == "AVIF" and not features.check("avif"):
                    self.skipTest("Pillow ne fournit pas d’encodeur AVIF ici.")
                operation = next(item for item in IMAGES if item.extension == extension)
                result = convert_file(source, self.output, operation, Event())
                with Image.open(result) as converted:
                    self.assertEqual(converted.format, format_name)
                    self.assertEqual(converted.size, (256, 256) if format_name == "ICO" else image.size)
                    if format_name in {"AVIF", "TIFF", "ICO"}:
                        self.assertEqual(converted.convert("RGBA").getpixel((0, 0))[3], 0)
                    elif format_name == "BMP":
                        self.assertEqual(converted.getpixel((0, 0)), (255, 255, 255))
                    if format_name == "ICO":
                        self.assertIn((16, 16), converted.ico.sizes())
        self.assertEqual(source.read_bytes(), originals)

    def test_audio_profiles_and_flac_sample_preservation(self):
        source = self.output / "Mono 44 kHz.wav"
        samples = (np.arange(4410, dtype=np.int16) * 5 - 11000).astype("<i2")
        with wave.open(str(source), "wb") as output:
            output.setparams((1, 2, 44100, 0, "NONE", ""))
            output.writeframes(samples.tobytes())
        for extension, codec in {".flac": "flac", ".m4a": "aac", ".ogg": "vorbis", ".opus": "opus"}.items():
            with self.subTest(format=extension):
                operation = next(item for item in MEDIA if item.extension == extension)
                result = convert_file(source, self.output, operation, Event())
                with av.open(str(result)) as converted:
                    track = converted.streams.audio[0]
                    self.assertEqual(track.codec_context.name, codec)
                    audio = list(converted.decode(track))
                    self.assertTrue(audio)
                    if codec == "flac":
                        self.assertEqual((track.sample_rate, track.layout.name), (44100, "mono"))
                        decoded = np.concatenate([frame.to_ndarray().ravel() for frame in audio])
                        np.testing.assert_array_equal(decoded, samples)

    def test_webm_encodes_audiovisual_input_and_mkv_copies_tracks(self):
        source = self.output / "Vidéo avec piste audio.mp4"
        with av.open(str(source), "w") as container:
            video = container.add_stream("libx264", rate=24, width=64, height=48, pix_fmt="yuv420p")
            audio = container.add_stream("aac", rate=48000, layout="stereo")
            for index in range(8):
                frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), (index * 30, 100, 120)))
                frame.pts, frame.time_base = index, Fraction(1, 24)
                container.mux(video.encode(frame))
                sound = av.AudioFrame.from_ndarray(np.zeros((2, 2000), dtype=np.float32), format="fltp", layout="stereo")
                sound.pts, sound.sample_rate, sound.time_base = index * 2000, 48000, Fraction(1, 48000)
                container.mux(audio.encode(sound))
            container.mux(video.encode(None))
            container.mux(audio.encode(None))

        for extension, codecs in {".webm": ("vp9", "opus"), ".mkv": ("h264", "aac")}.items():
            with self.subTest(format=extension):
                operation = next(item for item in MEDIA if item.extension == extension)
                result = convert_file(source, self.output, operation, Event())
                with av.open(str(result)) as converted:
                    self.assertEqual(tuple(track.codec_context.name for track in converted.streams), codecs)
                    frames = list(converted.decode(*converted.streams))
                    self.assertEqual(sum(isinstance(frame, av.VideoFrame) for frame in frames), 8)
                    self.assertTrue(any(isinstance(frame, av.AudioFrame) for frame in frames))


if __name__ == "__main__":
    unittest.main()
