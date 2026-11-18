"""App registry and launcher (Phase 3).

Three layers, tried in this order when the user says "open <something>":

1. The built-in registry below: hundreds of well-known apps with nicknames, spoken variants and common
   mis-hearings ("crome", "power shell", "vs code", "note pad"). Each app lists how to start it.
2. Apps installed on THIS PC, read from the Start Menu, Desktop shortcuts, the Windows app list (Store apps)
   and the registry "App Paths". So apps that are not in the registry still open.
3. Web versions (YouTube, WhatsApp Web, Canva...) when no installed app matches.

Names are matched exactly first, then loosely (starts-with, contains, sounds-alike). A close-but-unsure match
is never launched blindly: the reply lists the options ("Did you mean ...?") so the user can pick one.

Your own additions go in `apps.json` (see apps.example.json) or `tools.app_aliases` in config.json.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from forgecli.nova.tools.base import ToolError
from forgecli.nova.core.config import ROOT

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class App:
    name: str                 # what Nova calls it out loud
    aliases: tuple            # other names, spoken variants and common mis-hearings
    targets: tuple            # ways to start it, tried in order (exe name, URI, or a full path with %VARS%)
    web: str = ""             # web version, used only when the app itself isn't installed
    custom: bool = False


def _a(name: str, aliases: str = "", targets: str = "", web: str = "") -> App:
    """Compact way to write registry rows: aliases and targets are '|' separated."""
    al = tuple(x.strip() for x in aliases.split("|") if x.strip())
    tg = tuple(x.strip() for x in targets.split("|") if x.strip())
    return App(name, al, tg, web)


_P = r"%ProgramFiles%"
_P86 = r"%ProgramFiles(x86)%"
_LA = r"%LOCALAPPDATA%"
_RA = r"%APPDATA%"

# ---------------------------------------------------------------------------------------------- registry
BUILTIN_APPS: list[App] = [
    # ---- terminals and command lines. "terminal" prefers Windows Terminal, then PowerShell, then cmd.
    _a("Terminal", "terminal|the terminal|console|command line|term|termnal|terminol|consol",
       "wt.exe|powershell.exe|cmd.exe"),
    _a("PowerShell", "powershell|power shell|shell|windows powershell|ps|pshell|powershel|power shel|باورشل|شل",
       "powershell.exe|wt.exe powershell|cmd.exe"),
    _a("Command Prompt", "command prompt|cmd|cmd exe|dos|dos prompt|command|commandprompt|the black screen",
       "cmd.exe"),
    _a("Windows Terminal", "windows terminal|wt|windows terminal app|win terminal", "wt.exe"),
    _a("PowerShell 7", "powershell 7|pwsh|power shell 7|powershell seven", "pwsh.exe|" + _P + r"\PowerShell\7\pwsh.exe"),
    _a("Git Bash", "git bash|bash|gitbash|git shell", _P + r"\Git\git-bash.exe|" + _P86 + r"\Git\git-bash.exe"),
    _a("WSL", "wsl|ubuntu|linux|windows subsystem for linux|ubuntu terminal", "wsl.exe|ubuntu.exe"),
    _a("Anaconda Prompt", "anaconda prompt|anaconda|conda"),
    _a("Python", "python|python shell|python console|py|idle|python idle", "python.exe|idle.exe|py.exe"),
    _a("Node.js", "node|nodejs|node js|node command prompt", "node.exe"),

    # ---- Windows built-ins
    _a("Notepad", "notepad|note pad|notes pad|text editor|notepad exe|notpad|nodepad", "notepad.exe"),
    _a("Calculator", "calculator|calc|calculater|caculator|calcualtor|kikokotoo", "calc.exe|ms-calculator:"),
    _a("Paint", "paint|ms paint|mspaint|microsoft paint|paint app", "mspaint.exe|ms-paint:"),
    _a("Paint 3D", "paint 3d|paint three d|3d paint", "ms-paint:"),
    _a("WordPad", "wordpad|word pad|word pat", "wordpad.exe|write.exe"),
    _a("File Explorer", "file explorer|explorer|files|file manager|windows explorer|my computer|this pc|file browser|"
       "folder|folders|file exploror|fileexplorer", "explorer.exe"),
    _a("Task Manager", "task manager|taskmgr|tasks manager|task manger|processes|task manage", "taskmgr.exe"),
    _a("Snipping Tool", "snipping tool|snip|snipping|screenshot tool|snip and sketch|screen snip|snippet tool|"
       "snipping tools", "snippingtool.exe|ms-screenclip:"),
    _a("Control Panel", "control panel|control|controlpanel|old settings", "control.exe"),
    _a("Settings", "settings|windows settings|setting|system settings|pc settings|ms settings", "ms-settings:"),
    _a("Wi-Fi Settings", "wifi settings|wi fi settings|wifi|wi fi|network settings|internet settings|network",
       "ms-settings:network-wifi"),
    _a("Bluetooth Settings", "bluetooth settings|bluetooth|bluetooth devices", "ms-settings:bluetooth"),
    _a("Display Settings", "display settings|display|screen settings|resolution|screen resolution",
       "ms-settings:display"),
    _a("Sound Settings", "sound settings|sound|audio settings|volume settings|sound devices", "ms-settings:sound"),
    _a("Windows Update", "windows update|updates|check for updates|update windows", "ms-settings:windowsupdate"),
    _a("Apps and Features", "apps and features|installed apps|uninstall programs|programs and features|uninstall apps|"
       "add or remove programs", "ms-settings:appsfeatures|appwiz.cpl"),
    _a("Default Apps", "default apps|default programs", "ms-settings:defaultapps"),
    _a("Battery Settings", "battery settings|battery|power settings|battery saver", "ms-settings:batterysaver"),
    _a("Storage Settings", "storage settings|storage|disk space|storage sense", "ms-settings:storagesense"),
    _a("Network Connections", "network connections|network adapters|adapters", "ncpa.cpl"),
    _a("Device Manager", "device manager|devices manager|drivers|device mgr", "devmgmt.msc"),
    _a("Disk Management", "disk management|disk manager|partitions|partition manager|diskmgmt", "diskmgmt.msc"),
    _a("Services", "services|windows services|services console", "services.msc"),
    _a("Event Viewer", "event viewer|events|event log|event logs", "eventvwr.msc"),
    _a("Computer Management", "computer management|compmgmt|computer manager", "compmgmt.msc"),
    _a("Task Scheduler", "task scheduler|scheduler|scheduled tasks", "taskschd.msc"),
    _a("Registry Editor", "registry editor|regedit|registry|reg edit", "regedit.exe"),
    _a("System Information", "system information|system info|msinfo|msinfo32|sysinfo|pc info|computer info",
       "msinfo32.exe"),
    _a("Resource Monitor", "resource monitor|resmon|resources monitor", "resmon.exe"),
    _a("Performance Monitor", "performance monitor|perfmon|performance", "perfmon.exe"),
    _a("DirectX Diagnostic", "dxdiag|directx diagnostic|direct x|directx", "dxdiag.exe"),
    _a("Remote Desktop", "remote desktop|rdp|mstsc|remote desktop connection|remote connection", "mstsc.exe"),
    _a("Disk Cleanup", "disk cleanup|cleanup|clean up disk|cleanmgr|clean my pc", "cleanmgr.exe"),
    _a("On-Screen Keyboard", "on screen keyboard|onscreen keyboard|virtual keyboard|osk|screen keyboard", "osk.exe"),
    _a("Magnifier", "magnifier|magnify|zoom in screen|screen magnifier", "magnify.exe"),
    _a("Narrator", "narrator|screen reader", "narrator.exe"),
    _a("Character Map", "character map|charmap|special characters|symbols", "charmap.exe"),
    _a("Windows Security", "windows security|defender|windows defender|antivirus|security|virus protection",
       "windowsdefender:"),
    _a("Firewall", "firewall|windows firewall|firewall settings", "wf.msc"),
    _a("Sound Recorder", "sound recorder|voice recorder|recorder|record audio|audio recorder|voice memo",
       "ms-callrecording:|soundrecorder.exe"),
    _a("Clock", "clock|alarms|alarm clock|alarms and clock|timer app|world clock|stopwatch", "ms-clock:"),
    _a("Camera", "camera|webcam|windows camera|cam", "microsoft.windows.camera:"),
    _a("Photos", "photos|photo viewer|pictures|gallery|photo app|images", "ms-photos:"),
    _a("Microsoft Store", "microsoft store|store|windows store|app store|ms store", "ms-windows-store:"),
    _a("Maps", "maps|windows maps|bing maps", "bingmaps:"),
    _a("Weather", "weather|weather app|forecast", "bingweather:"),
    _a("Calendar", "calendar|windows calendar|my calendar", "outlookcal:"),
    _a("Mail", "windows mail|windows mail app|store mail", "outlookmail:"),
    _a("Email", "email|e mail|my email|my mail|mail|emails|e mails", "mailto:", "https://mail.google.com"),
    _a("Sticky Notes", "sticky notes|stickies|sticky note|sticky|post it", "ms-stickynotes:"),
    _a("Xbox", "xbox|xbox app|xbox game bar|game bar|gamebar", "xbox:|ms-gamebar:"),
    _a("Windows Media Player", "windows media player|media player|wmp|wmplayer", "wmplayer.exe"),
    _a("Movies and TV", "movies and tv|movies|films app", "mswindowsvideo:"),
    _a("Feedback Hub", "feedback hub|feedback", "windows-feedback:"),
    _a("Quick Assist", "quick assist|remote help", "quickassist.exe"),

    # ---- browsers
    _a("Google Chrome", "chrome|google chrome|crome|chrom|chrome browser|google browser|krome|chorme|cromium",
       "chrome.exe|" + _P + r"\Google\Chrome\Application\chrome.exe|" + _P86 + r"\Google\Chrome\Application\chrome.exe|"
       + _LA + r"\Google\Chrome\Application\chrome.exe", "https://www.google.com"),
    _a("your browser", "browser|web browser|internet|the internet|net|web|google|search engine|default browser",
       "https://www.google.com"),
    _a("Microsoft Edge", "edge|microsoft edge|ms edge|msedge|edge browser|internet explorer|ie|egde",
       "msedge.exe|microsoft-edge:"),
    _a("Firefox", "firefox|fire fox|mozilla|mozilla firefox|firefox browser|firfox|fyrefox",
       "firefox.exe|" + _P + r"\Mozilla Firefox\firefox.exe"),
    _a("Brave", "brave|brave browser|brave web browser|brav",
       "brave.exe|" + _P + r"\BraveSoftware\Brave-Browser\Application\brave.exe|"
       + _LA + r"\BraveSoftware\Brave-Browser\Application\brave.exe"),
    _a("Opera", "opera|opera browser|opra", "opera.exe|" + _LA + r"\Programs\Opera\opera.exe"),
    _a("Opera GX", "opera gx|operagx|gaming browser|opera g x", _LA + r"\Programs\Opera GX\opera.exe|opera_gx.exe"),
    _a("Vivaldi", "vivaldi|vivaldi browser", "vivaldi.exe|" + _LA + r"\Vivaldi\Application\vivaldi.exe"),
    _a("Tor Browser", "tor|tor browser|onion browser", "tor.exe|firefox.exe"),
    _a("DuckDuckGo", "duckduckgo|duck duck go|ddg", "", "https://duckduckgo.com"),

    # ---- Microsoft Office and friends
    _a("Word", "word|microsoft word|ms word|winword|word document|word processor|worde|wurd",
       "winword.exe|ms-word:ofe|word.exe"),
    _a("Excel", "excel|microsoft excel|ms excel|spreadsheet|spreadsheets|excell|exel|sheets",
       "excel.exe|ms-excel:ofe"),
    _a("PowerPoint", "powerpoint|power point|microsoft powerpoint|ms powerpoint|ppt|slides|presentation|"
       "powerpnt|power points", "powerpnt.exe|ms-powerpoint:ofe"),
    _a("Outlook", "outlook|microsoft outlook|ms outlook|outlook mail|outlok", "outlook.exe|olk.exe|outlookmail:"),
    _a("OneNote", "onenote|one note|microsoft onenote|ms onenote|one not", "onenote.exe|ms-onenote:"),
    _a("Access", "microsoft access|ms access|access database|msaccess", "msaccess.exe"),
    _a("Publisher", "publisher|microsoft publisher|ms publisher", "mspub.exe"),
    _a("Microsoft Teams", "teams|microsoft teams|ms teams|team|tims", "ms-teams.exe|msteams:|teams.exe|" + _LA + r"\Microsoft\Teams\current\Teams.exe"),
    _a("OneDrive", "onedrive|one drive|microsoft onedrive|ms onedrive", "onedrive.exe|" + _LA + r"\Microsoft\OneDrive\OneDrive.exe",
       "https://onedrive.live.com"),
    _a("Microsoft To Do", "to do|todo|microsoft to do|ms to do|tasks app", "ms-todo:"),
    _a("Microsoft Whiteboard", "whiteboard|ms whiteboard|microsoft whiteboard", "ms-whiteboard-cmd:"),
    _a("Microsoft Copilot", "copilot|ms copilot|windows copilot|microsoft copilot", "ms-copilot:|microsoft-edge:https://copilot.microsoft.com"),
    _a("LibreOffice", "libreoffice|libre office|libreoffice suite", "soffice.exe|" + _P + r"\LibreOffice\program\soffice.exe"),
    _a("LibreOffice Writer", "libreoffice writer|writer|libre writer", "swriter.exe|" + _P + r"\LibreOffice\program\swriter.exe"),
    _a("LibreOffice Calc", "libreoffice calc|libre calc|calc spreadsheet", "scalc.exe|" + _P + r"\LibreOffice\program\scalc.exe"),
    _a("LibreOffice Impress", "libreoffice impress|impress|libre impress", "simpress.exe|" + _P + r"\LibreOffice\program\simpress.exe"),
    _a("WPS Office", "wps|wps office|kingsoft office|wps writer", "wps.exe|wpsoffice.exe", "https://www.wps.com"),
    _a("Adobe Acrobat", "acrobat|adobe acrobat|adobe reader|acrobat reader|pdf reader|pdf viewer|reader|adobe pdf",
       "acrord32.exe|acrobat.exe|" + _P86 + r"\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe"),
    _a("Foxit Reader", "foxit|foxit reader|foxit pdf", "foxitreader.exe|foxitpdfreader.exe"),
    _a("SumatraPDF", "sumatra|sumatra pdf|sumatrapdf", "sumatrapdf.exe"),
    _a("Notion", "notion|notion app", "notion.exe|" + _LA + r"\Programs\Notion\Notion.exe", "https://www.notion.so"),
    _a("Obsidian", "obsidian|obsidian notes", "obsidian.exe|" + _LA + r"\Obsidian\Obsidian.exe"),
    _a("Evernote", "evernote|ever note", "evernote.exe", "https://www.evernote.com"),
    _a("Todoist", "todoist|to doist", "todoist.exe", "https://todoist.com"),
    _a("Trello", "trello", "", "https://trello.com"),
    _a("Google Drive", "google drive|gdrive|drive|g drive|my drive", "googledrivefs.exe|" + _P + r"\Google\Drive File Stream\launch.bat",
       "https://drive.google.com"),
    _a("Dropbox", "dropbox|drop box", "dropbox.exe|" + _P86 + r"\Dropbox\Client\Dropbox.exe", "https://www.dropbox.com"),
    _a("MEGA", "mega|megasync|mega sync", "megasync.exe", "https://mega.nz"),

    # ---- developer tools
    _a("Visual Studio Code", "vs code|vscode|v s code|visual studio code|code|vs-code|v scode|visual code|"
       "microsoft code|vs quote|vizual studio code|visual studio quote",
       _LA + r"\Programs\Microsoft VS Code\Code.exe|" + _P + r"\Microsoft VS Code\Code.exe|code.exe|code.cmd"),
    _a("Visual Studio", "visual studio|full visual studio|devenv|ms visual studio|microsoft visual studio",
       "devenv.exe"),
    _a("Cursor", "cursor|cursor editor|cursor ai", _LA + r"\Programs\cursor\Cursor.exe|cursor.exe"),
    _a("Windsurf", "windsurf|wind surf", "windsurf.exe"),
    _a("PyCharm", "pycharm|py charm|pie charm|jetbrains pycharm", "pycharm64.exe|pycharm.exe"),
    _a("IntelliJ IDEA", "intellij|intellij idea|idea|intelij|intel lij", "idea64.exe|idea.exe"),
    _a("WebStorm", "webstorm|web storm", "webstorm64.exe|webstorm.exe"),
    _a("Android Studio", "android studio|androidstudio|studio|android ide", "studio64.exe|studio.exe|"
       + _P + r"\Android\Android Studio\bin\studio64.exe"),
    _a("Eclipse", "eclipse|eclipse ide", "eclipse.exe"),
    _a("NetBeans", "netbeans|net beans", "netbeans64.exe|netbeans.exe"),
    _a("Sublime Text", "sublime|sublime text|sublime editor", "sublime_text.exe|" + _P + r"\Sublime Text\sublime_text.exe"),
    _a("Notepad++", "notepad++|notepad plus plus|notepad plus|notepadpp|notepad p p|npp|note pad plus plus",
       "notepad++.exe|" + _P + r"\Notepad++\notepad++.exe|" + _P86 + r"\Notepad++\notepad++.exe"),
    _a("Atom", "atom|atom editor", "atom.exe"),
    _a("Vim", "vim|gvim|neovim|nvim", "gvim.exe|nvim.exe|vim.exe"),
    _a("Git GUI", "git gui|git", "git-gui.exe|git.exe"),
    _a("GitHub Desktop", "github desktop|git hub desktop|github|git hub|github app", "githubdesktop.exe|"
       + _LA + r"\GitHubDesktop\GitHubDesktop.exe", "https://github.com"),
    _a("GitKraken", "gitkraken|git kraken", "gitkraken.exe", ""),
    _a("Postman", "postman|post man|api tester", "postman.exe|" + _LA + r"\Postman\Postman.exe", "https://web.postman.co"),
    _a("Insomnia", "insomnia|insomnia rest", "insomnia.exe"),
    _a("Docker Desktop", "docker|docker desktop|dockers|doka", _P + r"\Docker\Docker\Docker Desktop.exe|docker.exe"),
    _a("XAMPP", "xampp|zampp|exampp|xamp|x a m p p|lamp", r"C:\xampp\xampp-control.exe"),
    _a("WampServer", "wamp|wampserver|wamp server", r"C:\wamp64\wampmanager.exe"),
    _a("Laragon", "laragon", r"C:\laragon\laragon.exe"),
    _a("MySQL Workbench", "mysql workbench|mysql|my sql|workbench|sql workbench", "mysqlworkbench.exe"),
    _a("DBeaver", "dbeaver|d beaver|db beaver", "dbeaver.exe"),
    _a("DB Browser for SQLite", "db browser|sqlite browser|sqlite|db browser for sqlite|sql lite", "db browser for sqlite.exe"),
    _a("SQL Server Management Studio", "ssms|sql server|sql server management studio|sql management studio", "ssms.exe"),
    _a("pgAdmin", "pgadmin|pg admin|postgres|postgresql", "pgadmin4.exe"),
    _a("MongoDB Compass", "mongodb compass|compass|mongo|mongodb|mongo compass", "mongodbcompass.exe"),
    _a("FileZilla", "filezilla|file zilla|ftp|ftp client", "filezilla.exe"),
    _a("PuTTY", "putty|putti|ssh client|ssh", "putty.exe"),
    _a("WinSCP", "winscp|win scp|scp", "winscp.exe"),
    _a("Wireshark", "wireshark|wire shark|packet sniffer", "wireshark.exe"),
    _a("Burp Suite", "burp|burp suite|burpsuite", "burpsuite_community.exe|burpsuite.exe"),
    _a("Nmap", "nmap|zenmap|n map", "zenmap.exe|nmap.exe"),
    _a("VirtualBox", "virtualbox|virtual box|vbox|oracle virtualbox|vm box", "virtualbox.exe|" + _P + r"\Oracle\VirtualBox\VirtualBox.exe"),
    _a("VMware Workstation", "vmware|vm ware|vmware workstation|vmware player|virtual machine|vmplayer", "vmware.exe|vmplayer.exe"),
    _a("Hyper-V Manager", "hyper v|hyperv|hyper-v manager|hyper v manager", "virtmgmt.msc"),
    _a("Unity Hub", "unity|unity hub|unityhub|unity engine", "unity hub.exe|" + _P + r"\Unity Hub\Unity Hub.exe"),
    _a("Unreal Engine", "unreal|unreal engine|epic unreal|ue5|ue4", "unrealeditor.exe|ue4editor.exe"),
    _a("Godot", "godot|godot engine", "godot.exe"),
    _a("Blender", "blender|blender 3d|blendr", "blender.exe|" + _P + r"\Blender Foundation\Blender\blender.exe"),
    _a("Arduino IDE", "arduino|arduino ide|arduino software", "arduino ide.exe|arduino.exe"),
    _a("Figma", "figma|fig ma|figma app", "figma.exe|" + _LA + r"\Figma\Figma.exe", "https://www.figma.com"),
    _a("Flutter", "flutter|flutter sdk", "flutter.exe"),
    _a("Jupyter", "jupyter|jupyter notebook|jupiter|jupyter lab|jupyterlab|notebook", "jupyter-notebook.exe|jupyter.exe"),
    _a("Spyder", "spyder|spider ide|spyder ide", "spyder.exe"),
    _a("RStudio", "rstudio|r studio|r", "rstudio.exe"),
    _a("MATLAB", "matlab|mat lab|mathlab", "matlab.exe"),
    _a("Ngrok", "ngrok|en grok", "ngrok.exe"),
    _a("Cisco Packet Tracer", "packet tracer|cisco packet tracer|cisco", "packettracer.exe"),
    _a("GNS3", "gns3|gns 3", "gns3.exe"),
    _a("Kali Linux", "kali|kali linux", "kali.exe|kali-linux.exe"),
    _a("Cmder", "cmder|cmd er", "cmder.exe"),

    # ---- chat, calls and social
    _a("WhatsApp", "whatsapp|whats app|watsapp|what's app|whatsup|whatsap|wa|whatapp|wasap",
       "whatsapp.exe|whatsapp:|" + _LA + r"\WhatsApp\WhatsApp.exe", "https://web.whatsapp.com"),
    _a("Telegram", "telegram|telegram desktop|telegramm|tg|telegraam", "telegram.exe|" + _RA + r"\Telegram Desktop\Telegram.exe|tg:",
       "https://web.telegram.org"),
    _a("Discord", "discord|disc ord|dis cord|discourd", "discord.exe|" + _LA + r"\Discord\Update.exe --processStart Discord.exe", "https://discord.com/app"),
    _a("Slack", "slack|slak", "slack.exe|" + _LA + r"\slack\slack.exe", "https://app.slack.com"),
    _a("Zoom", "zoom|zoom meeting|zoom meetings|zoom app|zoom video|zum", "zoom.exe|" + _RA + r"\Zoom\bin\Zoom.exe|zoommtg:",
       "https://zoom.us"),
    _a("Skype", "skype|skipe|skyp", "skype.exe|skype:", "https://web.skype.com"),
    _a("Signal", "signal|signal messenger|signal app", "signal.exe|" + _LA + r"\Programs\signal-desktop\Signal.exe"),
    _a("Messenger", "messenger|facebook messenger|fb messenger|messanger", "messenger.exe", "https://www.messenger.com"),
    _a("Viber", "viber|vyber", "viber.exe", "https://www.viber.com"),
    _a("WeChat", "wechat|we chat|weixin|wechat app", "wechat.exe"),
    _a("Line", "line|line messenger", "line.exe"),
    _a("Google Meet", "meet|google meet|g meet|gmeet", "", "https://meet.google.com"),
    _a("Facebook", "facebook|face book|fb|fbook", "", "https://www.facebook.com"),
    _a("Instagram", "instagram|insta|ig|instagramm|insta gram", "", "https://www.instagram.com"),
    _a("TikTok", "tiktok|tik tok|tic toc|tiktok app", "tiktok.exe", "https://www.tiktok.com"),
    _a("X (Twitter)", "twitter|x|x twitter|tweeter|tweet|twiter", "", "https://x.com"),
    _a("LinkedIn", "linkedin|linked in|linkdin", "", "https://www.linkedin.com"),
    _a("Snapchat", "snapchat|snap chat|snap", "", "https://www.snapchat.com"),
    _a("Reddit", "reddit|redit|red it", "", "https://www.reddit.com"),
    _a("Pinterest", "pinterest|pin interest|pintrest", "", "https://www.pinterest.com"),
    _a("Threads", "threads|threads app", "", "https://www.threads.net"),
    _a("Gmail", "gmail|g mail|google mail|jimail|gmal", "", "https://mail.google.com"),
    _a("Yahoo Mail", "yahoo|yahoo mail|ya hoo", "", "https://mail.yahoo.com"),
    _a("Thunderbird", "thunderbird|thunder bird|mozilla mail", "thunderbird.exe|" + _P + r"\Mozilla Thunderbird\thunderbird.exe"),
    _a("Proton Mail", "proton mail|protonmail|proton", "", "https://mail.proton.me"),

    # ---- music, video and streaming
    _a("Spotify", "spotify|spot ify|spotifi|spotty fy|music app|spotfy", "spotify.exe|spotify:|" + _RA + r"\Spotify\Spotify.exe",
       "https://open.spotify.com"),
    _a("YouTube", "youtube|you tube|yt|utube|youtub|u tube|yutube", "", "https://www.youtube.com"),
    _a("YouTube Music", "youtube music|yt music|you tube music|ytmusic", "", "https://music.youtube.com"),
    _a("Netflix", "netflix|net flix|netflicks|netflx", "netflix.exe|netflix:", "https://www.netflix.com"),
    _a("Prime Video", "prime video|amazon prime|prime|amazon video|amazon prime video", "", "https://www.primevideo.com"),
    _a("Disney Plus", "disney plus|disney+|disney|disney plus app|disneyplus", "", "https://www.disneyplus.com"),
    _a("Showmax", "showmax|show max", "", "https://www.showmax.com"),
    _a("Twitch", "twitch|twich", "twitch.exe", "https://www.twitch.tv"),
    _a("VLC", "vlc|vlc media player|vlc player|vlc app|v l c|video lan|video player|vlc media|vlcc",
       "vlc.exe|" + _P + r"\VideoLAN\VLC\vlc.exe|" + _P86 + r"\VideoLAN\VLC\vlc.exe"),
    _a("iTunes", "itunes|i tunes|apple music|apple itunes", "itunes.exe|" + _P + r"\iTunes\iTunes.exe"),
    _a("Groove Music", "groove music|groove|media app", "mswindowsmusic:"),
    _a("MPC-HC", "mpc|mpc hc|media player classic|mpc player|potplayer|pot player", "mpc-hc64.exe|mpc-hc.exe|potplayermini64.exe"),
    _a("KMPlayer", "kmplayer|km player|kmp", "kmplayer.exe"),
    _a("Audacity", "audacity|audasity|audio editor", "audacity.exe|" + _P + r"\Audacity\Audacity.exe"),
    _a("OBS Studio", "obs|obs studio|o b s|open broadcaster|screen recorder|record screen|streaming software|obs recorder",
       "obs64.exe|obs.exe|" + _P + r"\obs-studio\bin\64bit\obs64.exe"),
    _a("Streamlabs", "streamlabs|stream labs|streamlabs obs", "streamlabs obs.exe|streamlabs desktop.exe"),
    _a("HandBrake", "handbrake|hand brake|video converter", "handbrake.exe"),
    _a("DaVinci Resolve", "davinci resolve|da vinci resolve|davinci|da vinci|resolve|video editor", "resolve.exe"),
    _a("CapCut", "capcut|cap cut|cut cap|capcut editor", "capcut.exe|" + _LA + r"\CapCut\Apps\CapCut.exe"),
    _a("Kdenlive", "kdenlive|k den live", "kdenlive.exe"),
    _a("Shotcut", "shotcut|shot cut", "shotcut.exe"),
    _a("Camtasia", "camtasia|camtasia studio", "camtasia.exe|camtasia studio.exe"),
    _a("Filmora", "filmora|wondershare filmora|filmora editor", "filmora.exe"),
    _a("Deezer", "deezer|deezer music", "deezer.exe", "https://www.deezer.com"),
    _a("Audiomack", "audiomack|audio mack", "", "https://audiomack.com"),
    _a("SoundCloud", "soundcloud|sound cloud", "", "https://soundcloud.com"),
    _a("Boomplay", "boomplay|boom play", "", "https://www.boomplay.com"),
    _a("Shazam", "shazam|sha zam", "", "https://www.shazam.com"),

    # ---- creative tools
    _a("Photoshop", "photoshop|photo shop|adobe photoshop|ps photoshop|photoshap|fotoshop", "photoshop.exe|" + _P + r"\Adobe\Adobe Photoshop 2024\Photoshop.exe"),
    _a("Illustrator", "illustrator|adobe illustrator|ai illustrator|illustrater", "illustrator.exe"),
    _a("Premiere Pro", "premiere|premiere pro|adobe premiere|premier pro|premier", "adobe premiere pro.exe"),
    _a("After Effects", "after effects|aftereffects|adobe after effects|ae", "afterfx.exe"),
    _a("Lightroom", "lightroom|light room|adobe lightroom|lr", "lightroom.exe"),
    _a("InDesign", "indesign|in design|adobe indesign", "indesign.exe"),
    _a("Adobe XD", "adobe xd|xd|experience design", "xd.exe"),
    _a("Creative Cloud", "creative cloud|adobe|adobe creative cloud|cc|adobe cc", "creative cloud.exe|" + _P + r"\Adobe\Adobe Creative Cloud\ACC\Creative Cloud.exe"),
    _a("Canva", "canva|can va|canvas design|canva design", "canva.exe", "https://www.canva.com"),
    _a("GIMP", "gimp|g imp|gym p|gimp editor|gnu image", "gimp-2.10.exe|gimp.exe"),
    _a("Inkscape", "inkscape|ink scape", "inkscape.exe"),
    _a("Krita", "krita|kreeta", "krita.exe"),
    _a("Paint.NET", "paint net|paint dot net|paintdotnet|paint.net", "paintdotnet.exe"),
    _a("Lightshot", "lightshot|light shot|screenshot app", "lightshot.exe"),
    _a("ShareX", "sharex|share x", "sharex.exe"),
    _a("Sketchbook", "sketchbook|sketch book", "sketchbook.exe"),
    _a("SketchUp", "sketchup|sketch up", "sketchup.exe"),
    _a("AutoCAD", "autocad|auto cad|cad|autocadd", "acad.exe"),
    _a("Revit", "revit|ravit", "revit.exe"),
    _a("SolidWorks", "solidworks|solid works|solidwork", "sldworks.exe"),
    _a("Fusion 360", "fusion 360|fusion|autodesk fusion", "fusion360.exe"),
    _a("Cura", "cura|ultimaker cura", "ultimaker-cura.exe|cura.exe"),

    # ---- games and game launchers
    _a("Steam", "steam|steam games|stem|steam app|steam launcher", "steam.exe|steam:|" + _P86 + r"\Steam\steam.exe"),
    _a("Epic Games", "epic games|epic|epic launcher|epic games launcher|epic store|fortnite", "epicgameslauncher.exe|"
       + _P86 + r"\Epic Games\Launcher\Portal\Binaries\Win32\EpicGamesLauncher.exe"),
    _a("EA App", "ea app|ea|origin|ea desktop|ea play|ea launcher", "eadesktop.exe|origin.exe"),
    _a("Ubisoft Connect", "ubisoft|ubisoft connect|uplay|u play", "ubisoftconnect.exe|upc.exe"),
    _a("Battle.net", "battle net|battlenet|blizzard|battle.net|blizzard launcher", "battle.net.exe"),
    _a("GOG Galaxy", "gog|gog galaxy|good old games", "galaxyclient.exe"),
    _a("Riot Client", "riot|riot client|riot games|valorant|league of legends|league", "riotclientservices.exe"),
    _a("Minecraft", "minecraft|mine craft|minecraft launcher|mine kraft", "minecraftlauncher.exe|minecraft:"),
    _a("Roblox", "roblox|ro blox|robux", "robloxplayerbeta.exe|roblox-player:"),
    _a("GeForce Experience", "geforce experience|geforce|nvidia experience|nvidia app|nvidia", "nvidia app.exe|nvidia geforce experience.exe"),
    _a("NVIDIA Control Panel", "nvidia control panel|nvidia panel|graphics settings", "nvcplui.exe"),
    _a("AMD Software", "amd software|amd adrenalin|adrenalin|amd|radeon|amd radeon", "radeonsoftware.exe"),
    _a("MSI Afterburner", "afterburner|msi afterburner", "msiafterburner.exe"),
    _a("Rockstar Launcher", "rockstar|rockstar launcher|gta|gta 5|gta v|rockstar games", "launcher.exe"),
    _a("Call of Duty", "call of duty|cod|warzone|call of duty warzone|modern warfare", "battle.net.exe"),
    _a("FIFA", "fifa|ea sports fc|fc 24|fc 25|ea fc|efootball|e football|pes", "eadesktop.exe"),
    _a("BlueStacks", "bluestacks|blue stacks|android emulator|emulator|blue stack", "hd-player.exe|bluestacks.exe"),
    _a("LDPlayer", "ldplayer|ld player|ld emulator", "dnplayer.exe|ldplayer.exe"),
    _a("Nox Player", "nox|nox player|noxplayer", "nox.exe"),

    # ---- utilities
    _a("7-Zip", "7zip|7 zip|seven zip|7-zip|zip|7z|sevenzip|7 z", "7zfm.exe|" + _P + r"\7-Zip\7zFM.exe"),
    _a("WinRAR", "winrar|win rar|rar|winrar archiver|win rar archiver", "winrar.exe|" + _P + r"\WinRAR\WinRAR.exe"),
    _a("WinZip", "winzip|win zip", "winzip64.exe|winzip32.exe"),
    _a("CCleaner", "ccleaner|c cleaner|cleaner|ccleaner app|see cleaner", "ccleaner64.exe|ccleaner.exe"),
    _a("TeamViewer", "teamviewer|team viewer|team view", "teamviewer.exe|" + _P86 + r"\TeamViewer\TeamViewer.exe"),
    _a("AnyDesk", "anydesk|any desk|anydisk", "anydesk.exe"),
    _a("qBittorrent", "qbittorrent|q bittorrent|torrent|bittorrent|torrent client|qbit", "qbittorrent.exe"),
    _a("uTorrent", "utorrent|u torrent|micro torrent|mu torrent", "utorrent.exe"),
    _a("Internet Download Manager", "idm|internet download manager|download manager|i d m|internet downloader", "idman.exe|"
       + _P86 + r"\Internet Download Manager\IDMan.exe"),
    _a("Free Download Manager", "free download manager|fdm", "fdm.exe"),
    _a("Everything", "everything|everything search|voidtools|file search tool", "everything.exe"),
    _a("CPU-Z", "cpu z|cpuz|cpu-z|processor info", "cpuz.exe|cpuz_x64.exe"),
    _a("GPU-Z", "gpu z|gpuz|gpu-z|graphics card info", "gpu-z.exe"),
    _a("HWiNFO", "hwinfo|hw info|hardware info|hwinfo64", "hwinfo64.exe|hwinfo32.exe"),
    _a("Speccy", "speccy|spec ee", "speccy64.exe|speccy.exe"),
    _a("Rufus", "rufus|boot usb maker|bootable usb", "rufus.exe"),
    _a("balenaEtcher", "etcher|balena etcher|balenaetcher", "balenaetcher.exe"),
    _a("Ventoy", "ventoy|ven toy", "ventoy2disk.exe"),
    _a("Malwarebytes", "malwarebytes|malware bytes|malware|anti malware", "mbam.exe"),
    _a("Avast", "avast|a vast", "avastui.exe"),
    _a("AVG", "avg|a v g", "avgui.exe"),
    _a("Kaspersky", "kaspersky|casper sky", "avpui.exe"),
    _a("ESET", "eset|eset nod32|nod32", "egui.exe"),
    _a("Norton", "norton|norton security|norton antivirus", "nortonui.exe"),
    _a("McAfee", "mcafee|mac afee|macafee", "mcuicnt.exe"),
    _a("Bitwarden", "bitwarden|bit warden", "bitwarden.exe"),
    _a("KeePass", "keepass|keepassxc|keep pass|password manager", "keepass.exe|keepassxc.exe"),
    _a("NordVPN", "nordvpn|nord vpn|nord", "nordvpn.exe"),
    _a("ProtonVPN", "protonvpn|proton vpn|vpn", "protonvpn.exe"),
    _a("ExpressVPN", "expressvpn|express vpn", "expressvpn.exe"),
    _a("Psiphon", "psiphon|sai fon|psi phon", "psiphon3.exe"),
    _a("Windscribe", "windscribe|wind scribe", "windscribe.exe"),
    _a("Cloudflare WARP", "warp|cloudflare warp|1.1.1.1", "warp-taskbar.exe"),
    _a("Ookla Speedtest", "speedtest|speed test|internet speed", "speedtest.exe", "https://www.speedtest.net"),
    _a("Lively Wallpaper", "lively|lively wallpaper|wallpaper engine|wallpaper", "livelywpf.exe|wallpaper32.exe"),
    _a("Rainmeter", "rainmeter|rain meter", "rainmeter.exe"),
    _a("PowerToys", "powertoys|power toys|microsoft powertoys", "powertoys.exe|" + _LA + r"\PowerToys\PowerToys.exe"),
    _a("Sandboxie", "sandboxie|sandbox", "sbiectrl.exe"),
    _a("Mozilla VPN", "mozilla vpn", "mozillavpn.exe"),

    # ---- AI apps and assistants (desktop apps if installed, otherwise the web)
    _a("ChatGPT", "chatgpt|chat gpt|chat g p t|gpt|openai|open ai|chat jpt|chatgbt", "chatgpt.exe|chatgpt:", "https://chatgpt.com"),
    _a("Claude", "claude|claud|anthropic|clod", "claude.exe", "https://claude.ai"),
    _a("Gemini", "gemini|google gemini|gemini ai|jemini|bard|google bard", "", "https://gemini.google.com"),
    _a("Perplexity", "perplexity|perplexity ai", "", "https://www.perplexity.ai"),
    _a("Grok", "grok|grock", "", "https://grok.com"),
    _a("DeepSeek", "deepseek|deep seek", "", "https://chat.deepseek.com"),
    _a("Google AI Studio", "ai studio|google ai studio|aistudio", "", "https://aistudio.google.com"),
    _a("Ollama", "ollama|olama|o llama", "ollama app.exe|ollama.exe"),
    _a("LM Studio", "lm studio|lmstudio|l m studio", "lm studio.exe"),

    # ---- Google, shopping, money, and other web services (opened in the browser)
    _a("Google Docs", "google docs|g docs|gdocs|google document", "", "https://docs.google.com"),
    _a("Google Sheets", "google sheets|g sheets|gsheets|google spreadsheet", "", "https://sheets.google.com"),
    _a("Google Slides", "google slides|g slides|gslides", "", "https://slides.google.com"),
    _a("Google Calendar", "google calendar|g calendar|gcal", "", "https://calendar.google.com"),
    _a("Google Maps", "google maps|g maps|gmaps|directions", "", "https://maps.google.com"),
    _a("Google Photos", "google photos|g photos", "", "https://photos.google.com"),
    _a("Google Translate", "google translate|translate|translator|g translate", "", "https://translate.google.com"),
    _a("Google Classroom", "classroom|google classroom|g classroom", "", "https://classroom.google.com"),
    _a("Google Keep", "keep|google keep|g keep|keep notes", "", "https://keep.google.com"),
    _a("Google Earth", "google earth|earth", "googleearth.exe|googleearthpro.exe", "https://earth.google.com"),
    _a("Google Scholar", "google scholar|scholar", "", "https://scholar.google.com"),
    _a("Amazon", "amazon|amazon shopping|amazone|amazon store", "", "https://www.amazon.com"),
    _a("Jumia", "jumia|jumia kenya|jumia shopping", "", "https://www.jumia.co.ke"),
    _a("Kilimall", "kilimall|kili mall", "", "https://www.kilimall.co.ke"),
    _a("Jiji", "jiji|jiji kenya|jiji.co.ke", "", "https://jiji.co.ke"),
    _a("AliExpress", "aliexpress|ali express|ali baba|alibaba", "", "https://www.aliexpress.com"),
    _a("eBay", "ebay|e bay", "", "https://www.ebay.com"),
    _a("PayPal", "paypal|pay pal", "", "https://www.paypal.com"),
    _a("M-Pesa", "mpesa|m pesa|m-pesa|safaricom mpesa", "", "https://www.safaricom.co.ke/main-mpesa/m-pesa-services"),
    _a("Safaricom", "safaricom|safaricom website|saf", "", "https://www.safaricom.co.ke"),
    _a("KRA iTax", "kra|itax|i tax|kra itax|kra portal|tax portal", "", "https://itax.kra.go.ke"),
    _a("eCitizen", "ecitizen|e citizen|e-citizen|ecitizen portal", "", "https://www.ecitizen.go.ke"),
    _a("Wikipedia", "wikipedia|wiki|wikipedya", "", "https://www.wikipedia.org"),
    _a("Stack Overflow", "stack overflow|stackoverflow|stack", "", "https://stackoverflow.com"),
    _a("W3Schools", "w3schools|w3 schools|w three schools", "", "https://www.w3schools.com"),
    _a("MDN", "mdn|mdn docs|mozilla docs", "", "https://developer.mozilla.org"),
    _a("PythonAnywhere", "pythonanywhere|python anywhere", "", "https://www.pythonanywhere.com"),
    _a("Vercel", "vercel", "", "https://vercel.com"),
    _a("Netlify", "netlify", "", "https://www.netlify.com"),
    _a("Replit", "replit|repl it", "", "https://replit.com"),
    _a("CodePen", "codepen|code pen", "", "https://codepen.io"),
    _a("Kaggle", "kaggle", "", "https://www.kaggle.com"),
    _a("Hugging Face", "hugging face|huggingface", "", "https://huggingface.co"),
    _a("Coursera", "coursera", "", "https://www.coursera.org"),
    _a("Udemy", "udemy|u demy", "", "https://www.udemy.com"),
    _a("Khan Academy", "khan academy|khan", "", "https://www.khanacademy.org"),
    _a("Duolingo", "duolingo|duo lingo", "", "https://www.duolingo.com"),
    _a("Moodle", "moodle|e learning|elearning", "", "https://moodle.org"),
    _a("Google Chrome Web Store", "chrome web store|chrome extensions|extensions", "", "https://chromewebstore.google.com"),
    _a("Uber", "uber", "", "https://www.uber.com"),
    _a("Bolt", "bolt|bolt taxi", "", "https://bolt.eu"),
    _a("Airbnb", "airbnb|air bnb", "", "https://www.airbnb.com"),
    _a("Booking.com", "booking|booking.com|booking com", "", "https://www.booking.com"),
    _a("BBC News", "bbc|bbc news", "", "https://www.bbc.com/news"),
    _a("CNN", "cnn", "", "https://edition.cnn.com"),
    _a("Citizen Digital", "citizen digital|citizen tv|citizen", "", "https://www.citizen.digital"),
    _a("Nation Africa", "nation|nation africa|daily nation", "", "https://nation.africa"),
    _a("Tuko", "tuko|tuko news|tuko kenya", "", "https://www.tuko.co.ke"),
    _a("Google News", "google news|news", "", "https://news.google.com"),
    _a("Weather.com", "weather website|weather site", "", "https://weather.com"),
]


# ---------------------------------------------------------------------------------------------- names
_STRIP_PREFIX = re.compile(r"^(?:(?:please|kindly|just|fungua|open|launch|start|run)\s+)+", re.I)
_ARTICLE = re.compile(r"^(?:the|my|a|an|this|that|our)\s+", re.I)
_STRIP_SUFFIX = re.compile(r"\s+(?:app|apps|application|program|software|browser|for me|please|window|desktop app|exe)$", re.I)


def norm(text) -> str:
    """Lowercase, no punctuation (keeping + and #), no filler words: 'Open the Chrome app, please' -> 'chrome'."""
    s = str(text or "").lower().strip()
    s = s.replace("&", " and ").replace("_", " ").replace("-", " ").replace(".", " ")
    s = re.sub(r"[^\w+#\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(4):
        before = s
        s = _STRIP_PREFIX.sub("", s)
        s = _ARTICLE.sub("", s)
        s = _STRIP_SUFFIX.sub("", s)
        if s == before:
            break
    return s.strip()


def compact(s: str) -> str:
    return s.replace(" ", "")


def _skeleton(s: str) -> str:
    """Rough sound of a word: first letter, then consonants, doubles collapsed ('crome' ~ 'chrome')."""
    s = compact(s)
    if not s:
        return ""
    s = s.replace("ph", "f").replace("ch", "c").replace("ck", "k").replace("kn", "n").replace("wh", "w").replace("x", "ks")
    out = s[0]
    for ch in s[1:]:
        if ch in "aeiouyh":
            continue
        if ch != out[-1]:
            out += ch
    return out


# ---------------------------------------------------------------------------------------------- scoring
# Ordinary words that must never be guessed into an app ("open what" is noise, not WhatsApp).
_STOPWORDS = {"what", "who", "when", "where", "why", "how", "this", "that", "there", "here", "then", "them", "they",
              "with", "from", "have", "will", "just", "okay", "yes", "yeah", "thanks", "thank", "hello", "please",
              "time", "date", "today", "something", "anything", "nothing", "everything", "some", "more", "next",
              "back", "again", "sure", "maybe", "good", "nice", "cool", "fine", "well", "now", "one", "two", "it"}

AUTO_SCORE = 0.85      # at or above: open it
ASK_SCORE = 0.65       # between: "Did you mean ...?"


def score(q: str, cand: str) -> float:
    """How well does the spoken/typed name q match the candidate name? Both already norm()ed."""
    if not q or not cand:
        return 0.0
    qc, cc = compact(q), compact(cand)
    if q == cand or qc == cc:
        return 1.0
    if len(qc) < 3:
        return 0.0                       # very short names must match exactly (no guessing)
    if cc.startswith(qc):
        if len(qc) < 6 and len(qc) < 0.6 * len(cc):
            return 0.70                  # "what" is the start of "whatsapp", but too little to go on: ask
        return 0.93 - min(0.20, (len(cc) - len(qc)) * 0.02)
    if q in cand.split():
        return 0.88
    base = 0.70 if len(cc) >= 4 and cc in qc else 0.0   # "chrome extensions tool": worth asking about, never guessing
    if len(qc) < 4:
        return base
    if re.search(r"\s\w$", q):
        return base                      # "drive d", "disk e": a trailing letter is a drive letter, not a typo
    r = difflib.SequenceMatcher(None, qc, cc).ratio()
    sq, sc = _skeleton(qc), _skeleton(cc)
    if len(sq) >= 3 and len(sc) >= 3 and sq[0] == sc[0]:
        r = max(r, difflib.SequenceMatcher(None, sq, sc).ratio() * 0.9)
    return max(r, base)


# ---------------------------------------------------------------------------------------------- results
@dataclass
class Plan:
    """How to open something. `tries` is a list of (kind, value); kinds: file, uri, exe, uwp, url."""
    label: str
    tries: list
    web: str = ""
    source: str = "registry"     # registry | installed | web | custom
    score: float = 1.0


class Unsure(ToolError):
    """Not clear which app is meant. The message lists options so the user can say which."""

    def __init__(self, message: str, options: list[str]):
        super().__init__(message)
        self.options = options


def _kind_of(target: str) -> tuple[str, str]:
    t = target.strip()
    if re.match(r"^https?://", t, re.I):
        return "url", t
    if "%" in t or "\\" in t or "/" in t or re.match(r"^[a-zA-Z]:", t):
        return "file", os.path.expandvars(t)
    if re.match(r"^[a-z][a-z0-9.+-]{1,}:", t, re.I) and not t.lower().endswith((".exe", ".msc", ".cpl", ".cmd", ".bat")):
        return "uri", t
    return "exe", t


# Windows' own link types that are always safe to open. Any other link type ("netflix:", "whatsapp:", "spotify:")
# is opened only if an app has really registered it: otherwise Windows shows "Get an app to open this link".
_INBOX_SCHEMES = {"microsoft-edge", "bingmaps", "bingweather", "outlookcal", "outlookmail", "xbox", "windowsdefender",
                  "mswindowsvideo", "mswindowsmusic", "windows-feedback", "microsoft.windows.camera", "ms"}


def uri_registered(uri: str) -> bool:
    scheme = uri.split(":", 1)[0].lower()
    if scheme.startswith("ms-") or scheme in _INBOX_SCHEMES:
        return True
    if os.name != "nt":
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme) as key:
            winreg.QueryValueEx(key, "URL Protocol")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------------------------- catalog
def _desktop_dirs() -> list[Path]:
    home = Path.home()
    out = [home / "Desktop", home / "OneDrive" / "Desktop"]
    pub = os.environ.get("PUBLIC")
    if pub:
        out.append(Path(pub) / "Desktop")
    return out


def _start_dirs() -> list[Path]:
    return [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]


_JUNK = re.compile(r"uninstall|readme|read me|release notes|documentation|license|help|website|manual|"
                   r"what's new|whats new|setup|installer|update|changelog|support|samples?", re.I)


def scan_shortcuts() -> dict[str, tuple[str, str]]:
    """Start Menu + Desktop shortcuts: normalized name -> ('file', path)."""
    found: dict[str, tuple[str, str]] = {}
    for d in [*_start_dirs(), *_desktop_dirs()]:
        try:
            if not d.is_dir():
                continue
            for lnk in d.rglob("*.lnk"):
                if _JUNK.search(lnk.stem):
                    continue
                found.setdefault(norm(lnk.stem), ("file", str(lnk)))
        except OSError:
            continue
    return found


def scan_app_paths() -> dict[str, tuple[str, str]]:
    """Registry 'App Paths': programs that registered themselves (chrome.exe, winword.exe, ...)."""
    found: dict[str, tuple[str, str]] = {}
    if os.name != "nt":
        return found
    try:
        import winreg
    except ImportError:
        return found
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, base) as key:
                count = winreg.QueryInfoKey(key)[0]
                for i in range(count):
                    try:
                        sub = winreg.EnumKey(key, i)
                        if not sub.lower().endswith(".exe"):
                            continue
                        with winreg.OpenKey(key, sub) as k2:
                            path, _t = winreg.QueryValueEx(k2, "")
                        path = os.path.expandvars(str(path).strip('"'))
                        if path and os.path.exists(path):
                            found.setdefault(norm(sub[:-4]), ("file", path))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def scan_store_apps(timeout: float = 45.0) -> dict[str, tuple[str, str]]:
    """Apps Windows lists (Get-StartApps), keeping Store apps only if the package is REALLY installed.
    (The Start menu can show pinned placeholders for apps that are not installed, like WhatsApp or Netflix.)"""
    found: dict[str, tuple[str, str]] = {}
    if os.name != "nt":
        return found
    script = ("$f=@(Get-AppxPackage | ForEach-Object { $_.PackageFamilyName }); "
              "$a=@(Get-StartApps | Select-Object Name,AppID); "
              "@{apps=$a; fams=$f} | ConvertTo-Json -Compress -Depth 3")
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        raw = proc.stdout.decode("utf-8", "replace").strip()
        if not raw:
            return found
        data = json.loads(raw)
        rows = data.get("apps") or []
        if isinstance(rows, dict):
            rows = [rows]
        fams = data.get("fams") or []
        fams = {str(x).lower() for x in ([fams] if isinstance(fams, str) else fams)}
        for row in rows:
            name, appid = str(row.get("Name", "")).strip(), str(row.get("AppID", "")).strip()
            if not name or not appid or _JUNK.search(name):
                continue
            if "!" in appid and fams and appid.split("!")[0].lower() not in fams:
                continue                     # a pinned placeholder, not an installed app
            found.setdefault(norm(name), ("uwp", appid))
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        pass
    return found


class AppCatalog:
    """The registry plus what is installed on this PC. Thread safe; scans lazily and in the background."""

    CACHE_HOURS = 12

    def __init__(self, extra_aliases: dict | None = None, custom_path: Path | None = None,
                 cache_path: Path | None = None, scanners: dict | None = None):
        self.extra_aliases = {norm(k): norm(v) for k, v in (extra_aliases or {}).items() if k and v}
        self.custom_path = Path(custom_path) if custom_path else ROOT / "apps.json"
        self.cache_path = Path(cache_path) if cache_path else ROOT / "data" / "apps_cache_v2.json"
        self.scanners = scanners or {"shortcuts": scan_shortcuts, "apppaths": scan_app_paths, "store": scan_store_apps}
        self.lock = threading.RLock()
        self.installed: dict[str, tuple[str, str]] = {}      # norm name -> (kind, value)
        self._scanned_at = 0.0
        self._store_started = False
        self._store_cache: dict | None = None
        self._custom_mtime = -1
        self._custom_size = -1
        self._custom: list[App] = []
        self._by_alias: dict[str, App] = {}
        self._pool: list[tuple[str, App]] = []
        self._rebuild_registry()

    # -- registry ---------------------------------------------------------------------------
    def _load_custom(self) -> list[App]:
        try:
            st = self.custom_path.stat()
            state = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._custom_mtime, self._custom_size, self._custom = -1, -1, []
            return self._custom
        if state == (self._custom_mtime, self._custom_size):
            return self._custom
        apps: list[App] = []
        try:
            data = json.loads(self.custom_path.read_text(encoding="utf-8-sig"))
            for row in data.get("apps", []) if isinstance(data, dict) else []:
                name = str(row.get("name", "")).strip()
                if not name:
                    continue
                targets = [str(x) for x in ([row["path"]] if row.get("path") else []) + list(row.get("targets") or [])]
                apps.append(App(name, tuple(str(a) for a in row.get("aliases") or []), tuple(targets),
                                str(row.get("web", "")), custom=True))
        except (OSError, ValueError, AttributeError):
            apps = []
        self._custom_mtime, self._custom_size, self._custom = state[0], state[1], apps
        return apps

    def _rebuild_registry(self) -> None:
        by_alias: dict[str, App] = {}
        pool: list[tuple[str, App]] = []
        # custom entries first so they win over built-ins that share a name
        for app in [*self._load_custom(), *BUILTIN_APPS]:
            for al in (app.name, *app.aliases):
                n = norm(al)
                if not n:
                    continue
                if n not in by_alias:
                    by_alias[n] = app
                    pool.append((n, app))
                if compact(n) not in by_alias:
                    by_alias[compact(n)] = app
        self._by_alias, self._pool = by_alias, pool

    def registry(self) -> dict[str, App]:
        with self.lock:
            self._rebuild_registry()
            return dict(self._by_alias)

    # -- installed apps ---------------------------------------------------------------------
    def _read_cache(self) -> dict[str, tuple[str, str]]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if time.time() - float(data.get("time", 0)) < self.CACHE_HOURS * 3600:
                return {k: (v[0], v[1]) for k, v in data.get("store", {}).items()}
        except (OSError, ValueError, TypeError, IndexError):
            pass
        return {}

    def _write_cache(self, store: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"time": time.time(), "store": store}, ensure_ascii=False),
                                       encoding="utf-8")
        except OSError:
            pass

    def scan(self, fast_only: bool = False) -> None:
        """Reads the Start Menu, Desktop and registry (fast). The Store-app list (slow) runs in the background."""
        found: dict[str, tuple[str, str]] = {}
        for key in ("shortcuts", "apppaths"):
            try:
                for k, v in self.scanners[key]().items():
                    found.setdefault(k, v)
            except Exception:
                pass
        with self.lock:
            store = self._store_cache if self._store_cache is not None else self._read_cache()
            self._store_cache = store
            for k, v in store.items():
                found.setdefault(k, v)
            self.installed = found
            self._scanned_at = time.monotonic()
            need_store = not store and not self._store_started and not fast_only
            if need_store:
                self._store_started = True
        if need_store:
            threading.Thread(target=self._scan_store_bg, daemon=True, name="app-store-scan").start()

    def _scan_store_bg(self) -> None:
        try:
            store = self.scanners["store"]()
        except Exception:
            store = {}
        with self.lock:
            if store:
                self._store_cache = store
                for k, v in store.items():
                    self.installed.setdefault(k, v)
                self._write_cache(store)

    def ensure_scanned(self, max_age: float = 3600.0) -> None:
        with self.lock:
            fresh = self._scanned_at and (time.monotonic() - self._scanned_at) < max_age
        if not fresh:
            self.scan()

    def rescan_if_stale(self, min_gap: float = 30.0) -> bool:
        """Used after a miss: the app may have been installed a minute ago."""
        with self.lock:
            if self._scanned_at and (time.monotonic() - self._scanned_at) < min_gap:
                return False
        self.scan(fast_only=True)
        return True

    # -- lookups ----------------------------------------------------------------------------
    def _installed_for(self, app: App) -> tuple[str, str] | None:
        """Is the app (by any of its names) in the installed list? Used when its direct targets don't launch."""
        names = [norm(app.name), *(norm(a) for a in app.aliases)]
        for n in names:
            if n in self.installed and len(compact(n)) >= 3:
                return self.installed[n]
        label = norm(app.name)
        for key, val in self.installed.items():
            if len(label) >= 4 and (key == label or key.startswith(label + " ") or label in key.split()):
                return val
        return None

    def is_available(self, app: App) -> bool:
        """Is this app really on the PC (not just in my list)?"""
        for t in app.targets:
            kind, val = _kind_of(t)
            if kind == "file" and os.path.exists(val.split(" --")[0]):
                return True
            if kind == "exe":
                exe = val.split()[0]
                if shutil.which(exe) or norm(Path(exe).stem) in self.installed:
                    return True
            if kind == "uri" and not val.startswith(("ms-", "mailto")) and uri_registered(val):
                return True
        return self._installed_for(app) is not None

    def describe(self, query: str) -> str:
        """Honest answer to 'do I have X?': installed, only on the web, or unknown."""
        self.ensure_scanned()
        try:
            plan = self.plan(query)
        except Unsure as exc:
            return str(exc)
        if plan is None:
            return f"I couldn't find an app like '{query}' on this PC."
        app = next((a for a in [*self._custom, *BUILTIN_APPS] if a.name == plan.label), None)
        if plan.source == "installed" or (app is not None and self.is_available(app)):
            return f"Yes, {plan.label} is installed on this PC."
        if plan.web:
            return f"{plan.label} is not installed on this PC, but I can open it in your browser."
        return f"{plan.label} doesn't seem to be installed on this PC."

    def plan(self, raw) -> Plan | None:
        """Best way to open `raw`, or None. Raises Unsure when several apps fit about equally well."""
        self.ensure_scanned()
        with self.lock:
            self._rebuild_registry()
        q = norm(raw)
        if not q:
            return None
        q = self.extra_aliases.get(q, q)

        for attempt in range(2):
            hit = self._plan_once(q)
            if hit is not None:
                return hit
            if attempt == 0 and not self.rescan_if_stale():
                break
        return None

    def _app_plan(self, app: App, sc: float) -> Plan:
        tries = [_kind_of(t) for t in app.targets]
        inst = self._installed_for(app)
        if inst:
            tries.append(inst)
        if not tries and app.web:
            tries = [("url", app.web)]
        return Plan(app.name, tries, app.web, "custom" if app.custom else "registry", sc)

    def _plan_once(self, q: str) -> Plan | None:
        with self.lock:
            app = self._by_alias.get(q) or self._by_alias.get(compact(q))
            if app is not None:
                return self._app_plan(app, 1.0)
            inst = self.installed.get(q) or next((v for k, v in self.installed.items() if compact(k) == compact(q)), None)
            if inst is not None:
                return Plan(q.title(), [inst], "", "installed", 1.0)

            if q in _STOPWORDS:
                return None
            scored: dict[str, tuple[float, str, object]] = {}   # identity -> (score, label, payload)
            registry_names = {n for n, _a in self._pool}
            for n, a in self._pool:
                s = score(q, n) + 0.005
                ident = "app:" + a.name
                if s > scored.get(ident, (0,))[0]:
                    scored[ident] = (s, a.name, a)
            for n, val in self.installed.items():
                if n in registry_names:
                    continue
                s = score(q, n)
                ident = "inst:" + n
                if s > 0:
                    scored[ident] = (s, n.title(), val)
        ranked = sorted(scored.values(), key=lambda r: r[0], reverse=True)
        if not ranked or ranked[0][0] < ASK_SCORE:
            return None
        best = ranked[0]
        near = [r for r in ranked if r[0] >= best[0] - 0.03 and r[0] >= AUTO_SCORE]
        # several different apps that fit equally well ("adobe", "microsoft ...") -> ask which
        if best[0] < 0.97 and len(near) >= 2:
            raise Unsure(f"I found more than one match for '{q}': {self._say_list([r[1] for r in near[:4]])}. Which one?",
                         [r[1] for r in near[:4]])
        if best[0] < AUTO_SCORE:
            opts = [r[1] for r in ranked[:3] if r[0] >= ASK_SCORE]
            raise Unsure(f"I couldn't find '{q}'. Did you mean {self._say_list(opts, 'or')}?", opts)
        payload = best[2]
        if isinstance(payload, App):
            return self._app_plan(payload, best[0])
        return Plan(best[1], [payload], "", "installed", best[0])

    @staticmethod
    def _say_list(items: list[str], joiner: str = "or") -> str:
        items = list(dict.fromkeys(items))
        if len(items) <= 1:
            return "".join(items)
        return ", ".join(items[:-1]) + f" {joiner} " + items[-1]

    def suggestions(self, raw: str, n: int = 3) -> list[str]:
        q = norm(raw)
        with self.lock:
            names = [(nm, a.name) for nm, a in self._pool] + [(k, k.title()) for k in self.installed]
        best: dict[str, float] = {}
        for nm, label in names:
            s = score(q, nm)
            if s >= ASK_SCORE and s > best.get(label, 0):
                best[label] = s
        return [k for k, _v in sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:n]]

    def search(self, query: str = "", limit: int = 25) -> list[str]:
        """Names of apps on this PC / known to Nova that fit the query (all installed ones if empty)."""
        self.ensure_scanned()
        q = norm(query)
        with self.lock:
            names = {k.title() for k in self.installed}
            candidates = [a for a in [*self._custom, *BUILTIN_APPS] if a.targets]
        apps = {a.name for a in candidates if self.is_available(a)}
        if not q:
            return sorted(names, key=str.lower)[:limit]
        hits = [(0.9 if q in norm(n) else score(q, norm(n)), n) for n in (*names, *apps)]
        hits = sorted({(s, n) for s, n in hits if s >= AUTO_SCORE}, key=lambda t: (-t[0], t[1].lower()))
        return list(dict.fromkeys(n for _s, n in hits))[:limit]

    # -- launching --------------------------------------------------------------------------
    def launch(self, plan: Plan, startfile=None, popen=None, open_url=None, uri_ok=None) -> str:
        """Tries each way of starting the app. Returns a spoken-style message or raises ToolError."""
        startfile = startfile or getattr(os, "startfile", None)
        popen = popen or subprocess.Popen
        open_url = open_url or webbrowser.open
        uri_ok = uri_ok or uri_registered
        if startfile is None:
            raise ToolError("Opening apps only works on Windows.")
        last = ""
        for kind, value in plan.tries:
            if kind == "uri" and not uri_ok(value):
                continue                 # no app handles this link type: skip it (Windows would show an error box)
            try:
                if kind == "url":
                    open_url(value)
                    return f"Opening {plan.label}." if plan.label.lower().startswith("your ") \
                        else f"Opening {plan.label} in your browser."
                if kind == "uwp":
                    popen(["explorer.exe", f"shell:AppsFolder\\{value}"])
                elif kind == "file":
                    if not os.path.exists(value.split(" --")[0]):
                        raise FileNotFoundError(value)
                    if " --" in value:                               # e.g. Update.exe --processStart X.exe
                        exe, _, rest = value.partition(" --")
                        popen([exe, *("--" + rest).split()])
                    else:
                        startfile(value)
                else:                                                # exe name or URI: Windows finds it
                    if " " in value and kind == "exe":               # "wt.exe powershell"
                        exe, _, rest = value.partition(" ")
                        popen([exe, *rest.split()])
                    else:
                        startfile(value)
                return f"Opening {plan.label}."
            except (OSError, FileNotFoundError, ValueError) as exc:
                last = str(exc)
                continue
        if plan.web:
            try:
                open_url(plan.web)
                return f"{plan.label} isn't installed on this PC, so I opened it in your browser."
            except Exception:
                pass
        raise ToolError(f"{plan.label} doesn't seem to be installed on this PC.")


# ---------------------------------------------------------------------------------------------- shared
_catalog: AppCatalog | None = None
_catalog_lock = threading.Lock()


def get_catalog(cfg: dict | None = None) -> AppCatalog:
    """One shared catalog; `cfg` is the 'tools' section of config.json (for app_aliases)."""
    global _catalog
    with _catalog_lock:
        if _catalog is None:
            _catalog = AppCatalog((cfg or {}).get("app_aliases"))
        return _catalog


def open_app(name: str, cfg: dict | None = None) -> str:
    """Open an app by any reasonable name. Raises ToolError (or Unsure) with a helpful message."""
    cat = get_catalog(cfg)
    plan = cat.plan(name)
    if plan is None:
        tips = cat.suggestions(name)
        extra = f" Did you mean {AppCatalog._say_list(tips, 'or')}?" if tips else ""
        raise ToolError(f"I couldn't find an app called '{str(name).strip()}'.{extra}")
    return cat.launch(plan)


def app_exists(name: str, cfg: dict | None = None) -> bool:
    """Can open_app find this app? (Used when a skill is created.) A web version counts."""
    try:
        plan = get_catalog(cfg).plan(name)
    except Unsure:
        return True              # several apps fit: it exists, Nova will ask which when it runs
    except ToolError:
        return False
    return plan is not None


def knows_exactly(name: str, cfg: dict | None = None) -> bool:
    """True only when the whole name is a known app (not a guess): used to tell 'chrome and notepad' from one app."""
    try:
        plan = get_catalog(cfg).plan(name)
    except ToolError:
        return False
    return plan is not None and plan.score >= 0.97


def warm_up(cfg: dict | None = None) -> None:
    """Start reading the installed apps in the background so the first 'open ...' is instant."""
    cat = get_catalog(cfg)
    threading.Thread(target=cat.ensure_scanned, daemon=True, name="app-scan").start()
