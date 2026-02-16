
import sys
import os

# Add current directory to sys.path
sys.path.append(os.getcwd())

try:
    print("Attempting to import tests.e2e.test_security...")
    import tests.e2e.test_security
    print("Import successful!")
except Exception as e:
    print(f"Import failed: {e}")
    import traceback
    traceback.print_exc()

try:
    print("Attempting to import pocketpaw.dashboard...")
    from pocketpaw import dashboard
    print("pocketpaw.dashboard import successful!")
except Exception as e:
    print(f"pocketpaw.dashboard import failed: {e}")
    import traceback
    traceback.print_exc()
