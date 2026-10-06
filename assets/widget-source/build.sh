#!/bin/zsh
set -eu
cd "$(dirname "$0")"
APP="$PWD/Codex用量浮窗.app"
if [[ -e "$APP" || -L "$APP" ]]; then
  print -u2 "目标应用已存在，请在新的运行目录构建，或先确认如何保留旧版"
  exit 1
fi
command -v swiftc >/dev/null
command -v python3 >/dev/null
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc src/Widget.swift src/ChatPageVisibility.swift -o "$APP/Contents/MacOS/UsageWidget" -framework Cocoa -framework SwiftUI
python3 -c 'import sys; print(sys.executable)' > "$APP/Contents/Resources/python-runtime.txt"
cp src/radar.py "$APP/Contents/Resources/radar.py"
cp src/backend.py "$APP/Contents/Resources/backend.py"
cp src/reset_cards.py "$APP/Contents/Resources/reset_cards.py"
# Do not redistribute extracted vendor assets; reuse the recipient's local icon if present
for widget_icon in /Applications/ChatGPT.app/Contents/Resources/chatgptTemplate@2x.png /Applications/Codex.app/Contents/Resources/chatgptTemplate@2x.png; do
  if [[ -f "$widget_icon" ]]; then
    cp "$widget_icon" "$APP/Contents/Resources/OpenAIModel.png"
    break
  fi
done
cp src/hierarchy.py "$APP/Contents/Resources/hierarchy.py"
cp src/panel.py "$APP/Contents/Resources/panel.py"
mkdir -p "$APP/Contents/Resources/web"
cp src/web/panel.html src/web/panel.css src/web/panel.js "$APP/Contents/Resources/web/"
cp src/Info.plist "$APP/Contents/Info.plist"
