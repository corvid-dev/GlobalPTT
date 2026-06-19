# GlobalPTT

A lightweight push-to-talk application for Windows 11. Mutes your microphone system-wide until a key or mouse button is held. Works across all applications without virtual audio cables.

## Requirements

- Windows 11
- Python 3.10+

## Dependencies

```
pip install sounddevice pynput pycaw comtypes
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

1. Select your input device from the dropdown
2. Click **+ Add Key** and press any key or mouse button to register it
3. Hold the key to transmit — release to mute
4. Add multiple bindings if needed — any one will trigger PTT

Settings are saved automatically to `%APPDATA%\GlobalPTT\prefs.json`.

## Notes

- If using Elgato Wave Link, select the **Stream Mix** device rather than the raw mic input
- The application works when minimized or in the background
- Original mic mute state is restored on exit

## License

MIT
