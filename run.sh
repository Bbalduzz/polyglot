#!/bin/bash

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Starting setup and execution...${NC}"

# 1. Check Python installation
echo -e "${YELLOW}Checking Python installation...${NC}"
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version 2>&1)
    echo -e "${GREEN}Python found: $PYTHON_VERSION${NC}"
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_VERSION=$(python --version 2>&1)
    echo -e "${GREEN}Python found: $PYTHON_VERSION${NC}"
    PYTHON_CMD="python"
else
    echo -e "${RED}Python not found. Installing Python...${NC}"
    
    # 2. Download and install Python if not present
    # Check if Homebrew is installed first
    if ! command -v brew &> /dev/null; then
        echo -e "${YELLOW}Homebrew not found. Installing Homebrew first...${NC}"
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        
        # Add Homebrew to PATH for current session
        if [[ $(uname -m) == "arm64" ]]; then
            export PATH="/opt/homebrew/bin:$PATH"
        else
            export PATH="/usr/local/bin:$PATH"
        fi
    fi
    
    echo -e "${YELLOW}Installing Python via Homebrew...${NC}"
    brew install python
    
    if command -v python3 &> /dev/null; then
        PYTHON_CMD="python3"
        echo -e "${GREEN}Python installed successfully!${NC}"
    else
        echo -e "${RED}Failed to install Python. Exiting.${NC}"
        exit 1
    fi
fi

# 3. Download/install requirements
echo -e "${YELLOW}Installing Python requirements...${NC}"
if [ -f "requirements.txt" ]; then
    $PYTHON_CMD -m pip install --upgrade pip
    $PYTHON_CMD -m pip install -r requirements.txt
    echo -e "${GREEN}Requirements installed successfully!${NC}"
else
    echo -e "${YELLOW}No requirements.txt found. Skipping Python package installation.${NC}"
fi

# 4. Use brew to install tesseract-lang
echo -e "${YELLOW}Installing Tesseract with language packs...${NC}"
if ! command -v brew &> /dev/null; then
    echo -e "${RED}Homebrew is required but not found. Please install Homebrew first.${NC}"
    exit 1
fi

# Install tesseract and common language packs
brew install tesseract tesseract-lang

if command -v tesseract &> /dev/null; then
    echo -e "${GREEN}Tesseract installed successfully!${NC}"
    echo -e "${GREEN}Available languages:${NC}"
    tesseract --list-langs
else
    echo -e "${RED}Failed to install Tesseract. Exiting.${NC}"
    exit 1
fi

# 5. Run Python script
echo -e "${YELLOW}Running Python script...${NC}"
if [ -f "src/main.py" ]; then
    $PYTHON_CMD src/main.py
    echo -e "${GREEN}Script execution completed!${NC}"
else
    echo -e "${RED}Script src/main.py not found!${NC}"
    exit 1
fi

echo -e "${GREEN}All tasks completed successfully!${NC}"