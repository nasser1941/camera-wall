"""python-for-android recipe for PyAV (FFmpeg bindings), overriding the bundled one.

Why an override: the recipe bundled with python-for-android v2026.05.09 pins PyAV 13.1.0,
which does not compile against the FFmpeg 8.0.1 that the same release builds (FFmpeg 8 removed
``avcodec_close`` and ``AV_CODEC_CAP_SUBFRAMES``), and its install step hands ``--ffmpeg-dir``
to pip, which rejects it. PyAV supports FFmpeg 8 from 15.1.

It also drops the bundled recipe's ``av_codecs`` dependency (x264, LAME/shine and libvpx
encoders, which turn FFmpeg into a GPL build): the camera wall only decodes, and FFmpeg's own
H.264/H.265 decoders, RTSP demuxer and network protocols are what it needs.

Keep ``version`` in step with ``requirements-android.txt`` so the desktop test run and the
APK use the same PyAV.
"""

from os.path import join

from pythonforandroid.recipe import PyProjectRecipe, Recipe


class PyAVRecipe(PyProjectRecipe):
    name = "av"
    version = "16.1.0"
    url = "https://github.com/PyAV-Org/PyAV/archive/v{version}.zip"
    depends = ["python3", "ffmpeg"]

    def get_recipe_env(self, arch, **kwargs):
        env = super().get_recipe_env(arch, **kwargs)

        # PyAV's setup.py finds FFmpeg with pkg-config. Point it at the cross-compiled FFmpeg
        # only, never at the build machine's own libraries.
        ffmpeg_dir = Recipe.get_recipe("ffmpeg", self.ctx).get_build_dir(arch.arch)
        env["PKG_CONFIG_LIBDIR"] = join(ffmpeg_dir, "lib", "pkgconfig")
        env.pop("PKG_CONFIG_PATH", None)

        # Link the extension modules the way python-for-android's Cython recipes do.
        env["LDFLAGS"] += " -L{} -L{}".format(self.ctx.get_libs_dir(arch.arch), self.ctx.libs_dir)
        env["LDSHARED"] = env["CC"] + " -shared"
        return env


recipe = PyAVRecipe()
