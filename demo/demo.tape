# satdeploy demo — single pane walkthrough
#
# Flow:
#   1. demo start          (boot the simulated satellite)
#   2. demo shell          (hop onto the sat, run test_app → v1)
#   3. push test_app       (deploy v2 from the ground station)
#   4. demo shell          (hop back on, run test_app → v2 with new feature)
#
# Render:
#   ROD_BROWSER_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" vhs demo/demo.tape

Output demo/demo.gif

Set Shell        "zsh"
Set FontSize     14
Set Width        1400
Set Height       900
Set Theme        "Catppuccin Mocha"
Set TypingSpeed  45ms
Set Padding      20

# ── Pre-clean previous state ───────────────────────────────────
Type "satdeploy demo stop 2>/dev/null >/dev/null; clear"
Sleep 300ms
Enter
Sleep 2s

# ── Scene 1: launch the sim ────────────────────────────────────
Type "satdeploy demo start"
Sleep 300ms
Enter
Sleep 8s

# ── Scene 2: hop onto the satellite, run test_app (v1) ────────
Type "satdeploy demo shell"
Sleep 300ms
Enter
Sleep 3s

Type "/opt/demo/bin/test_app"
Sleep 300ms
Enter
Sleep 2500ms

Type "exit"
Sleep 300ms
Enter
Sleep 1500ms

# ── Scene 3: ground station pushes v2 ──────────────────────────
Type "satdeploy status"
Sleep 300ms
Enter
Sleep 2500ms

Type "satdeploy push test_app"
Sleep 300ms
Enter
Sleep 5s

Type "satdeploy status"
Sleep 300ms
Enter
Sleep 2500ms

# ── Scene 4: hop back on, confirm v2 landed ────────────────────
Type "satdeploy demo shell"
Sleep 300ms
Enter
Sleep 3s

Type "/opt/demo/bin/test_app"
Sleep 300ms
Enter
Sleep 2500ms

Type "exit"
Sleep 300ms
Enter
Sleep 1500ms

Sleep 2s
