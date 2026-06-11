@echo off
cd /d %~dp0
pip install --quiet regex zeroconf prompt_toolkit
python -m netnotepad
pause
