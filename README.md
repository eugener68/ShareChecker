# ShareChecker

Small Windows desktop app (Tkinter) that shows a share card for a ticker symbol.

Default symbol at startup: `DOX`.

## Features
- Opening price
- Close price
  - If NYSE is still open, card uses previous day close
- Daily change (%)
- Daily change ($)
- Interactive symbol input (`Load` button + Enter key)
- Real-time symbol validation against online US symbol lists
- Prefix-based autocomplete suggestions while typing (scrollable)

## Requirements
- Windows
- Python 3.11+ (tested with newer versions too)

## Setup
1. Create and activate a virtual environment.
2. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

## Run
```powershell
python app.py
```

Optional startup symbol:
```powershell
python app.py --symbol MSFT
```

## Proxy / TLS (corporate network)
Set proxy variables in the same PowerShell session before running:
```powershell
$env:HTTP_PROXY = "<YOUR_PROXY>"
$env:HTTPS_PROXY = "<YOUR_PROXY>"
python app.py
```

If TLS inspection is enabled, set your corporate CA bundle:
```powershell
$env:REQUESTS_CA_BUNDLE = "<CA_PATH>"
python app.py
```

Or pass CA bundle directly:
```powershell
python app.py --ca-bundle "<CA_PATH>"
```

Temporary troubleshooting only (not recommended):
```powershell
python app.py --insecure-ssl
```
