"""Run a factory module under python -I: no current folder, script folder or PYTHON* variable on the import path, so a file planted beside downloaded data is never imported. Only the repository is added.

usage (from the repository root): python -I training/factory/isolated.py training.factory.public fetch eia
"""
import os
import runpy
import site
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(site.getusersitepackages())     # -I leaves the user's own packages out; openpyxl and sqlalchemy live there
module = sys.argv[1]
sys.argv = [module] + sys.argv[2:]
runpy.run_module(module, run_name="__main__", alter_sys=True)
