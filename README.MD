# GlobalPTT
A lightweight global push-to-talk application for Windows 11. Keeps your microphone muted system-wide until you hold a key or mouse button. Works across all applications. Supports multiple independent PTT channels, each with its own device, keybinds, and release delay.

## Requirements
- Windows 11
- Python 3.10+

## Dependencies
```
pip install pynput pycaw comtypes
```

## Running from source
```
python GlobalPTT.py
```

## Building an executable
```
pip install pyinstaller
pyinstaller --onefile --noconsole --icon=GlobalPTTIcon.ico --add-data "GlobalPTTIcon.ico;." --name GlobalPTT GlobalPTT.py
```
Output will be in the `dist/` folder.

## Usage
1. Click **+ Add Channel** to add a PTT channel
2. Select an input device from the dropdown
3. Click **+ Add Key** and press any key or mouse button to bind it
4. Hold the key to go live — release to mute
5. Optionally set a **Release Delay** to keep the mic live briefly after releasing

Settings save automatically to `%APPDATA%\GlobalPTT\prefs.json`.

## Notes
- Keybinds are global — works minimised or in the background
- Original mute state is restored on exit
- If a device stays muted after a crash: **Win + R** → `mmsys.cpl`

## License
MIT