"""Точка входа для Vercel serverless."""

import sys
from pathlib import Path

# Добавляем родительскую папку в sys.path, чтобы импорты работали
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app
