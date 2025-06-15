import flet as ft
import mss
import threading
import time
from typing import Optional, Dict, Any, List, Tuple
import multiprocessing
import tempfile
import json
import os
import imageio.v2 as imageio

# monkey patch distutils
import sys
from types import ModuleType

# Create a mock distutils module, dependecy of argostranslate
distutils_module = ModuleType('distutils')
distutils_util_module = ModuleType('distutils.util')

def strtobool(val):
    """Convert a string representation of truth to True or False."""
    val = val.lower()
    if val in ('y', 'yes', 't', 'true', 'on', '1'):
        return True
    elif val in ('n', 'no', 'f', 'false', 'off', '0'):
        return False
    else:
        raise ValueError(f"invalid truth value {val!r}")

distutils_util_module.strtobool = strtobool
distutils_module.util = distutils_util_module

sys.modules['distutils'] = distutils_module
sys.modules['distutils.util'] = distutils_util_module

import argostranslate.package
import argostranslate.translate
from dataclasses import dataclass
from abc import ABC, abstractmethod
import cv2
import numpy as np

import traceback
from ui.components import (
    FactoryButton, FactorySecondaryButton, FactoryTextField, FactoryCheckBox,
    FactoryDropdown, FactoryDropdownOption, FactoryCard, FactoryField,
    colors_map
)
import platform
try:
    from ocrmac import ocrmac
    OCRMAC_AVAILABLE = True
except ImportError:
    OCRMAC_AVAILABLE = False


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class Region:
    x: int
    y: int
    width: int
    height: int
    
    def to_dict(self):
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}
    
    @classmethod
    def from_dict(cls, data: dict):
        return cls(data["x"], data["y"], data["width"], data["height"])

@dataclass
class SubtitleData:
    original_text: str
    translated_text: str
    is_translated: bool
    source_language: str
    target_language: str
    confidence: float = 1.0
    timestamp: float = 0.0
    
    def __post_init__(self):
        self.timestamp = time.time()

@dataclass
class TranslationSettings:
    enabled: bool = False
    source_language: str = "en"
    target_language: str = "es"
    
@dataclass
class AppState:
    region: Optional[Region] = None
    translation_settings: TranslationSettings = None
    current_subtitle: Optional[SubtitleData] = None
    is_running: bool = False
    status_message: str = "Ready"
    status_color: str = "orange"
    
    def __post_init__(self):
        if self.translation_settings is None:
            self.translation_settings = TranslationSettings()

# ============================================================================
# OBSERVERS PATTERN (Keep existing)
# ============================================================================

class Observer(ABC):
    @abstractmethod
    def update(self, event_type: str, data: Any):
        pass

class Observable:
    def __init__(self):
        self._observers = []
    
    def add_observer(self, observer: Observer):
        self._observers.append(observer)
    
    def remove_observer(self, observer: Observer):
        if observer in self._observers:
            self._observers.remove(observer)
    
    def notify_observers(self, event_type: str, data: Any = None):
        for observer in self._observers:
            try:
                observer.update(event_type, data)
            except Exception as e:
                print(f"Observer error: {e}")

# ============================================================================
# CORE SERVICES (Keep existing services unchanged)
# ============================================================================

class RegionSelectionService:
    def __init__(self):
        self.temp_file = os.path.join(tempfile.gettempdir(), 'ocr_region_selection.json')
    
    def save_region(self, region: Optional[Region]):
        try:
            with open(self.temp_file, 'w') as f:
                json.dump(region.to_dict() if region else None, f)
        except Exception as e:
            print(f"Error saving region: {e}")
    
    def load_region(self) -> Optional[Region]:
        try:
            if os.path.exists(self.temp_file):
                with open(self.temp_file, 'r') as f:
                    data = json.load(f)
                    return Region.from_dict(data) if data else None
        except Exception as e:
            print(f"Error loading region: {e}")
        return None
    
    def clear_region(self):
        try:
            if os.path.exists(self.temp_file):
                os.remove(self.temp_file)
        except Exception as e:
            print(f"Error clearing region: {e}")

class TranslationService:
    def __init__(self):
        self.available_packages = []
        self.installed_packages = []
        self._load_packages()
    
    def _load_packages(self):
        try:
            argostranslate.package.update_package_index()
            self.available_packages = argostranslate.package.get_available_packages()
            self.installed_packages = argostranslate.package.get_installed_packages()
            print(f"Loaded {len(self.installed_packages)} installed translation packages")
                
        except Exception as e:
            print(f"Error loading translation packages: {e}")
    
    def is_package_installed(self, from_lang: str, to_lang: str) -> bool:
        """Check if direct translation package is installed"""
        for package in self.installed_packages:
            if package.from_code == from_lang and package.to_code == to_lang:
                return True
        return False
    
    def is_package_available(self, from_lang: str, to_lang: str) -> bool:
        """Check if direct translation package is available for download"""
        for package in self.available_packages:
            if package.from_code == from_lang and package.to_code == to_lang:
                return True
        return False
    
    def find_translation_path(self, from_lang: str, to_lang: str) -> list:
        """
        Find shortest translation path using installed packages.
        Always tries English as intermediate language first since it's most common.
        """
        if from_lang == to_lang:
            return [from_lang]
        
        # Check for direct translation first
        if self.is_package_installed(from_lang, to_lang):
            return [from_lang, to_lang]
        
        # For non-English languages, try via English first (most common path)
        if from_lang != 'en' and to_lang != 'en':
            if (self.is_package_installed(from_lang, 'en') and 
                self.is_package_installed('en', to_lang)):
                return [from_lang, 'en', to_lang]
        
        # If English path doesn't work, do full BFS
        return self._bfs_translation_path(from_lang, to_lang)
    
    def find_available_translation_path(self, from_lang: str, to_lang: str) -> list:
        """
        Find shortest translation path using available packages.
        Always tries English as intermediate language first.
        """
        if from_lang == to_lang:
            return [from_lang]
        
        # Check for direct translation first
        if self.is_package_available(from_lang, to_lang):
            return [from_lang, to_lang]
        
        # For non-English languages, try via English first
        if from_lang != 'en' and to_lang != 'en':
            if (self.is_package_available(from_lang, 'en') and 
                self.is_package_available('en', to_lang)):
                return [from_lang, 'en', to_lang]
        
        # If English path doesn't work, do full BFS on available packages
        return self._bfs_available_translation_path(from_lang, to_lang)
    
    def _bfs_translation_path(self, from_lang: str, to_lang: str) -> list:
        """BFS for installed packages"""
        from collections import deque
        
        # Build graph of installed translations
        available_translations = {}
        for package in self.installed_packages:
            if package.from_code not in available_translations:
                available_translations[package.from_code] = []
            available_translations[package.from_code].append(package.to_code)
        
        # BFS
        queue = deque([(from_lang, [from_lang])])
        visited = {from_lang}
        
        while queue:
            current_lang, path = queue.popleft()
            
            if current_lang == to_lang:
                return path
            
            if current_lang in available_translations and len(path) < 4:
                for next_lang in available_translations[current_lang]:
                    if next_lang not in visited:
                        visited.add(next_lang)
                        queue.append((next_lang, path + [next_lang]))
        
        return []
    
    def _bfs_available_translation_path(self, from_lang: str, to_lang: str) -> list:
        """BFS for available packages"""
        from collections import deque
        
        # Build graph of available translations
        available_translations = {}
        for package in self.available_packages:
            if package.from_code not in available_translations:
                available_translations[package.from_code] = []
            available_translations[package.from_code].append(package.to_code)
        
        # BFS
        queue = deque([(from_lang, [from_lang])])
        visited = {from_lang}
        
        while queue:
            current_lang, path = queue.popleft()
            
            if current_lang == to_lang:
                return path
            
            if current_lang in available_translations and len(path) < 4:
                for next_lang in available_translations[current_lang]:
                    if next_lang not in visited:
                        visited.add(next_lang)
                        queue.append((next_lang, path + [next_lang]))
        
        return []
    
    def can_translate(self, from_lang: str, to_lang: str) -> bool:
        """Check if translation is possible with currently installed packages"""
        path = self.find_translation_path(from_lang, to_lang)
        return len(path) >= 2
    
    def can_translate_if_installed(self, from_lang: str, to_lang: str) -> bool:
        """Check if translation would be possible if required packages were installed"""
        path = self.find_available_translation_path(from_lang, to_lang)
        return len(path) >= 2
    
    def get_required_packages(self, from_lang: str, to_lang: str) -> list:
        """Get list of (source, target) packages needed for translation"""
        path = self.find_available_translation_path(from_lang, to_lang)
        if len(path) < 2:
            return []
        
        required_packages = []
        for i in range(len(path) - 1):
            source = path[i]
            target = path[i + 1]
            if not self.is_package_installed(source, target):
                required_packages.append((source, target))
        
        return required_packages
    
    def install_package(self, from_lang: str, to_lang: str) -> bool:
        """Install a specific translation package"""
        try:
            package_to_install = None
            for package in self.available_packages:
                if package.from_code == from_lang and package.to_code == to_lang:
                    package_to_install = package
                    break
            
            if package_to_install:
                print(f"Installing {from_lang} → {to_lang} package...")
                argostranslate.package.install_from_path(package_to_install.download())
                self.installed_packages = argostranslate.package.get_installed_packages()
                print(f"Successfully installed {from_lang} → {to_lang}")
                return True
            else:
                print(f"Package {from_lang} → {to_lang} not found in available packages")
                return False
        except Exception as e:
            print(f"Error installing package {from_lang} → {to_lang}: {e}")
            return False
    
    def install_translation_path(self, from_lang: str, to_lang: str) -> bool:
        """Install all packages needed for translation path"""
        required_packages = self.get_required_packages(from_lang, to_lang)
        
        if not required_packages:
            if self.can_translate(from_lang, to_lang):
                print(f"Translation path {from_lang} → {to_lang} already available")
                return True
            else:
                print(f"No translation path available for {from_lang} → {to_lang}")
                return False
        
        print(f"Installing translation path {from_lang} → {to_lang}")
        print(f"Required packages: {required_packages}")
        
        success_count = 0
        for source, target in required_packages:
            if self.install_package(source, target):
                success_count += 1
            else:
                print(f"Failed to install {source} → {target}")
        
        success = success_count == len(required_packages)
        if success:
            print(f"Successfully installed all packages for {from_lang} → {to_lang}")
        else:
            print(f"Failed to install {len(required_packages) - success_count} packages")
        
        return success
    
    def translate(self, text: str, from_lang: str, to_lang: str) -> str:
        """Translate text using direct or pivot translation"""
        try:
            if not text.strip() or from_lang == to_lang:
                return text
            
            path = self.find_translation_path(from_lang, to_lang)
            if len(path) < 2:
                print(f"No translation path available from {from_lang} to {to_lang}")
                return text
            
            if len(path) == 2:
                # Direct translation
                translated = argostranslate.translate.translate(text, from_lang, to_lang)
                print(f"Direct translation: '{text}' ({from_lang}) → '{translated}' ({to_lang})")
                return translated
            else:
                # Pivot translation
                current_text = text
                print(f"Using pivot translation: {' → '.join(path)}")
                
                for i in range(len(path) - 1):
                    source = path[i]
                    target = path[i + 1]
                    prev_text = current_text
                    current_text = argostranslate.translate.translate(current_text, source, target)
                    print(f"Step {i+1}: '{prev_text}' ({source}) → '{current_text}' ({target})")
                
                print(f"Final result: '{text}' ({from_lang}) → '{current_text}' ({to_lang})")
                return current_text
            
        except Exception as e:
            print(f"Translation error: {e}")
            return text

class OCRService:
    def __init__(self):
        self.sct = mss.mss()
        self.temp_image_path = os.path.join(tempfile.gettempdir(), 'ocr_screenshot-polyglot.png')
        
        # Check if we're on macOS and ocrmac is available
        if not OCRMAC_AVAILABLE or platform.system() != "Darwin":
            raise RuntimeError("ocrmac is only available on macOS. Please install with: pip install ocrmac")
        
        print("Using ocrmac (Apple Vision Framework) for OCR")
        self._setup_ocrmac()
        
        # Current language setting
        self.current_language = 'en'
    
    def _setup_ocrmac(self):
        """Setup ocrmac with language mappings"""
        # Map common language codes to IANA language tags for ocrmac
        self.language_codes = {
            'en': 'en-US',      # English
            'es': 'es-ES',      # Spanish  
            'fr': 'fr-FR',      # French
            'de': 'de-DE',      # German
            'it': 'it-IT',      # Italian
            'pt': 'pt-PT',      # Portuguese
            'ru': 'ru-RU',      # Russian
            'ja': 'ja-JP',      # Japanese
            'ko': 'ko-KR',      # Korean
            'zh': 'zh-Hans',    # Chinese (Simplified)
            'ar': 'ar-SA',      # Arabic
            'hi': 'hi-IN',      # Hindi
            'nl': 'nl-NL',      # Dutch
            'sv': 'sv-SE',      # Swedish
            'da': 'da-DK',      # Danish
            'no': 'no-NO',      # Norwegian
            'fi': 'fi-FI',      # Finnish
            'pl': 'pl-PL',      # Polish
            'cs': 'cs-CZ',      # Czech
            'sk': 'sk-SK',      # Slovak
        }
        
        # OCR settings
        self.recognition_level = 'accurate'  # 'fast' or 'accurate'
        self.use_livetext = True  # Try LiveText first (macOS Sonoma+)
    
    def set_language(self, language_code: str):
        """Set the OCR language"""
        self.current_language = self.language_codes.get(language_code)
        print(f"OCR language set to: {language_code} -> {self.current_language}")
    
    def capture_and_recognize(self, region: Region) -> str:
        try:
            # Capture screen region
            monitor_area = {
                "top": region.y,
                "left": region.x,
                "width": region.width,
                "height": region.height
            }
            screenshot = self.sct.grab(monitor_area)
            
            # Convert screenshot to numpy array directly
            img_array = np.frombuffer(screenshot.bgra, dtype=np.uint8)
            img_array = img_array.reshape((screenshot.height, screenshot.width, 4))
            
            # Convert BGRA to RGB (remove alpha channel and swap B/R channels)
            img_rgb = img_array[:, :, [2, 1, 0]]  # BGRA -> RGB
            imageio.imwrite(self.temp_image_path, img_rgb)
            print(self.current_language)
            # Try LiveText first (more accurate on newer macOS)
            if self.use_livetext:
                try:
                    annotations = ocrmac.livetext_from_image(
                        self.temp_image_path,
                        language_preference=[self.current_language]
                    )
                    
                    # Extract text from annotations
                    if annotations:
                        texts = [annotation[0] for annotation in annotations if annotation[0].strip()]
                        result = ' '.join(texts)
                        if result.strip():
                            return self._clean_ocr_text(result)
                except Exception as e:
                    print(f"LiveText failed, falling back to Vision Framework: {e}")
            
            # Fallback to regular Vision Framework
            ocr_instance = ocrmac.OCR(
                self.temp_image_path,
                recognition_level=self.recognition_level,
                language_preference=[self.current_language]
            )
            
            annotations = ocr_instance.recognize()
            
            if annotations:
                # Extract text from annotations (text, confidence, bounding_box)
                texts = [annotation[0] for annotation in annotations if annotation[0].strip()]
                result = ' '.join(texts)
                return self._clean_ocr_text(result)
            
            return ""
            
        except Exception as e:
            print(f"OCR error: {e}")
            return ""
    
    def _clean_ocr_text(self, text: str) -> str:
        """Clean up OCR output text (language-agnostic)"""
        if not text:
            return ""
        
        # Remove extra whitespace and normalize newlines
        lines = text.split('\n')
        cleaned_lines = []
        
        for line in lines:
            # Remove leading/trailing whitespace
            line = line.strip()
            if line:  # Only keep non-empty lines
                # Normalize internal whitespace
                line = ' '.join(line.split())
                cleaned_lines.append(line)
        
        # Join lines with spaces for subtitle-like output
        result = ' '.join(cleaned_lines)
        
        # Only return if we have substantial text (at least 1 character for non-Latin scripts)
        return result if len(result.strip()) > 0 else ""
    
    def cleanup(self):
        try:
            if os.path.exists(self.temp_image_path):
                os.remove(self.temp_image_path)
        except:
            pass

# ============================================================================
# BUSINESS LOGIC CONTROLLER (Keep existing controller unchanged)
# ============================================================================

class OCRController(Observable):
    def __init__(self):
        super().__init__()
        self.state = AppState()
        
        # Services
        self.region_service = RegionSelectionService()
        self.translation_service = TranslationService()
        self.ocr_service = OCRService()
        
        # Runtime state
        self.is_running = False
        self.capture_thread: Optional[threading.Thread] = None
        self.last_processed_text = ""
        
    def set_region(self, region: Region):
        """Set the capture region"""
        self.state.region = region
        self.notify_observers("region_changed", region)
        print(f"Region set: {region}")
    
    def set_translation_settings(self, settings: TranslationSettings):
        """Update translation settings"""
        self.state.translation_settings = settings
        
        # Update OCR language to match source language
        self.ocr_service.set_language(settings.source_language)
        
        self.notify_observers("translation_settings_changed", settings)
        print(f"Translation settings: {settings}")
    
    def check_translation_package(self) -> bool:
        """Check if current translation package is available"""
        settings = self.state.translation_settings
        if not settings.enabled:
            return True
        
        return self.translation_service.can_translate(
            settings.source_language, 
            settings.target_language
        )
        
    def install_translation_package(self) -> bool:
        """Install required translation packages for pivot translation"""
        settings = self.state.translation_settings
        return self.translation_service.install_translation_path(
            settings.source_language,
            settings.target_language
        )
    
    def start_capture(self):
        """Start OCR capture process"""
        if self.is_running or not self.state.region:
            return
        
        if not self.check_translation_package():
            self._update_status("Translation package required", "orange")
            return
        
        self.is_running = True
        self.state.is_running = True
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.capture_thread.start()
        
        self._update_status("Running OCR and translation...", "green")
        self.notify_observers("capture_started", None)
    
    def stop_capture(self):
        """Stop OCR capture process"""
        self.is_running = False
        self.state.is_running = False
        
        if self.capture_thread:
            self.capture_thread.join(timeout=2)
        
        self._update_status("Stopped", "orange")
        self.notify_observers("capture_stopped", None)
    
    def _capture_loop(self):
        """Main capture and processing loop"""
        while self.is_running and self.state.region:
            try:
                # Capture and OCR
                raw_text = self.ocr_service.capture_and_recognize(self.state.region)
                
                if raw_text and raw_text != self.last_processed_text:
                    self.last_processed_text = raw_text
                    
                    # Process translation
                    subtitle_data = self._process_text(raw_text)
                    
                    # Update state and notify
                    self.state.current_subtitle = subtitle_data
                    self.notify_observers("subtitle_updated", subtitle_data)
                
                time.sleep(0.3)  # Capture interval
                
            except Exception as e:
                print(f"Capture loop error: {e}")
                time.sleep(1)
    
    def _process_text(self, text: str) -> SubtitleData:
        """Process raw OCR text with translation"""
        settings = self.state.translation_settings
        
        if settings.enabled and self.check_translation_package():
            translated_text = self.translation_service.translate(
                text, 
                settings.source_language, 
                settings.target_language
            )
            
            return SubtitleData(
                original_text=text,
                translated_text=translated_text,
                is_translated=True,
                source_language=settings.source_language,
                target_language=settings.target_language
            )
        else:
            return SubtitleData(
                original_text=text,
                translated_text=text,
                is_translated=False,
                source_language=settings.source_language,
                target_language=settings.target_language
            )
    
    def _update_status(self, message: str, color: str):
        """Update status message"""
        self.state.status_message = message
        self.state.status_color = color
        self.notify_observers("status_changed", {"message": message, "color": color})

    def get_translation_info(self) -> dict:
        """Get translation path information for UI display"""
        settings = self.state.translation_settings
        if not settings.enabled:
            return {'status': 'disabled', 'message': 'Translation disabled'}
        
        # Check if currently possible
        if self.translation_service.can_translate(settings.source_language, settings.target_language):
            path = self.translation_service.find_translation_path(settings.source_language, settings.target_language)
            return {
                'status': 'available',
                'path': path,
                'message': f'Ready: {" → ".join(path)}'
            }
        
        # Check if possible with installation
        if self.translation_service.can_translate_if_installed(settings.source_language, settings.target_language):
            path = self.translation_service.find_available_translation_path(settings.source_language, settings.target_language)
            required = self.translation_service.get_required_packages(settings.source_language, settings.target_language)
            return {
                'status': 'needs_install',
                'path': path,
                'required_packages': required,
                'message': f'Need {len(required)} packages for: {" → ".join(path)}'
            }
        
        return {
            'status': 'impossible',
            'message': f'No translation available for {settings.source_language} → {settings.target_language}'
        }

    def cleanup(self):
        """Cleanup resources"""
        self.stop_capture()
        self.ocr_service.cleanup()
        self.region_service.clear_region()


class TranslationCommunicationService:
    """Service to handle communication between main app and detached translation window"""
    
    def __init__(self):
        self.temp_dir = tempfile.gettempdir()
        self.subtitle_file = os.path.join(self.temp_dir, 'ocr_current_subtitle.json')
        self.status_file = os.path.join(self.temp_dir, 'ocr_translation_status.json')
        
    def save_subtitle(self, subtitle_data: Optional[SubtitleData]):
        """Save current subtitle data to temp file"""
        try:
            if subtitle_data:
                data = {
                    'original_text': subtitle_data.original_text,
                    'translated_text': subtitle_data.translated_text,
                    'is_translated': subtitle_data.is_translated,
                    'source_language': subtitle_data.source_language,
                    'target_language': subtitle_data.target_language,
                    'confidence': subtitle_data.confidence,
                    'timestamp': subtitle_data.timestamp
                }
            else:
                data = None
                
            with open(self.subtitle_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving subtitle: {e}")
    
    def load_subtitle(self) -> Optional[SubtitleData]:
        """Load current subtitle data from temp file"""
        try:
            if os.path.exists(self.subtitle_file):
                with open(self.subtitle_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data:
                        return SubtitleData(
                            original_text=data['original_text'],
                            translated_text=data['translated_text'],
                            is_translated=data['is_translated'],
                            source_language=data['source_language'],
                            target_language=data['target_language'],
                            confidence=data.get('confidence', 1.0),
                            timestamp=data.get('timestamp', 0.0)
                        )
        except Exception as e:
            print(f"Error loading subtitle: {e}")
        return None
    
    def set_window_status(self, is_detached: bool):
        """Set the detached window status"""
        try:
            status = {
                'is_detached': is_detached,
                'timestamp': time.time()
            }
            with open(self.status_file, 'w') as f:
                json.dump(status, f)
        except Exception as e:
            print(f"Error saving status: {e}")
    
    def get_window_status(self) -> bool:
        """Get the detached window status"""
        try:
            if os.path.exists(self.status_file):
                with open(self.status_file, 'r') as f:
                    data = json.load(f)
                    return data.get('is_detached', False)
        except Exception as e:
            print(f"Error loading status: {e}")
        return False
    
    def cleanup(self):
        """Clean up temp files"""
        try:
            for file_path in [self.subtitle_file, self.status_file]:
                if os.path.exists(file_path):
                    os.remove(file_path)
        except Exception as e:
            print(f"Error cleaning up: {e}")

# ============================================================================
# REGION SELECTION UI (Keep existing region selector)
# ============================================================================

class RegionSelector:
    def __init__(self):
        self.start_x = None
        self.start_y = None
        self.end_x = None
        self.end_y = None
        self.is_selecting = False

def region_selection_screen(page: ft.Page):
    """Fullscreen region selection overlay"""
    page.title = "Select Subtitle Region"
    page.window.bgcolor = ft.Colors.TRANSPARENT
    page.bgcolor = ft.Colors.TRANSPARENT
    page.window.title_bar_hidden = True
    page.window.title_bar_buttons_hidden = True
    page.window.frameless = True
    page.window.always_on_top = True
    page.window.maximized = True
    page.window.movable = False
    page.window.resizable = False
    page.window.skip_task_bar = True
    page.window.center()
    
    selector = RegionSelector()
    region_service = RegionSelectionService()
    
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        screen_width = monitor['width']
        screen_height = monitor['height']
    
    selection_rect = ft.Container(
        width=0, height=0, left=0, top=0,
        bgcolor=ft.Colors.with_opacity(0.3, ft.Colors.RED),
        border=ft.border.all(2, ft.Colors.RED),
        visible=False
    )
    
    instructions = ft.Container(
        content=ft.Text(
            "Drag to select subtitle region • ESC to cancel",
            color=ft.Colors.WHITE, size=18, text_align=ft.TextAlign.CENTER,
            weight=ft.FontWeight.BOLD
        ),
        bgcolor=ft.Colors.with_opacity(0.9, ft.Colors.BLACK),
        padding=15, border_radius=8,
        left=screen_width // 2 - 180, top=30,
    )
    
    cumulative_x = 0
    cumulative_y = 0
    
    def start_selection(e: ft.DragStartEvent):
        nonlocal cumulative_x, cumulative_y
        selector.start_x = e.global_x
        selector.start_y = e.global_y
        selector.end_x = e.global_x
        selector.end_y = e.global_y
        selector.is_selecting = True
        cumulative_x = cumulative_y = 0
        
        selection_rect.left = selector.start_x
        selection_rect.top = selector.start_y
        selection_rect.width = selection_rect.height = 2
        selection_rect.visible = True
        page.update()
    
    def update_selection(e: ft.DragUpdateEvent):
        nonlocal cumulative_x, cumulative_y
        if not selector.is_selecting:
            return
            
        cumulative_x += e.delta_x
        cumulative_y += e.delta_y
        selector.end_x = selector.start_x + cumulative_x
        selector.end_y = selector.start_y + cumulative_y
        
        left = min(selector.start_x, selector.end_x)
        top = min(selector.start_y, selector.end_y)
        width = abs(selector.end_x - selector.start_x)
        height = abs(selector.end_y - selector.start_y)
        
        selection_rect.left = left
        selection_rect.top = top
        selection_rect.width = max(width, 2)
        selection_rect.height = max(height, 2)
        page.update()
    
    def end_selection(e: ft.DragEndEvent):
        if not selector.is_selecting:
            return
            
        left = min(selector.start_x, selector.end_x)
        top = min(selector.start_y, selector.end_y)
        width = abs(selector.end_x - selector.start_x)
        height = abs(selector.end_y - selector.start_y)
        
        if width >= 20 and height >= 10:
            region = Region(int(left), int(top), int(width), int(height))
            region_service.save_region(region)
            print(f"Region saved: {region}")
        else:
            region_service.save_region(None)
            print("Region too small")
        
        selector.is_selecting = False
        page.window.close()
    
    def cancel_selection(e: ft.KeyboardEvent):
        if e.key == "Escape":
            region_service.save_region(None)
            page.window.close()
    
    gesture_detector = ft.GestureDetector(
        content=ft.Container(
            width=screen_width, height=screen_height,
            bgcolor=ft.Colors.with_opacity(0.02, ft.Colors.WHITE),
        ),
        on_pan_start=start_selection,
        on_pan_update=update_selection,
        on_pan_end=end_selection,
        drag_interval=1,
        mouse_cursor=ft.MouseCursor.PRECISE,
    )
    
    page.on_keyboard_event = cancel_selection
    page.add(ft.Stack([gesture_detector, selection_rect, instructions]))

# Helper functions for multi-page apps
def start_page(sub_page_class):
    import flet
    def APP(page: flet.Page):
        if sub_page_class.target:
            sub_page_class.target(page)
        page.update()
    flet.app(target=APP, view=sub_page_class.view)

class SubPage:
    def __init__(self, target=None, view=ft.FLET_APP):
        self.target = target
        self.view = view
    
    def start(self):
        multiprocessing.Process(target=start_page, args=[self]).start()

# ============================================================================
# TRASLATION SCREEN (detached)
# ============================================================================

def translation_overlay_screen(page: ft.Page):
    """Floating translation overlay window with file-based communication"""
    page.title = "Translation Overlay"
    page.window.bgcolor = ft.Colors.TRANSPARENT
    page.bgcolor = ft.Colors.with_opacity(0.85, ft.Colors.BLACK)
    page.window.title_bar_hidden = True
    page.window.always_on_top = True
    page.window.movable = True
    page.window.resizable = True
    page.window.minimizable = False
    page.window.skip_task_bar = True
    page.padding = 20
    page.window.width = 400
    page.window.height = 200
    
    # Communication service
    comm_service = TranslationCommunicationService()
    
    # Create the translation text widget
    translation_text = ft.Text(
        "Waiting for translation...",
        color=ft.Colors.WHITE,
        size=16,
        weight=ft.FontWeight.W_500,
        selectable=True
    )
    
    # Language info text
    language_info = ft.Text(
        "",
        color=ft.Colors.WHITE70,
        size=12,
    )
    
    # Track last processed subtitle to avoid unnecessary updates
    last_timestamp = 0
    
    def on_close():
        """Close overlay and notify main window"""
        comm_service.set_window_status(False)
        page.window.close()
    
    def update_translation_display():
        """Poll for subtitle updates and update display"""
        nonlocal last_timestamp
        
        try:
            subtitle_data = comm_service.load_subtitle()
            
            if subtitle_data and subtitle_data.timestamp > last_timestamp:
                last_timestamp = subtitle_data.timestamp
                
                if subtitle_data.translated_text.strip():
                    translation_text.value = subtitle_data.translated_text
                    translation_text.color = ft.Colors.WHITE if subtitle_data.is_translated else ft.Colors.WHITE70
                    if subtitle_data.is_translated:
                        language_info.value = f"{subtitle_data.source_language.upper()} → {subtitle_data.target_language.upper()}"
                    else:
                        language_info.value = f"Original ({subtitle_data.source_language.upper()})"
                else:
                    translation_text.value = "Waiting for translation..."
                    translation_text.color = ft.Colors.WHITE70
                    language_info.value = ""
                
                page.update()
                
        except Exception as e:
            print(f"Overlay update error: {e}")
    
    def polling_loop():
        """Main polling loop"""
        while comm_service.get_window_status():  # Continue while window should be detached
            update_translation_display()
            time.sleep(0.1)  # Poll every 100ms
        
        print("Overlay polling stopped - window was reattached")
    
    # Set initial status
    comm_service.set_window_status(True)
    
    # Load initial subtitle if available
    update_translation_display()
    
    # Start polling thread
    polling_thread = threading.Thread(target=polling_loop, daemon=True)
    polling_thread.start()
    
    # Handle window close
    def on_window_event(e):
        if e.data == "close":
            close_overlay()
    
    page.on_window_event = on_window_event
    
    page.add(
        ft.Container(
            content=ft.Column([
                # Main content area with stack for positioning
                ft.Container(
                    content=ft.Stack([
                        # Centered translation text
                        ft.Container(
                            content=translation_text,
                            expand=True,
                            alignment=ft.alignment.center
                        ),
                        # Language info positioned in top right of this container
                        ft.Container(
                            content=language_info,
                            top=0,
                            right=0,
                        )
                    ]),
                    expand=True
                )
            ]),
            padding=10,
            expand=True
        )
    )

# ============================================================================
# MODERN UI USING FACTORY COMPONENTS
# ============================================================================

class ModernOCRUI(Observer):
    def __init__(self, page: ft.Page):
        self.page = page
        self.page.window.title_bar_hidden = True
        self.controller = OCRController()
        self.controller.add_observer(self)

        self.comm_service = TranslationCommunicationService()
        self.translation_detached = False
        
        # Language options for dropdowns
        self.language_options = [
            FactoryDropdownOption("en", "English"),
            FactoryDropdownOption("de", "German"),
            FactoryDropdownOption("es", "Spanish"),
            FactoryDropdownOption("fr", "French"),
            FactoryDropdownOption("it", "Italian"),
            FactoryDropdownOption("pt", "Portuguese"),
            FactoryDropdownOption("ru", "Russian"),
            FactoryDropdownOption("ja", "Japanese"),
            FactoryDropdownOption("ko", "Korean"),
            FactoryDropdownOption("zh", "Chinese"),
            FactoryDropdownOption("ar", "Arabic"),
            FactoryDropdownOption("hi", "Hindi"),
        ]
        
        self._setup_ui()
        self._check_translation_package()
        self._start_status_monitoring()
    
    def _setup_ui(self):
        """Setup the modern UI layout"""
        self.page.title = "Polyglot"
        self.page.window.width = 480
        self.page.window.height = 700
        self.page.window.resizable = True
        self.page.padding = 20
        self.page.bgcolor = "#f8fafc"
        
        # Create direct references to text widgets to avoid indexing issues
        self.recognized_text_widget = ft.Text(
            "",
            size=12,
            color=colors_map["text_secondary"],
            selectable=True
        )
        
        self.translated_text_widget = ft.Text(
            "",
            size=14,
            color=colors_map["text_secondary"],
            selectable=True,
            weight=ft.FontWeight.W_500
        )
        
        # Header with app icon and title
        header = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Column([
                                        ft.Text(
                                            "Real time",
                                            size=24,
                                            weight=ft.FontWeight.BOLD,
                                            color=colors_map["text_secondary"]
                                        ),
                                        ft.Text(
                                            "OCR + Translation",
                                            size=24,
                                            weight=ft.FontWeight.BOLD,
                                            color=colors_map["text_secondary"]
                                        ),
                                    ], spacing=0),
                                    ft.Row(
                                        [
                                            ft.Container(
                                                ft.Image(
                                                    src="github-logo.svg",
                                                    color=ft.Colors.BLACK12,
                                                    width=15,
                                                    height=15,
                                                ),
                                                on_click=lambda e: self.page.launch_url("https://github.com/Bbalduzz/fletfactory")
                                            ),
                                            ft.Text(
                                                "v.0.0.1",
                                                size=12,
                                                color=ft.Colors.BLACK12
                                            ),
                                        ],
                                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                    )
                                ],
                                spacing = 10
                            ),
                            ft.Container(expand=True),  # Spacer
                            ft.Container(
                                ft.Image(
                                    src="logo.png",
                                    width=100,
                                    height=100,
                                ),
                                margin = ft.margin.only(bottom=20)
                            )
                        ],
                    ),
                ],
                spacing = 0
            ),
            margin=ft.margin.only(bottom=20, top=10),
        )
        
        # Region selection section
        self.region_info = ft.Container(
            content=ft.Text(
                "region: unselected",
                size=8,
                color=ft.Colors.GREY_500
            ),
            bgcolor="#f1f5f9",
            padding=ft.padding.symmetric(horizontal=12, vertical=4),
            border_radius=8,
            border=ft.border.all(1, "#cbd5e1")
        )
        
        region_section = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Column([
                        ft.Text(
                            "Select Screen Region",
                            size=16,
                            weight=ft.FontWeight.BOLD,
                            color=colors_map["text_secondary"]
                        ),
                        ft.Text(
                            "This is the region where the OCR will work on",
                            size=12,
                            color=ft.Colors.GREY_600
                        )
                    ], expand=True),
                    ft.Column([
                        FactorySecondaryButton(
                            content=ft.Text("Select Region"),
                            on_click=self._select_region
                        ),
                        self.region_info
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER)
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
            ]),
            padding=ft.padding.only(top=10),
        )
        
        # Translation settings section
        self.translation_checkbox = FactoryCheckBox(
            label="Enable",
            value=False,
            on_change=self._on_translation_settings_changed
        )
        
        self.source_dropdown = FactoryDropdown(
            options=self.language_options,
            value="it",
            width=120,
        )
        
        self.target_dropdown = FactoryDropdown(
            options=self.language_options,
            value="en",
            width=120,
        )
        
        self.package_status = ft.Container(
            content=ft.Text(
                "✓ Available",
                size=8,
                color=colors_map["primary"]
            ),
            bgcolor=colors_map["secondary"],
            padding=ft.padding.symmetric(horizontal=12, vertical=4),
            border_radius=8,
            border=ft.border.all(1, colors_map["primary"])
        )
        
        self.download_btn = FactoryButton(
            content=ft.Text("Download model"),
            on_click=self._install_package,
            visible=False
        )
        
        translation_section = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Column([
                        ft.Text(
                            "Live Translation",
                            size=16,
                            weight=ft.FontWeight.BOLD,
                            color=colors_map["text_secondary"]
                        ),
                        ft.Text(
                            "Translations are local, offline and private",
                            size=12,
                            color=ft.Colors.GREY_600
                        )
                    ], expand=True),
                    self.translation_checkbox
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Row([
                    self.source_dropdown,
                    ft.Container(
                        content=ft.Icon(ft.Icons.ARROW_FORWARD, size=16, color=ft.Colors.BLACK),
                        margin=ft.margin.symmetric(horizontal=10)
                    ),
                    self.target_dropdown,
                    ft.Container(expand=True),
                    ft.Column([
                        self.download_btn,
                        self.package_status
                    ], horizontal_alignment = ft.CrossAxisAlignment.CENTER),
                ], alignment=ft.MainAxisAlignment.CENTER)
            ], spacing=15),
            padding=ft.padding.only(top=20),
            margin=ft.margin.only(bottom=20)
        )
        
        # Text display sections - using direct widget references
        self.recognized_text = ft.Container(
            content=ft.Column([
                ft.Text(
                    "Recognized text",
                    size=14,
                    weight=ft.FontWeight.BOLD,
                    color=colors_map["text_secondary"]
                ),
                ft.Container(
                    content=self.recognized_text_widget,  # Direct reference
                    height=50,
                    bgcolor="#ffffff",
                    border_radius=8,
                    padding=12,
                    alignment=ft.alignment.top_left
                )
            ], spacing=8),
            margin=ft.margin.only(bottom=20)
        )

        self.detach_btn = ft.Container(
            ft.Image(
                src = "icons/square-arrow-out-up-right.svg",
                width = 20,
                height = 20,
            ),
            on_click=self._detach_translation
        )

        self.attach_btn = ft.IconButton(
            icon=ft.Icons.PICTURE_IN_PICTURE_ALT,
            icon_color=colors_map["text_secondary"],
            icon_size=20,
            tooltip="Attach translation window",
            on_click=self._attach_translation,
            visible=False
        )
        
        
        self.translated_text = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Text(
                        "Translated text",
                        size=14,
                        weight=ft.FontWeight.BOLD,
                        color=colors_map["text_secondary"]
                    ),
                    ft.Container(expand=True),
                    self.attach_btn,
                    self.detach_btn
                ]),
                ft.Container(
                    content=self.translated_text_widget,  # Direct reference
                    height=100,
                    bgcolor="#ffffff",
                    border_radius=8,
                    padding=12,
                    alignment=ft.alignment.top_left
                )
            ], spacing=8),
            margin=ft.margin.only(bottom=20)
        )
        
        # Control buttons
        self.start_btn = FactoryButton(
            content=ft.Row([
                ft.Icon(ft.Icons.PLAY_ARROW, size=16, color=ft.Colors.BLACK),
                ft.Text("Start", color=ft.Colors.BLACK)
            ], spacing=8, tight=True),
            on_click=self._start_ocr,
            disabled=True
        )
        
        self.stop_btn = FactorySecondaryButton(
            content=ft.Row([
                ft.Icon(ft.Icons.STOP, size=16),
                ft.Text("Stop")
            ], spacing=8, tight=True),
            on_click=self._stop_ocr,
            disabled=True
        )

        fab_container = ft.Container(
            content=ft.Row([
                self.start_btn,
                self.stop_btn
            ], spacing=15),
            shadow=ft.BoxShadow(
                spread_radius=1,
                blur_radius=10,
                color=ft.Colors.with_opacity(0.25, ft.Colors.BLACK),
            ),
            right=20,
            bottom=20,
        )
        
        # Add all components to page
        self.page.add(ft.Column([
            header,
            ft.Column([
                region_section,
                translation_section,
            ], spacing = 0),
            self.recognized_text,
            self.translated_text,
        ], spacing=0, scroll=ft.ScrollMode.AUTO))

        self.page.overlay.append(fab_container)
        self.page.update()
        
        self.page.on_window_event = self._on_window_close
        
        # Set up dropdown change handlers
        self.source_dropdown.on_change = self._on_translation_settings_changed
        self.target_dropdown.on_change = self._on_translation_settings_changed
    
    def update(self, event_type: str, data: Any):
        """Observer pattern update method"""
        if event_type == "subtitle_updated":
            self._update_subtitle_display(data)
        elif event_type == "status_changed":
            self._update_status(data)
        elif event_type == "region_changed":
            self._update_region_info(data)
        elif event_type == "capture_started":
            self._on_capture_started()
        elif event_type == "capture_stopped":
            self._on_capture_stopped()

    def _start_status_monitoring(self):
        """Start monitoring the detached window status"""
        def monitor_status():
            while True:
                try:
                    # Check if window was closed externally
                    if self.translation_detached and not self.comm_service.get_window_status():
                        # Window was closed, reattach
                        self.translation_detached = False
                        self.translated_text.visible = True
                        self.detach_btn.visible = True
                        self.attach_btn.visible = False
                        
                        # Update with current subtitle if available
                        if self.controller.state.current_subtitle:
                            self._update_subtitle_display(self.controller.state.current_subtitle)
                        
                        self.page.update()
                        print("Translation window reattached automatically")
                    
                    time.sleep(0.5) 
                except Exception as e:
                    print(f"Status monitoring error: {e}")
                    time.sleep(1)
        
        monitoring_thread = threading.Thread(target=monitor_status, daemon=True)
        monitoring_thread.start()

    def _detach_translation(self, e):
        """Detach translation section to a separate window"""
        if not self.translation_detached:
            print("Detaching translation window...")
            self.translation_detached = True
            
            # Hide the translated text section in main window
            self.translated_text.visible = False
            self.detach_btn.visible = False
            self.attach_btn.visible = True
            
            # Save current subtitle to file
            if self.controller.state.current_subtitle:
                self.comm_service.save_subtitle(self.controller.state.current_subtitle)
            
            # Create and start the overlay window
            overlay_page = SubPage(target=translation_overlay_screen)
            overlay_page.start()
            
            self.page.update()
            print("Translation window detached")
    
    def _attach_translation(self, e):
        """Attach translation section back to main window"""
        if self.translation_detached:
            print("Attaching translation window...")
            self.translation_detached = False
            
            # Signal the detached window to close
            self.comm_service.set_window_status(False)
            
            # Show the translated text section in main window
            self.translated_text.visible = True
            self.detach_btn.visible = True
            self.attach_btn.visible = False
            
            # Update with current subtitle if available
            if self.controller.state.current_subtitle:
                self._update_subtitle_display(self.controller.state.current_subtitle)
            
            self.page.update()
            print("Translation window attached")
    
    def _update_subtitle_display(self, subtitle_data: SubtitleData):
        """Update subtitle display"""
        try:
            # Always save subtitle data for detached window
            self.comm_service.save_subtitle(subtitle_data)
            
            # Update recognized text using direct widget reference
            if subtitle_data.original_text.strip():
                self.recognized_text_widget.value = subtitle_data.original_text
                self.recognized_text_widget.color = colors_map["text_secondary"]
            else:
                self.recognized_text_widget.value = "Waiting for text..."
                self.recognized_text_widget.color = ft.Colors.GREY_400
            
            # Update translated text using direct widget reference (only if not detached)
            if not self.translation_detached:
                if subtitle_data.translated_text.strip():
                    self.translated_text_widget.value = subtitle_data.translated_text
                    self.translated_text_widget.color = colors_map["primary"] if subtitle_data.is_translated else colors_map["text_secondary"]
                else:
                    self.translated_text_widget.value = "Waiting for translation..."
                    self.translated_text_widget.color = ft.Colors.GREY_400
            
            self.page.update()
            
        except Exception as e:
            print(f"Error updating subtitle display: {e}")
            traceback.print_exc()
    
    def _update_status(self, status_data: Dict):
        """Update status message - could add a status indicator if needed"""
        pass
    
    def _update_region_info(self, region: Region):
        """Update region info display"""
        self.region_info.content.value = f"region: {region.width} x {region.height}"
        self.region_info.bgcolor = colors_map["secondary"]
        self.region_info.content.color = colors_map["text_secondary"]
        self.region_info.border = ft.border.all(1, colors_map["primary"])
        self.start_btn.disabled = False
        self.page.update()
    
    def _on_capture_started(self):
        """Handle capture started event"""
        self.start_btn.disabled = True
        self.stop_btn.disabled = False
        
        # Update region info to show active state
        self.region_info.bgcolor = "#dcfce7"  # Light green
        self.region_info.border = ft.border.all(1, "#16a34a")  # Green border
        self.region_info.content.color = "#16a34a"  # Green text
        
        self.page.update()
    
    def _on_capture_stopped(self):
        """Handle capture stopped event"""
        self.start_btn.disabled = False
        self.stop_btn.disabled = True
        
        # Reset region info to normal state
        self.region_info.bgcolor = colors_map["secondary"]
        self.region_info.border = ft.border.all(1, colors_map["primary"])
        self.region_info.content.color = colors_map["primary"]
        
        self.page.update()
    
    def _select_region(self, e):
        """Handle region selection"""
        self.controller.region_service.clear_region()
        region_page = SubPage(target=region_selection_screen)
        region_page.start()
        
        def check_selection():
            for i in range(300):  # 30 seconds timeout
                time.sleep(0.1)
                region = self.controller.region_service.load_region()
                if region is not None:
                    self.controller.set_region(region)
                    return
            
            # Timeout - region selection cancelled
            pass
        
        threading.Thread(target=check_selection, daemon=True).start()
    
    def _start_ocr(self, e):
        """Start OCR capture"""
        self.controller.start_capture()
    
    def _stop_ocr(self, e):
        """Stop OCR capture"""
        self.controller.stop_capture()
    
    def _on_translation_settings_changed(self, e):
        """Handle translation settings change"""
        settings = TranslationSettings(
            enabled=self.translation_checkbox.value,
            source_language=self.source_dropdown.value or "de",
            target_language=self.target_dropdown.value or "en"
        )
        
        self.controller.set_translation_settings(settings)
        self._check_translation_package()
        
        # Show/hide translation section based on enabled state
        self.translated_text.visible = settings.enabled
        self.page.update()
    
    def _check_translation_package(self):
        """Check translation package availability with pivot support"""
        settings = self.controller.state.translation_settings
        
        if not settings.enabled:
            self.package_status.content.value = "Translation disabled"
            self.package_status.content.color = ft.Colors.GREY_500
            self.package_status.bgcolor = "#f1f5f9"
            self.package_status.border = ft.border.all(1, "#cbd5e1")
            self.download_btn.visible = False
        else:
            # Get translation info
            info = self.controller.get_translation_info()
            
            if info['status'] == 'available':
                # Translation is ready
                path = info['path']
                if len(path) == 2:
                    self.package_status.content.value = "✓ Direct"
                else:
                    self.package_status.content.value = f"✓ Via {path[1]}"
                
                self.package_status.content.color = colors_map["primary"]
                self.package_status.bgcolor = colors_map["secondary"]
                self.package_status.border = ft.border.all(1, colors_map["primary"])
                self.download_btn.visible = False
                
            elif info['status'] == 'needs_install':
                # Translation possible but needs packages
                required_count = len(info['required_packages'])
                path = info['path']
                
                if len(path) == 2:
                    self.package_status.content.value = f"⚠ Need direct package"
                else:
                    self.package_status.content.value = f"⚠ Need {required_count} packages"
                
                self.package_status.content.color = "#dc2626"
                self.package_status.bgcolor = "#fef2f2"
                self.package_status.border = ft.border.all(1, "#dc2626")
                self.download_btn.visible = True
                
                # Update button text
                if required_count == 1:
                    self.download_btn.content.value = "Download 1 model"
                else:
                    self.download_btn.content.value = f"Download {required_count} models"
                    
            else:  # impossible
                self.package_status.content.value = "❌ Not available"
                self.package_status.content.color = "#dc2626"
                self.package_status.bgcolor = "#fef2f2"
                self.package_status.border = ft.border.all(1, "#dc2626")
                self.download_btn.visible = False
        
        self.page.update()
    
    def _install_package(self, e):
        """Install translation package"""
        settings = self.controller.state.translation_settings
        self.package_status.content.value = "📦 Installing..."
        self.package_status.content.color = "#16a34a"
        self.package_status.bgcolor = "#dcfce7"
        self.package_status.border = ft.border.all(1, "#16a34a")
        self.download_btn.disabled = True
        self.page.update()
        
        def install_in_background():
            success = self.controller.install_translation_package()
            if success:
                self.package_status.content.value = "✓ Available"
                self.package_status.content.color = "#16a34a"
                self.package_status.bgcolor = "#dcfce7"
                self.package_status.border = ft.border.all(1, "#16a34a")
                self.download_btn.visible = False
            else:
                self.package_status.content.value = "❌ Failed"
                self.package_status.content.color = "#dc2626"
                self.package_status.bgcolor = "#fef2f2"
                self.package_status.border = ft.border.all(1, "#dc2626")
                self.download_btn.disabled = False
            self.page.update()
        
        threading.Thread(target=install_in_background, daemon=True).start()
    
    def _on_window_close(self, e):
        """Handle main window close"""
        # Signal detached window to close
        self.comm_service.set_window_status(False)
        # Clean up temp files
        self.comm_service.cleanup()
        # Clean up controller
        self.controller.cleanup()


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main(page: ft.Page):
    ui = ModernOCRUI(page)

if __name__ == "__main__":
    ft.app(target=main, assets_dir = "assets")