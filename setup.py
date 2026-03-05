from setuptools import setup

APP = ["app.py"]
DATA_FILES = ["Icon.png"]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "AppIcon.icns",
    "packages": ["certifi"],
    "plist": {
        "CFBundleName": "ShareChecker",
        "CFBundleDisplayName": "ShareChecker",
        "CFBundleIdentifier": "com.sharechecker.app",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
