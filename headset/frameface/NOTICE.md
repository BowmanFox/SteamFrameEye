# FrameFace notices

FrameFace (`headset/frameface/`, released as `FrameFace-<version>.tar.gz`) is part of
SteamFrameEye. This file lists its licence and the third-party works it relies on.

## FrameFace

FrameFace is released under the MIT License, exactly as the repository's `LICENSE`:

```text
MIT License

Copyright (c) 2026 OTT Labs

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Third-party notices

The VIVE Facial Tracker protocol that FrameFace implements (the extension-unit commands, the
register table and the activation order) comes from the MIT-licensed works below. Their
notices are reproduced here as their licences require.

### LordOfDragons/vivefacialtracker (MIT)

Source of the protocol facts: the extension-unit commands and the register table.

```text
Copyright (c) 2024 Plüss Roland

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Aidansabre/Liptracker `vive-face-init.py`, revision `5bc0700` (MIT)

Source of the order tested on a Steam Frame that FrameFace uses as strategy S5:
SETCONFIGURATION before claiming the interfaces, 384-byte commands, and the `0x53 0x54`
trailer.

```text
Copyright (c) 2024 DragonDreams GmbH
Copyright (c) 2026 Contributors to this repository

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Attribution chain, as stated in Liptracker's `THIRD_PARTY_NOTICES.md`: the extension-unit
sequence of `vive-face-init.py` is adapted from Project Babble
`BabbleApp/vivefacialtracker/vivetracker.py`, which carries its own MIT header (the rest of the
ProjectBabble repository is under the Babble Software Distribution License and was not used):

```text
Copyright DragonDreams GmbH 2024

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Consulted for behaviour only

No code was copied or paraphrased from these projects; they were consulted only for how the
hardware behaves:

- BITFOXDEN Driver.SteamFrame.ViveFacialTracker, Babble Frame Runtime and its Vamprigamer005
  and grzesjam forks (GPL-3.0);
- Baballonia, vft-stream and Liptracker revisions before `5bc0700` (Babble Software
  Distribution License 1.0);
- CyrusOtter Babble-Bridge (no licence).

The image pipeline reproduces the documented semantics of OpenCV/OpenCvSharp functions (resize,
blur, lookup table); these are API facts, not copied code.

### Not included

Baballonia (Babble Software Distribution License 1.0) is installed by the user and is never
bundled with FrameFace. Its face model (`faceModel.onnx`) is never copied or redistributed.
