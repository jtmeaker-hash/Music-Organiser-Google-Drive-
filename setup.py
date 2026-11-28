"""
Setup configuration for Music Vault Manager.
"""
from setuptools import setup, find_packages

setup(
    name="musicvault",
    version="2.0.4",
    description="Standalone Audio Library & Metadata Organizer for Termux on Android",
    author="Jacob Tyler Meaker",
    author_email="jtmeaker@gmail.com",
    url="https://github.com/jtmeaker-hash/Music-Organiser-Google-Drive-",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "mutagen>=1.47.0",
        "requests>=2.28.0",
        "google-api-python-client>=2.100.0",
        "google-auth>=2.20.0",
        "google-auth-oauthlib>=1.0.0",
        "google-auth-httplib2>=0.2.0",
    ],
    entry_points={
        "console_scripts": [
            "musicvault=musicvault.__main__:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Operating System :: POSIX :: Linux",
        "Operating System :: Android",
        "Topic :: Multimedia :: Sound/Audio :: Analysis",
    ],
)
