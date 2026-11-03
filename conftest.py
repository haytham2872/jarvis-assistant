"""Settings the test suite runs under, set before `backend` is imported.

Importing `backend` sets the process clock to JARVIS_TIMEZONE, so this has to happen
before backend/conftest.py, which is itself inside the package. Europe/Paris: the tests
exercise real clock changes (25 October 2026, 28 March 2027) in a zone that has them.
"""
import os

os.environ["JARVIS_TIMEZONE"] = "Europe/Paris"
os.environ["JARVIS_OWNER_NAME"] = "Sam"
os.environ["JARVIS_OWNER_PRONOUNS"] = "he"
