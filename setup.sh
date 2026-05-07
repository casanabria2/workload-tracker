#!/bin/bash
# Setup Workload Tracker on a new computer
# Run this after cloning the repo

set -e

# Install Homebrew dependencies
echo "Installing Homebrew dependencies..."
brew install python tmux gh
brew install --cask hammerspoon iterm2

# Create and activate venv, install Python dependencies
echo "Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/WorkloadTracker"

# Create ~/WorkloadTracker symlink to iCloud folder
if [ -L "$HOME/WorkloadTracker" ]; then
    echo "~/WorkloadTracker symlink already exists"
elif [ -e "$HOME/WorkloadTracker" ]; then
    echo "ERROR: ~/WorkloadTracker exists but is not a symlink"
    exit 1
else
    ln -s "$ICLOUD_DIR" "$HOME/WorkloadTracker"
    echo "Created ~/WorkloadTracker -> $ICLOUD_DIR"
fi

# Create ~/.workload_tracker.json symlink to data file in iCloud
if [ -L "$HOME/.workload_tracker.json" ]; then
    echo "~/.workload_tracker.json symlink already exists"
elif [ -e "$HOME/.workload_tracker.json" ]; then
    echo "ERROR: ~/.workload_tracker.json exists but is not a symlink"
    echo "       Back it up and remove it first if you want to use the shared data"
    exit 1
else
    ln -s "$HOME/WorkloadTracker/.workload_tracker.json" "$HOME/.workload_tracker.json"
    echo "Created ~/.workload_tracker.json -> ~/WorkloadTracker/.workload_tracker.json"
fi

# Set up 'wt' command: symlink wrapper into venv/bin (which should be in PATH)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ln -sf "${SCRIPT_DIR}/wt" "${SCRIPT_DIR}/venv/bin/wt"
echo "Linked 'wt' command to venv/bin/"

# Set up zsh autocompletion
ZSH_SITE_FUNCTIONS="/opt/homebrew/share/zsh/site-functions"
if [ -d "$ZSH_SITE_FUNCTIONS" ]; then
    ln -sf "${SCRIPT_DIR}/_wt" "${ZSH_SITE_FUNCTIONS}/_wt"
    echo "Linked zsh completions to ${ZSH_SITE_FUNCTIONS}/_wt"
    echo "Run 'rm -f ~/.zcompdump* && exec zsh' to activate completions"
fi

echo ""
echo "Done. To run the workload tracker:"
echo "  source venv/bin/activate"
echo "  python tracker.py"
echo ""
echo "CLI (with venv in PATH):"
echo "  wt list"
echo "  wt sprint"
