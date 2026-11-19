#!/usr/bin/env bash
set -euo pipefail

# One-step SteamOS / Steam Deck installer for open_ineb Beni FPV Controller.
# Installs `uv` (if missing), warms up the Python dependencies, and creates a
# Desktop/Steam launcher entry that can be added as a Non-Steam Game.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$HOME/.local/bin/open-ineb-deck"
DESKTOP="$HOME/.local/share/applications/open-ineb-deck.desktop"

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"

if ! command -v uv >/dev/null 2>&1; then
  echo "[open_ineb] Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "[open_ineb] Pre-warming Python environment..."
uv run "$SCRIPT_DIR/run.py" --help >/dev/null

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
export PATH="\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH"
exec uv run "$SCRIPT_DIR/run.py" --fullscreen "\$@"
EOF
chmod +x "$LAUNCHER"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Beni FPV Controller (open_ineb)
Comment=Native Steam Deck FPV controller, HUD, TTS voice, and media gallery for Mondo Robotics Beni
Exec=$LAUNCHER
Terminal=false
Categories=Game;Utility;
EOF

echo ""
echo "=============================================================="
echo "  open_ineb Steam Deck Controller installed!"
echo "=============================================================="
echo "  Launcher : $LAUNCHER"
echo "  Desktop  : $DESKTOP"
echo ""
echo "  Next steps in Steam Deck Desktop Mode:"
echo "    1. Open Steam -> Games -> Add a Non-Steam Game to My Library..."
echo "    2. Select 'Beni FPV Controller (open_ineb)' (or browse to $LAUNCHER)"
echo "    3. Switch back to Gaming Mode & launch! (Pass --mock to test without Beni)"
echo "=============================================================="
