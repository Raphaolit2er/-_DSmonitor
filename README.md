An error was made during creation: Should be named "5_DSmonitor"

DS Monitor: A tool that turns your Nintendo DS into a wireless monitor.

What You Need:
- A Nintendo DS flashcart or a DS emulator that supports Wi-Fi simulation (such as **MelonDS** or **No$GBA**).
- A computer to run the Python code
- Python 3.8 or higher
- Run `pip install Pillow mss pyautogui`

How to Run:
1. Open the Python server script file.
2. Set which type of monitor you want (Streamer mode to be able to stream all your screens, 1 screen for either top or bottom, 1 giant screen for both as 1 screen, or 2 screens for both screens to be a seperate monitor)
3. Click the Start Server button. The server will listen via TCP on your chosen port.
3. Boot the compiled client "ds-monitor-client.nds" file on your DS hardware or emulator.
4. Press Y to open the server config, use the D-pad to enter your host PC's local IP address, and type the port number using the on-screen keypad. Press A to confirm.
5. Press A to switch the DS into Monitor Mode.
6. Enjoy your Nintendo DS as a monitor!

Disclaimers:
- Multiple consoles at a time aren't possible, as well as multiple times the script.
- The latency relies entirely on local Wi-Fi stability and TCP packet transmission. From my testing, it was fast enough to play games, even tho some latency was visible.
- Memory leaks are a possibility although might be rare.
- Only Wayland and X11 are supported
- This project was made using AIs, as I am not a good enough coder to do that on my own. Sorry to people who thought it was made by hand.
- Wayland users can't have the DS into a monitor due to Google Gemini telling me it's not possible. Comments about this are allowed, and I will fix the code accordingly.
