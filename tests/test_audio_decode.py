"""Exercise the real decoder without downloading a speech model."""
import io
import unittest
import wave

from faster_whisper.audio import decode_audio


class AudioDecodeTests(unittest.TestCase):
    def test_real_decoder_reads_valid_mono_pcm(self):
        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 16000)
        audio.seek(0)
        samples = decode_audio(audio)
        self.assertEqual(samples.shape, (16000,))
        self.assertEqual(str(samples.dtype), "float32")
        self.assertTrue((samples == 0).all())


if __name__ == "__main__":
    unittest.main()
