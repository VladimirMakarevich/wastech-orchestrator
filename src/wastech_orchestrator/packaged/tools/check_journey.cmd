@echo off
rem Windows launcher for the extensionless check_journey script (the interpreter ignores the file's
rem lack of a .py suffix). %~dp0 is this wrapper's own directory, so the sibling script is always
rem found regardless of the caller's cwd; stdin/stdout are inherited from the tool-node runner.
python "%~dp0check_journey" %*
