@echo off
schtasks /create /tn "FlightMonitor_0900" /tr "python F:\codex\flight_monitor\main.py" /sc daily /st 09:00 /f
schtasks /create /tn "FlightMonitor_1500" /tr "python F:\codex\flight_monitor\main.py" /sc daily /st 15:00 /f
schtasks /create /tn "FlightMonitor_2100" /tr "python F:\codex\flight_monitor\main.py" /sc daily /st 21:00 /f
echo Done.
pause