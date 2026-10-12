#!/usr/bin/env bash
set -e

echo "========================================================"
echo "              AlterChat - Automated Setup               "
echo "========================================================"
echo ""

# 1. Detect Python command
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
else
    echo "[ERROR] Python is not installed or not found in PATH."
    echo "Please install Python 3.10+ (e.g., via brew or package manager)."
    exit 1
fi

echo "[1/3] Checking Python environment..."
$PYTHON_CMD --version

# 2. Virtual environment setup
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment in .venv..."
    $PYTHON_CMD -m venv .venv || echo "[WARNING] Could not create .venv. Installing directly."
fi

if [ -f ".venv/bin/activate" ]; then
    echo "Activating virtual environment (.venv)..."
    source .venv/bin/activate
fi

echo "Upgrading pip..."
pip install --upgrade pip --quiet

# 3. Install dependencies
echo ""
echo "[2/3] Installing dependencies from requirements.txt..."
pip install -r requirements.txt

# 4. Folder structure & .env
echo ""
echo "[3/3] Checking directories and .env configuration..."
mkdir -p data/avatars data/history

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "[ACTION REQUIRED] Created .env template. Please edit .env with your CUSTOM_API_KEY."
    fi
else
    echo ".env file detected."
fi

echo ""
echo "========================================================"
echo "                   Setup Complete!                      "
echo "========================================================"
echo ""
echo "Launch AlterChat with:"
echo "  source .venv/bin/activate && python main.py"
echo ""
