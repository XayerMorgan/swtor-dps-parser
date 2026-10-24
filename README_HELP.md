# SWTOR Holocron DPS — Setup & Help Guide

## 1. How to Turn On Combat Logging in SWTOR
1. Launch **Star Wars: The Old Republic** and log into your character.
2. Press `ESC` (or `Ctrl + P`) and select **Preferences**.
3. Ensure the **GAME** tab is selected at the bottom-left.
4. Click **COMBAT LOGGING** on the left-hand menu.
5. Check the box labeled **☑ Enable combat logging to file**.
6. Click **Apply** and **OK**.

![SWTOR Combat Logging Preferences](swtor_combat_logging_help.png)

---

## 2. Where SWTOR Stores Your Combat Log Files
When combat logging is enabled, SWTOR creates timestamped `.txt` files (`combat_YYYY-MM-DD_HH_MM_SS_######.txt`) in your Windows `Documents` folder:

- **Standard Windows Path:**
  `C:\Users\<YourUsername>\Documents\Star Wars - The Old Republic\CombatLogs`
- **OneDrive Redirected Path (if OneDrive backs up Documents):**
  `C:\Users\<YourUsername>\OneDrive\Documents\Star Wars - The Old Republic\CombatLogs`

`SWTOR_DPS_Parser.exe` automatically checks both locations and tails the newest `combat_*.txt` file in real time. You can also click **❓ Help → 📂 Change Folder** or **🗂️ Open Folder in Explorer** inside the app at any time.

---

## 3. Running the Overlay While Playing
1. Set SWTOR's display mode (**Preferences → Graphics**) to **Fullscreen (Windowed)** so the overlay stays visible over the game.
2. Double-click **`SWTOR_DPS_Parser.exe`**.
3. Use **📌 Pin** to keep the overlay always on top, or **▣ Mini** to collapse it into a compact HUD bar during combat.
