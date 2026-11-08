# SPDX-License-Identifier: MIT
"""FrameFace: a VIVE Facial Tracker (0bb4:0321) on the Steam Frame, served as an MJPEG camera.

The package runs on the headset as the user service `frameface.service` (Python 3.12 stdlib and
ctypes usbfs); the PC side is Baballonia plus SteamFrameEyeModule 0.6.0. The core modules uvc,
descriptors, assembler, quality, policy, config and state import none of ctypes, select, socket
or threading (config and state only read and write their own files).
"""

__version__ = "0.6.0"
