"""Minimal ctypes wrapper for the Fraunhofer FDK AAC encoder (vendor/fdk-aac/libfdk-aac-2.dll).

Used because FFmpeg's built-in AAC encoder (what PyAV ships) is audibly weaker than fdk at
the same bitrate, and it cannot do AAC-ELD at all. Encodes interleaved int16 PCM to raw
access units (TT_MP4_RAW); RTP/LATM framing is done by the caller.

Licence: FDK AAC licence (vendor/fdk-aac/NOTICE); AAC is patent-encumbered. Personal use only.
"""

from __future__ import annotations

import ctypes as C
import pathlib

import numpy as np

DLL_PATH = pathlib.Path(__file__).resolve().parents[1] / "vendor" / "fdk-aac" / "libfdk-aac-2.dll"

# aacenc_lib.h parameter ids
AACENC_AOT = 0x0100
AACENC_BITRATE = 0x0101
AACENC_BITRATEMODE = 0x0102
AACENC_SAMPLERATE = 0x0103
AACENC_SBR_MODE = 0x0104
AACENC_GRANULE_LENGTH = 0x0105
AACENC_CHANNELMODE = 0x0106
AACENC_AFTERBURNER = 0x0200
AACENC_TRANSMUX = 0x0300
IN_AUDIO_DATA, OUT_BITSTREAM_DATA = 0, 3
AOT_AAC_LC, AOT_ER_AAC_ELD = 2, 39


class _BufDesc(C.Structure):
    _fields_ = [("numBufs", C.c_int), ("bufs", C.POINTER(C.c_void_p)), ("bufferIdentifiers", C.POINTER(C.c_int)),
                ("bufSizes", C.POINTER(C.c_int)), ("bufElSizes", C.POINTER(C.c_int))]


class _InArgs(C.Structure):
    _fields_ = [("numInSamples", C.c_int), ("numAncBytes", C.c_int)]


class _OutArgs(C.Structure):
    _fields_ = [("numOutBytes", C.c_int), ("numInSamples", C.c_int), ("numAncBytes", C.c_int),
                ("bitResState", C.c_int)]


class _Info(C.Structure):
    _fields_ = [("maxOutBufBytes", C.c_uint), ("maxAncBytes", C.c_uint), ("inBufFillLevel", C.c_uint),
                ("inputChannels", C.c_uint), ("frameLength", C.c_uint), ("nDelay", C.c_uint),
                ("nDelayCore", C.c_uint), ("confBuf", C.c_ubyte * 64), ("confSize", C.c_uint)]


_dll = None


def _lib():
    global _dll
    if _dll is None:
        if not DLL_PATH.exists():
            raise FileNotFoundError(f"{DLL_PATH} is missing. See vendor/fdk-aac/SOURCE.txt, or run with --codec=sbc.")
        _dll = C.CDLL(str(DLL_PATH))
    return _dll


class FdkAacEncoder:
    """aot=2 (AAC-LC) or 39 (AAC-ELD). bitrate_mode 0 = CBR (uses `bitrate`), 1..5 = VBR quality."""

    def __init__(self, aot: int = AOT_AAC_LC, rate: int = 48000, channels: int = 2,
                 bitrate: int = 256_000, bitrate_mode: int = 0, granule: int | None = None,
                 afterburner: bool = True) -> None:
        self.lib = _lib()
        self.channels = channels
        self.handle = C.c_void_p()
        self._check(self.lib.aacEncOpen(C.byref(self.handle), 0, channels), "aacEncOpen")
        params = [(AACENC_AOT, aot), (AACENC_SAMPLERATE, rate), (AACENC_CHANNELMODE, channels),
                  (AACENC_BITRATEMODE, bitrate_mode), (AACENC_TRANSMUX, 0),  # TT_MP4_RAW
                  (AACENC_AFTERBURNER, int(afterburner))]
        if bitrate_mode == 0:
            params.append((AACENC_BITRATE, bitrate))
        if aot == AOT_ER_AAC_ELD:
            params.append((AACENC_SBR_MODE, 0))
        if granule:
            params.append((AACENC_GRANULE_LENGTH, granule))
        for pid, value in params:
            self._check(self.lib.aacEncoder_SetParam(self.handle, pid, value), f"SetParam 0x{pid:04X}={value}")
        self._check(self.lib.aacEncEncode(self.handle, None, None, None, None), "aacEncEncode(init)")
        info = _Info()
        self._check(self.lib.aacEncInfo(self.handle, C.byref(info)), "aacEncInfo")
        self.frame_length = info.frameLength
        self.delay = info.nDelay
        self.asc = bytes(info.confBuf[:info.confSize])
        self._out = (C.c_ubyte * max(8192, info.maxOutBufBytes))()

    @staticmethod
    def _check(code: int, what: str) -> None:
        if code != 0:
            raise RuntimeError(f"fdk-aac {what} failed: 0x{code:04X}")

    def encode(self, pcm: np.ndarray) -> list[bytes]:
        """Feed interleaved int16 PCM (frames x channels); returns completed access units."""
        pcm = np.ascontiguousarray(pcm, dtype=np.int16)
        aus = []
        offset = 0
        total = pcm.size
        flat = pcm.reshape(-1)
        while offset < total:
            chunk = flat[offset:]
            in_ptr = C.c_void_p(chunk.ctypes.data)
            out_ptr = C.c_void_p(C.addressof(self._out))
            in_desc = _BufDesc(1, C.pointer(in_ptr), C.pointer(C.c_int(IN_AUDIO_DATA)),
                               C.pointer(C.c_int(chunk.nbytes)), C.pointer(C.c_int(2)))
            out_desc = _BufDesc(1, C.pointer(out_ptr), C.pointer(C.c_int(OUT_BITSTREAM_DATA)),
                                C.pointer(C.c_int(len(self._out))), C.pointer(C.c_int(1)))
            in_args, out_args = _InArgs(chunk.size, 0), _OutArgs()
            self._check(self.lib.aacEncEncode(self.handle, C.byref(in_desc), C.byref(out_desc),
                                              C.byref(in_args), C.byref(out_args)), "aacEncEncode")
            if out_args.numOutBytes:
                aus.append(bytes(self._out[:out_args.numOutBytes]))
            if out_args.numInSamples == 0 and not out_args.numOutBytes:
                break  # encoder buffered everything it can take
            offset += out_args.numInSamples
        return aus

    def close(self) -> None:
        if self.handle:
            self.lib.aacEncClose(C.byref(self.handle))
            self.handle = C.c_void_p()
