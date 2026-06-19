# GlobalPTT

A lightweight push-to-talk application for Windows 11.

Mutes your microphone system-wide until a key or mouse button is held. Works in any application — Discord, Teams, games — without virtual audio cables.

## Requirements

- Windows 11
- Python 3.10+

## Installation

```
pip install sounddevice pynput pycaw comtypes
```

## Usage

```
python GlobalPTT.py
```

1. Select your input device from the dropdown
2. Click **+ Add Key** and press any key or mouse button
3. Hold the key to transmit — release to mute

Settings are saved automatically to `%APPDATA%\GlobalPTT\prefs.json`.

## Compiling to an executable

Install PyInstaller:

```
pip install pyinstaller
```

Build:

```
pyinstaller --onefile --noconsole --name GlobalPTT GlobalPTT.py
```

The compiled executable will be in the `dist/` folder.

## Notes

If your device uses Elgato Wave Link, select the **Stream Mix** device rather than the raw mic input.

## License

MIT
