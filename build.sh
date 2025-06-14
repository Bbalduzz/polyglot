#!/bin/bash

# Nuitka build script for Polyglot app
# This script compiles the Python application into a standalone macOS app bundle

python3.12 -m nuitka \
    --standalone \
    --macos-create-app-bundle \
    --macos-app-icon=src/assets/icon.png \
    --output-dir=dist-standalone \
    --follow-imports \
    --assume-yes-for-downloads \
    --include-data-dir=src/assets=assets \
    --include-data-dir=storage=storage \
    --include-data-dir=src/tesseract=tesseract \
    --macos-app-mode="ui-element" \
    --macos-signed-app-name="it.edblcc.polyglot" \
    --include-package-data=flet \
    --product-name="Polyglot" \
    --static-libpython=no \
    src/main.py

echo "Build completed. Check the dist/ directory for your app bundle."