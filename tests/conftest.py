import sys
from pathlib import Path

# Let tests import the helpers in tests/fakes.py.
sys.path.insert(0, str(Path(__file__).parent))
