import sys
import os

# Add the 'src' directory to the Python path so it can find 'aimodels'
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

# Now import the handler
from aimodels.dashboard import Handler as handler
