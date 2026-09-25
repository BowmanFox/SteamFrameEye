"""
gl_texture.py - a persistent OpenGL ES texture for streaming an RGBA raster into a SteamVR overlay.

IVROverlay::SetOverlayRaw makes the compositor recreate the overlay texture on every call, which
shows a blank frame each time (visible flicker when updating more than about once a second).
SetOverlayTexture with an OpenGL texture keeps one texture alive and only its contents change.

This uses EGL's surfaceless platform (Mesa) with a GLES context, so it works headless and without a
window system; on the Steam Frame that is Mesa zink on Turnip. Standard library only (ctypes).
The context is current on the thread that creates the object; upload() and the SetOverlayTexture
call must run on that same thread.
"""
import ctypes
from ctypes import POINTER, byref, c_char_p, c_int, c_uint, c_void_p

EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
EGL_PLATFORM_GBM_MESA = 0x31D7
EGL_EXTENSIONS = 0x3055
EGL_VENDOR = 0x3053
EGL_VERSION = 0x3054
EGL_OPENGL_ES_API = 0x30A0
EGL_RENDERABLE_TYPE = 0x3040
EGL_OPENGL_ES2_BIT = 0x0004
EGL_SURFACE_TYPE = 0x3033
EGL_PBUFFER_BIT = 0x0001
EGL_RED_SIZE, EGL_GREEN_SIZE, EGL_BLUE_SIZE, EGL_ALPHA_SIZE = 0x3024, 0x3023, 0x3022, 0x3021
EGL_NONE = 0x3038
EGL_CONTEXT_CLIENT_VERSION = 0x3098
EGL_SUCCESS = 0x3000

GL_TEXTURE_2D = 0x0DE1
GL_RGBA = 0x1908
GL_RGBA8 = 0x8058
GL_UNSIGNED_BYTE = 0x1401
GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER = 0x2801, 0x2800
GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T = 0x2802, 0x2803
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_UNPACK_ALIGNMENT = 0x0CF5
GL_VENDOR, GL_RENDERER, GL_VERSION = 0x1F00, 0x1F01, 0x1F02
GL_NO_ERROR = 0


class GlError(Exception):
    pass


class GlTexture:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.egl = ctypes.CDLL("libEGL.so.1")
        self.gl = ctypes.CDLL("libGLESv2.so.2")
        self._prototype()
        self.display = None
        self.context = None
        self.texture = 0
        self._init_egl()
        self._init_texture()

    # -- setup

    def _prototype(self):
        egl, gl = self.egl, self.gl
        egl.eglGetProcAddress.restype = c_void_p
        egl.eglGetProcAddress.argtypes = [c_char_p]
        egl.eglGetError.restype = c_int
        egl.eglQueryString.restype = c_char_p
        egl.eglQueryString.argtypes = [c_void_p, c_int]
        egl.eglGetDisplay.restype = c_void_p
        egl.eglGetDisplay.argtypes = [c_void_p]
        egl.eglInitialize.restype = c_uint
        egl.eglInitialize.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]
        egl.eglBindAPI.restype = c_uint
        egl.eglBindAPI.argtypes = [c_uint]
        egl.eglChooseConfig.restype = c_uint
        egl.eglChooseConfig.argtypes = [c_void_p, POINTER(c_int), POINTER(c_void_p), c_int, POINTER(c_int)]
        egl.eglCreateContext.restype = c_void_p
        egl.eglCreateContext.argtypes = [c_void_p, c_void_p, c_void_p, POINTER(c_int)]
        egl.eglMakeCurrent.restype = c_uint
        egl.eglMakeCurrent.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p]
        egl.eglDestroyContext.restype = c_uint
        egl.eglDestroyContext.argtypes = [c_void_p, c_void_p]
        egl.eglTerminate.restype = c_uint
        egl.eglTerminate.argtypes = [c_void_p]
        gl.glGetString.restype = c_char_p
        gl.glGetString.argtypes = [c_uint]
        gl.glGetError.restype = c_uint
        gl.glGenTextures.argtypes = [c_int, POINTER(c_uint)]
        gl.glDeleteTextures.argtypes = [c_int, POINTER(c_uint)]
        gl.glBindTexture.argtypes = [c_uint, c_uint]
        gl.glTexParameteri.argtypes = [c_uint, c_uint, c_int]
        gl.glPixelStorei.argtypes = [c_uint, c_int]
        gl.glTexImage2D.argtypes = [c_uint, c_int, c_int, c_int, c_int, c_int, c_uint, c_uint, c_void_p]
        gl.glTexSubImage2D.argtypes = [c_uint, c_int, c_int, c_int, c_int, c_int, c_uint, c_uint, c_void_p]
        gl.glFlush.argtypes = []
        gl.glFinish.argtypes = []

    def _init_egl(self):
        egl = self.egl
        display = None
        get_platform_display = egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
        if get_platform_display:
            fn = ctypes.CFUNCTYPE(c_void_p, c_uint, c_void_p, POINTER(c_int))(get_platform_display)
            display = fn(EGL_PLATFORM_SURFACELESS_MESA, None, None)
        if not display:
            display = egl.eglGetDisplay(None)
        if not display:
            raise GlError("no EGL display (surfaceless platform unavailable)")
        major, minor = c_int(), c_int()
        if not egl.eglInitialize(display, byref(major), byref(minor)):
            raise GlError("eglInitialize failed: 0x%x" % egl.eglGetError())
        self.display = display
        if not egl.eglBindAPI(EGL_OPENGL_ES_API):
            raise GlError("eglBindAPI(GLES) failed: 0x%x" % egl.eglGetError())
        config = c_void_p()
        count = c_int()
        attribs = (c_int * 13)(EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
                               EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE)
        egl.eglChooseConfig(display, attribs, byref(config), 1, byref(count))
        cfg = config if count.value > 0 else None   # no matching config: EGL_KHR_no_config_context
        context = None
        for version in (3, 2):
            ctx_attribs = (c_int * 3)(EGL_CONTEXT_CLIENT_VERSION, version, EGL_NONE)
            context = egl.eglCreateContext(display, cfg, None, ctx_attribs)
            if context:
                break
        if not context:
            raise GlError("eglCreateContext failed: 0x%x" % egl.eglGetError())
        if not egl.eglMakeCurrent(display, None, None, context):
            raise GlError("eglMakeCurrent (surfaceless) failed: 0x%x" % egl.eglGetError())
        self.context = context
        self.info = "%s / %s" % ((self.gl.glGetString(GL_RENDERER) or b"?").decode(errors="replace"),
                                 (self.gl.glGetString(GL_VERSION) or b"?").decode(errors="replace"))

    def _init_texture(self):
        gl = self.gl
        tex = c_uint()
        gl.glGenTextures(1, byref(tex))
        self.texture = tex.value
        gl.glBindTexture(GL_TEXTURE_2D, self.texture)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 4)
        gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, self.width, self.height, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
        err = gl.glGetError()
        if err != GL_NO_ERROR:
            raise GlError("glTexImage2D failed: 0x%x" % err)

    # -- per frame

    def upload(self, buf):
        """Copies a top-down RGBA8 raster (width*height*4 bytes) into the texture. OpenGL's origin is the
        bottom-left corner, so rows are reversed here to keep the image upright in the overlay."""
        stride = self.width * 4
        expected = stride * self.height
        if len(buf) != expected:
            raise ValueError("raster is %d bytes, expected %d" % (len(buf), expected))
        view = memoryview(buf)
        flipped = b"".join(view[(self.height - 1 - y) * stride:(self.height - y) * stride] for y in range(self.height))
        gl = self.gl
        gl.glBindTexture(GL_TEXTURE_2D, self.texture)
        gl.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self.width, self.height, GL_RGBA, GL_UNSIGNED_BYTE, flipped)
        gl.glFlush()
        err = gl.glGetError()
        if err != GL_NO_ERROR:
            raise GlError("glTexSubImage2D failed: 0x%x" % err)

    def close(self):
        gl, egl = self.gl, self.egl
        try:
            if self.texture:
                tex = c_uint(self.texture)
                gl.glDeleteTextures(1, byref(tex))
                self.texture = 0
            if self.display:
                egl.eglMakeCurrent(self.display, None, None, None)
                if self.context:
                    egl.eglDestroyContext(self.display, self.context)
                    self.context = None
                egl.eglTerminate(self.display)
                self.display = None
        except Exception:  # best effort on shutdown
            pass
