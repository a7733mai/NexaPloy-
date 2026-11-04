import os
import sys

CONTROL_PLANE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CONTROL_PLANE not in sys.path:
	sys.path.insert(0, CONTROL_PLANE)
