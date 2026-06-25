# GlobalPTT

A lightweight push-to-talk application for Windows 11. Mutes your microphone system-wide until a key or mouse button is held. Works across all applications without virtual audio cables. Supports multiple independent PTT channels, each with its own device and keybinds.

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

1. Click **+ Add Channel** to add a PTT channel (or start with the default)
2. Select an input device from the dropdown for each channel
3. Click **+ Add Key** and press any key or mouse button to register a keybind
4. Hold the key to transmit — release to mute
5. Add multiple bindings per channel if needed — any one will trigger PTT
6. Click **✕** in a channel header to remove it — at least one channel is always kept

Each channel operates independently with its own device, keybinds, and release delay. If you add more channels than fit the window, a horizontal scrollbar appears. You can also drag the window wider to see them all at once.

Settings are saved automatically to `%APPDATA%\GlobalPTT\prefs.json`.

## Notes

- The application works when minimized or in the background
- Original mic mute state is restored on exit
- If your device is not functioning after closing the application, manually unmute it via Control Panel → Sound. You can open it quickly by pressing **Win + R** and typing `mmsys.cpl`
- Removing a channel while transmitting will cleanly unmute that device before releasing it

## License

MIT