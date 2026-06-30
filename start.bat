@echo off
cd /d %~dp0
pip install --quiet regex zeroconf prompt_toolkit tkinterdnd2
python -m netnotepad
pause
