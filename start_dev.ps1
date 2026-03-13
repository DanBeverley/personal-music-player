Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Starting Auralis Development Environment" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# Start Python Proxy in a new detached PowerShell window
Write-Host "[1/2] Booting Python Proxy Server on Port 8010..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd ingestion\python_proxy; Write-Host 'Auralis Python Engine' -ForegroundColor Cyan; python -m uvicorn server:app --host 0.0.0.0 --port 8010"

# Start Flutter App in the current window
Write-Host "[2/2] Booting Flutter Client..." -ForegroundColor Yellow
cd app
flutter run -d emulator-5554
