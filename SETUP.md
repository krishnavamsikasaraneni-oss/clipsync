# ClipSync - setup guide

Copy text on PC 1 and it's ready to paste on PC 2. PC 2 also has a mic and a typing box that can type into any window at the speed you choose. Both PCs need to be on the same Wi-Fi. It's free and needs no account or API key.

## 1. Install

- **PC 1 (the one you copy from):** Unzip `ClipSyncSender-PC1.zip` somewhere permanent, like `C:\ClipSync`, and run `ClipSyncSender.exe`. It lives in the system tray (bottom-right, near the clock - check the ^ arrow).
- **PC 2 (the one you paste on):** Unzip `ClipSyncReceiver-PC2.zip` and run `ClipSyncReceiver.exe`. A window opens.

The first time, **Windows Firewall** asks about each app. Tick **Private networks** and click **Allow**. If you miss it, see Troubleshooting below.

If Windows SmartScreen says "Windows protected your PC", click **More info > Run anyway**. This happens because the app isn't signed with a paid certificate.

## 2. Pair (one time only)

1. On **PC 2**, click **Settings > Pair a new PC**. A 6-digit code appears.
2. On **PC 1**, the pairing window opens by itself (or right-click the tray icon > **Pair with PC 2**). Pick PC 2 from the list, type the code and click **Pair**.

That's it. From now on they find each other by themselves, even after restarts or Wi-Fi changes.

## 3. Use it

PC 2 has two modes, chosen at the top of the window under "When a clip arrives":

- **Paste mode:** Copy on PC 1, then press Ctrl+V on PC 2. Received clips also show in the list, where you click one to copy it again or double-click to edit it.
- **Type mode:** Clips are never put on the clipboard. They're typed out letter by letter into your web app at the speed set by the slider.
  1. Open your web app in the browser on PC 2.
  2. In ClipSync, click **Pick web app window**, then click into the text box in your web app within 3 seconds. The window title then shows next to "Typing into".
  3. Copy on PC 1. The text is typed into that web app. If several clips arrive, they're typed one after another.
  - It types wherever the cursor is in that window, so leave the cursor in the right text box.
  - If you click into a different window mid-way, typing stops straight away, so nothing ends up in the wrong place. Click back into the web app and it carries on from where it stopped.
  - Clicking a clip in the list types it again.
  - If you close and reopen the browser window, pick it again.
- **Mic:** Click **Speak**, talk, then click **Stop**. Your words appear in the text box. It runs on the PC itself, so it works with no internet.
- **Typing box:** Type or edit text, then:
  - **Copy** puts it on the clipboard.
  - **Type into window** types it into the last window you used (shown under the buttons). If there isn't one, you get 3 seconds to click into the window you want.
- **Typing speed:** Use the slider to choose from 10 to 300 words per minute, or Instant. Below 150 it varies the timing a little, like a real person.
- **Ctrl+Alt+Z** pauses or resumes syncing, from either PC. While text is being typed (or waiting to be typed), it stops the typing and clears anything waiting.
- **Settings (PC 2):** Change the shortcut, turn Start with Windows on or off, pick a speech model, or remove a paired PC.

Both apps start with Windows by default. Closing the PC 2 window just hides it in the tray. To quit, right-click the tray icon > **Quit**.

Text copied from password managers (like Bitwarden, 1Password or KeePass) is skipped and never sent.

## Troubleshooting

- **PC 2 isn't in the list on PC 1:** Type PC 2's IP address instead. It's shown on PC 2 under the pairing code. Both PCs must be on the same Wi-Fi (or PC 1 on PC 2's hotspot, or the other way round). Some public or guest Wi-Fi blocks devices from seeing each other, and a phone hotspot fixes that.
- **Can't connect or it keeps saying "Looking for...":** Allow the apps through the firewall. Open Start > "Allow an app through Windows Firewall" > Change settings, then tick **Private** for ClipSyncSender and ClipSyncReceiver. Also make sure Windows calls your Wi-Fi a **Private** network (Settings > Network > your Wi-Fi > Network profile type).
- **"Ctrl+Alt+Z is already used by another program":** Another app grabbed it first. Choose a different shortcut when asked, or in Settings on PC 2 or the tray menu on PC 1.
- **Type mode says "Click into ... to start typing":** Windows sometimes stops apps from switching windows on their own. Click into the web app once and typing starts by itself.
- **"doesn't recognise this PC - pair again":** PC 2 removed this PC. Pair again.
- **Logs:** Each app writes a log in `%APPDATA%\ClipSync` (paste that into the File Explorer address bar).
