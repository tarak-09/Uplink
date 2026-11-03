import io
import unittest
import wave

from uplink.voice import AudioError, to_wav

from .audio import tone_wav, voice_note


class ConversionTest(unittest.TestCase):
    def setUp(self):
        self.note = voice_note()
        if self.note is None:
            self.skipTest("ffmpeg with Opus support is not installed")

    def test_a_voice_note_becomes_the_wav_the_transcriber_wants(self):
        self.assertEqual(self.note[:4], b"OggS")
        wav = to_wav(self.note)
        with wave.open(io.BytesIO(wav)) as reader:
            self.assertEqual((reader.getnchannels(), reader.getsampwidth(), reader.getframerate()), (1, 2, 16_000))
            self.assertAlmostEqual(reader.getnframes() / 16_000, 1.5, delta=0.1)

    def test_24_khz_is_supported_too(self):
        with wave.open(io.BytesIO(to_wav(tone_wav(), sample_rate=24_000))) as reader:
            self.assertEqual((reader.getnchannels(), reader.getframerate()), (1, 24_000))

    def test_metadata_is_stripped(self):
        wav = to_wav(self.note)
        self.assertNotIn(b"LIST", wav[:200])
        self.assertEqual(wav[36:40], b"data", "a plain 44-byte header")

    def test_unreadable_audio_is_reported(self):
        with self.assertRaises(AudioError) as caught:
            to_wav(b"this is not audio at all")
        self.assertIn("could not read", str(caught.exception))


class LimitsTest(unittest.TestCase):
    def test_empty_and_huge_inputs_are_refused(self):
        with self.assertRaises(AudioError):
            to_wav(b"")
        with self.assertRaises(AudioError):
            to_wav(b"x" * (16 * 1024 * 1024 + 1))

    def test_a_missing_ffmpeg_is_explained(self):
        with self.assertRaises(AudioError) as caught:
            to_wav(b"OggS", ffmpeg="/nonexistent/ffmpeg")
        self.assertIn("ffmpeg was not found", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
