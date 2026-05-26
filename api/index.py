import sys
import os

# Add parent directory to sys.path to import app.py from the root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
