import warnings

# Silence the UserWarning emitted by PyInstaller's importer when it imports
# the deprecated pkg_resources module. The warning is harmless for the app.
warnings.filterwarnings(
    "ignore",
    message="pkg_resources is deprecated as an API",
    category=UserWarning,
    module="PyInstaller.loader.pyimod02_importers",
)
