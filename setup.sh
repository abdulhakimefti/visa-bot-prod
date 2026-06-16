#!/usr/bin/env bash
# setup.sh — One-command setup for the Visa Bot
set -e

echo "=================================================="
echo "   VISA APPOINTMENT BOT — Setup"
echo "=================================================="

# 1. Python version check
python_version=$(python3 --version 2>&1)
echo "✓ $python_version detected"

# 2. Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate
echo "✓ Virtual environment activated"

# 3. Install dependencies
echo "Installing Python packages..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "✓ Packages installed"

# 4. Install Playwright browsers
echo "Installing Playwright browsers (Chromium)..."
playwright install chromium
playwright install-deps chromium
echo "✓ Playwright ready"

# 5. Copy .env template if not present
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  .env file created from template."
    echo "    Please edit .env with your credentials before running."
else
    echo "✓ .env already exists"
fi

# 6. Create directory structure
mkdir -p logs screenshots config core bot
touch config/__init__.py core/__init__.py bot/__init__.py

echo ""
echo "=================================================="
echo "   Setup complete!"
echo "=================================================="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your visa portal credentials"
echo "  2. Edit core/scraper.py to match your portal's HTML selectors"
echo "  3. Run: source .venv/bin/activate && python agent.py"
echo ""
